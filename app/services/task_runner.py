"""异步任务运行器 —— 在后台线程运行 LangGraph 工作流，
逐节点更新阶段进度，并把 完成/暂停(确认点)/错误 状态持久化到会话存储。

对外接口：
  start_run(session_id, user_input, user_id)  启动一次创作（后台执行）
  resume_run(session_id, user_id, decision, feedback)  对暂停的确认点续跑
"""

import threading
import time

from langgraph.types import Command

from app.core.config import settings
from app.services.events import get_hub
from app.services.logging import redact
from app.workflows.main_workflow import app_workflow
from app.services.session_store import get_session_store

_JOBS: dict[str, threading.Thread] = {}
# 实时 AI 思考日志：session_id -> [{"label","text"}, ...]（进程内存，供状态接口实时展示）
_THINKING: dict[str, list] = {}
# 中途打断标记：session_id -> True
_CANCEL: dict[str, bool] = {}
# 全局锁：上述 dict 会被后台线程与主线程并发读写
_LOCK = threading.Lock()


def get_thinking(session_id: str) -> list:
    """返回某会话实时累积的「AI 思考过程」。"""
    return _THINKING.get(session_id, [])


def cancel_run(session_id: str) -> bool:
    """中途打断：标记取消，后台线程在下一个节点边界停止。"""
    store = get_session_store()
    row = store.get(session_id)
    if not row:
        return False
    with _LOCK:
        _CANCEL[session_id] = True
    # 立即把状态置为 cancelled（后台线程随后也会同步）
    store.save(session_id, row.get("user_id") or "local", row.get("user_input", ""),
               "cancelled", "cancelled", row.get("payload"))
    return True


def _extract_review(data) -> dict:
    """从 __interrupt__ chunk 中取出人工确认点内容（Json 可序列化）。

    兼容：data 可能是 tuple/list of Interrupt、单个 dict、或已是 dict。
    （stream_mode='updates' 时 __interrupt__ 的 value 往往是 tuple 包裹的 Interrupt 对象）
    """
    items = data if isinstance(data, (list, tuple)) else [data]
    out = []
    for it in items:
        if isinstance(it, dict):
            out.append(it)
        elif hasattr(it, "value"):
            out.append(getattr(it, "value"))
        else:
            out.append(it)
    if not out:
        return {}
    return out[0] if len(out) == 1 else out


def _run_stream(job_input, session_id: str, user_id: str, user_input: str) -> None:
    """在后台线程里跑图（stream 模式），逐节点更新进度和状态。"""
    from app.services.runtime_settings import set_context

    # 本线程的兜底绑定（供 stream 之外、节点之外的读取点使用）；
    # 每个工作流节点进入时还会用 State 里的 run_context 再绑定一次（见 app/core/context.py）。
    set_context(user_id, session_id)
    store = get_session_store()
    config = {"configurable": {"thread_id": session_id}}
    hub = get_hub()

    # 预期会跑的节点顺序（来自注册表，避免前端/后端各自硬编码）。
    # 用途：chunk 里只会出现**实际执行**的节点，被 start/end 区间跳过的不会出现；
    # 拿它做差集才能如实标出 skipped，而不是把"没跑到"当成"没发生"。
    #
    # 命名空间：workflow 用 v1 节点名（story_planner/critics/prompt_compiler/…），
    # 而注册表与画布用 v2 key（showrunner/script_editor/technical_director/…）。
    # 事件统一翻译成 **注册表 key**，否则前端按注册表渲染的节点点不亮。
    try:
        from app.registry import api as _registry

        expected = list(_registry.execution_order())
        _to_reg = _registry.to_registry_node
    except Exception:  # noqa: BLE001  注册表不可用时退化为"只报实际发生的"
        expected = []
        _to_reg = lambda name: name  # noqa: E731

    nodes_state: dict[str, str] = {k: "pending" for k in expected}

    def _emit_progress(current: str | None) -> None:
        """广播一次完整进度快照，供晚连上的画布一次性对齐。"""
        hub.publish(session_id, "progress", nodes=dict(nodes_state), current=current)

    try:
        interrupted = False
        result = None
        phase = "start"
        review = None
        for chunk in app_workflow.stream(job_input, config=config, stream_mode="updates"):
            if _CANCEL.get(session_id):
                # 中途被打断：保存 cancelled 并退出
                store.save(session_id, user_id, user_input, "cancelled", phase or "cancelled", result or {})
                hub.publish(session_id, "status", status="cancelled", phase=phase)
                hub.close_stream(session_id)
                with _LOCK:
                    _CANCEL.pop(session_id, None)
                return
            for node, data in chunk.items():
                if node == "__interrupt__":
                    interrupted = True
                    review = _extract_review(data)
                    store.save(session_id, user_id, user_input, "review", "review", review)
                    review_node = review.get("node") if isinstance(review, dict) else None
                    if review_node:
                        reg_key = _to_reg(review_node)
                        nodes_state[reg_key] = "review"
                        hub.publish(session_id, "stage", stage=reg_key, status="review",
                                    workflow_node=review_node)
                        _emit_progress(reg_key)
                    hub.publish(session_id, "review", review=review)
                    hub.publish(session_id, "status", status="waiting_review")
                elif isinstance(data, dict):
                    # 确认点节点（review_*）不是"环节"，它们的状态由 review 事件表达；
                    # 若当成环节上报，画布上会出现一堆无对应职业的"已完成"节点。
                    if node.startswith("review_"):
                        continue
                    phase = data.get("current_phase", node)
                    # 实时累积「AI 思考过程」
                    tl = data.get("thinking_log")
                    if tl:
                        with _LOCK:
                            _THINKING.setdefault(session_id, []).extend(tl)
                    store.save(session_id, user_id, user_input, "running", phase, None)
                    if "result" in data:
                        result = data["result"]

                    # 该节点已完成；按注册表 key 记录
                    reg_key = _to_reg(node)
                    nodes_state[reg_key] = "done"
                    hub.publish(session_id, "stage", stage=reg_key, status="done",
                                workflow_node=node)
                    _emit_progress(_to_reg(phase))

                    if tl:
                        for item in tl:
                            hub.publish(session_id, "thinking", **item)
            # mock 模式：每环节稍作停顿，让前端可见地看到进度推进
            if settings.mock_llm or settings.mock_image:
                time.sleep(0.4)
        # 不论是否停在确认点，都把"本次没执行到的环节"补成 skipped，
        # 这样前端拿到的进度始终是完整的（而不是一堆永远 pending 的节点）。
        for key, st in list(nodes_state.items()):
            if st == "pending":
                nodes_state[key] = "skipped"
                hub.publish(session_id, "stage", stage=key, status="skipped")
        _emit_progress(_to_reg(phase))

        if _CANCEL.get(session_id):
            store.save(session_id, user_id, user_input, "cancelled", phase or "cancelled", result or {})
            hub.publish(session_id, "status", status="cancelled", phase=phase)
        elif not interrupted:
            store.save(session_id, user_id, user_input, "done", phase or "done", result or {})
            hub.publish(session_id, "done", phase=phase)
            hub.publish(session_id, "status", status="done", phase=phase)
        hub.close_stream(session_id)
    except Exception as exc:  # noqa: BLE001
        store.save(session_id, user_id, user_input, "error", "error", {"error": redact(str(exc))})
        hub.publish(session_id, "error", message=redact(str(exc)))
        hub.publish(session_id, "status", status="failed")
        hub.close_stream(session_id)


def start_run(session_id: str, user_input: str, user_id: str = "anon", seed: dict | None = None) -> None:
    """异步启动一次创作（可带 seed：已有中间产物则跳过对应环节）。"""
    store = get_session_store()
    with _LOCK:
        _THINKING.pop(session_id, None)  # 新会话清空思考日志
        _CANCEL.pop(session_id, None)    # 清空取消标记
    store.save(session_id, user_id, user_input, "running", "start", None)
    # run_context 进 State：此后"谁在跑"随状态传递，不依赖 thread-local
    from app.core.context import RunContext

    state = {
        "user_input": user_input,
        "session_id": session_id,
        "run_context": RunContext(user_id=user_id, session_id=session_id).as_dict(),
    }
    if seed:
        state.update(seed)
    t = threading.Thread(target=_run_stream, args=(state, session_id, user_id, user_input), daemon=True)
    with _LOCK:
        _JOBS[session_id] = t
    t.start()


def has_checkpoint(session_id: str) -> bool:
    """该会话在图 checkpointer 里是否还有可续跑的状态。

    背景（01-usage §6.1 实测）：`MemorySaver` 是**进程内存**，服务重启后
    `sessions.status` 仍写着 `review`，但 checkpointer 里已无对应线程；
    此时 `Command(resume=...)` 会在**空 checkpoint 上从头重放整张图**——
    不报错、却把已完成的工作重做一遍（真实 provider 下等于重复烧钱）。

    用 `get_state()` 的 `created_at` 判定：不存在时该字段为 None。
    """
    try:
        snap = app_workflow.get_state({"configurable": {"thread_id": session_id}})
    except Exception:  # noqa: BLE001  取不到就按"没有"处理，宁可拒绝也不要重放
        return False
    return getattr(snap, "created_at", None) is not None


def resume_run(session_id: str, user_id: str, decision: str, feedback: dict, content=None) -> tuple[bool, str]:
    """对暂停的确认点/人工接管段续跑（approve / redo+feedback / 提供 content）。

    Returns:
        (是否已启动, 说明)。**若 checkpoint 已丢失（如服务重启过），拒绝续跑**并返回
        (False, 原因)，避免静默重放整张图。
    """
    store = get_session_store()
    existing = store.get(session_id) or {}

    # 前置校验 1：会话必须存在
    if not existing:
        return False, f"会话不存在：{session_id}"

    # 前置校验 2：必须**停在确认点**才能续跑。
    #
    # 为什么在服务层也要查：`/api/crew/resume` 会查，但 `resume_run` 是个公共函数，
    # 脚本/测试可直接调用。若不查，对已完成或运行中的会话调它会**再跑一遍**——
    # 真实 provider 下就是重复烧钱。LangGraph 在跑完后仍保留 checkpoint，
    # 所以"有 checkpoint"不等于"可以续跑"。
    if existing.get("status") != "review":
        return False, (
            f"会话不在确认点，无法续跑（当前状态：{existing.get('status')}）。"
            "只有停在人工确认点的会话才能继续。"
        )

    # 前置校验 3：断点必须还在内存中（服务重启后 MemorySaver 会丢）
    if not has_checkpoint(session_id):
        reason = (
            "续跑状态已丢失（服务可能重启过）：当前会话的工作流断点不在内存中。"
            "为避免重复执行（可能重复计费），本次续跑已被拒绝。"
            "请重新发起一次创作。"
        )
        store.save(session_id, user_id, existing.get("user_input", ""),
                   "failed", "resume_lost", {"error": reason, "reason": "checkpoint_lost"})
        return False, reason

    store.save(session_id, user_id, existing.get("user_input", ""), "running", "resume", None)
    command = Command(resume={"decision": decision, "feedback": feedback, "content": content})
    t = threading.Thread(target=_run_stream, args=(command, session_id, user_id, existing.get("user_input", "")), daemon=True)
    with _LOCK:
        _JOBS[session_id] = t
    t.start()
    return True, "accepted"
