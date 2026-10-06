"""异步任务运行器 —— 在后台线程运行 LangGraph 工作流，
逐节点更新阶段进度，并把 完成/暂停(确认点)/错误 状态持久化到会话存储。

对外接口：
  start_run(session_id, user_input, user_id)  启动一次创作（后台执行）
  resume_run(session_id, user_id, decision, feedback)  对暂停的确认点续跑
"""

import logging
import threading
import time

from langgraph.types import Command

from app.core.config import settings
from app.services.events import get_hub
from app.services.logging import redact
# ⚠️ 不再 `from ... import app_workflow` —— 那是**按值绑定**，A2 运行时换图后
# 这里拿到的仍是第一张图。改成每次经 `current_graph()` 取"当前那张"
# （共享同一个 checkpointer，所以在途会话的断点不受换图影响）。
from app.workflows.graph_manager import current_graph
from app.services.session_store import get_session_store

logger = logging.getLogger("little_crew")

_JOBS: dict[str, threading.Thread] = {}
# 实时 AI 思考日志：session_id -> [{"label","text"}, ...]（进程内存，供状态接口实时展示）
_THINKING: dict[str, list] = {}
# 中途打断标记：session_id -> True
_CANCEL: dict[str, bool] = {}
# 全局锁：上述 dict 会被后台线程与主线程并发读写
_LOCK = threading.Lock()

#: 单会话并发上限。同一会话同时只允许一个 runner：
#: 两个线程用同一个 `thread_id` 跑同一张图会重复执行下游节点
#: （含真实生图/生视频＝重复烧钱），而 LangGraph 的 MemorySaver 存储无锁，
#: checkpoint 会互相覆盖导致续跑状态错乱。
_MAX_RUNNERS_PER_SESSION = 1


def get_thinking(session_id: str) -> list:
    """返回某会话实时累积的「AI 思考过程」**快照**。

    必须复制：调用方（`/api/crew/status`）会把它交给 JSON 序列化，
    而同一时刻后台线程正在 `append`。直接返回内部列表 = 把
    "边序列化边被改写"的竞态暴露出去（可能在迭代中途变长）。
    """
    with _LOCK:
        return list(_THINKING.get(session_id, ()))


def _join_job_locked(session_id: str) -> bool:
    """在**已持锁**的前提下判断该会话是否已有存活的 runner。

    顺带把已结束的线程条目清掉——否则 `_JOBS` 只增不减，
    且 Thread 对象会一直持有 `args`（含该会话的 State，可能很大），
    等于把历史会话永久钉在内存里。
    """
    t = _JOBS.get(session_id)
    if t is None:
        return False
    if t.is_alive():
        return True
    _JOBS.pop(session_id, None)
    return False


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


def _run_stream(job_input, session_id: str, user_id: str, user_input: str, project: str = "") -> None:
    """在后台线程里跑图（stream 模式），逐节点更新进度和状态。"""
    from app.services.runtime_settings import set_context

    # 本线程的兜底绑定（供 stream 之外、节点之外的读取点使用）；
    # 每个工作流节点进入时还会用 State 里的 run_context 再绑定一次（见 app/core/context.py）。
    set_context(user_id, session_id, project)
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
        for chunk in current_graph().stream(job_input, config=config, stream_mode="updates"):
            if _CANCEL.get(session_id):
                # 中途被打断：保存 cancelled 并退出。
                # 关流与清理交给 finally（这里 return 也会走 finally），
                # 避免"两处各写一遍清理"以后漂移。
                store.save(session_id, user_id, user_input, "cancelled", phase or "cancelled", result or {})
                hub.publish(session_id, "status", status="cancelled", phase=phase)
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
    except Exception as exc:  # noqa: BLE001
        # 兜底写入本身也可能失败（SQLite locked / 磁盘满）。若让它抛出去：
        #   线程静默死亡（daemon 线程，无日志出口）→ 会话永久停在 running，
        #   且 `close_stream` 执行不到 → 所有 SSE 订阅者永久等待、前端永远"运行中"。
        # 所以这里再包一层，并把清理挪到 finally。
        logger.exception("[task] 会话 %s 运行失败", session_id)
        try:
            store.save(session_id, user_id, user_input, "error", "error",
                       {"error": redact(str(exc))})
        except Exception:  # noqa: BLE001
            logger.exception("[task] 会话 %s 的失败状态也写不进去（DB 异常）", session_id)
        try:
            hub.publish(session_id, "error", message=redact(str(exc)))
            hub.publish(session_id, "status", status="failed")
        except Exception:  # noqa: BLE001
            logger.exception("[task] 会话 %s 的失败事件推送失败", session_id)
    finally:
        # 无论成功、失败还是取消，都必须关流 + 清理标记：
        #   - close_stream 让 SSE 订阅者收到哨兵并结束，不再无限等待；
        #   - _CANCEL/_JOBS 不清就会只增不减（内存泄漏），
        #     而 _JOBS 里的 Thread 一直持有 args（含 State，可能很大）。
        try:
            hub.close_stream(session_id)
        except Exception:  # noqa: BLE001
            logger.exception("[task] 会话 %s 关闭事件流出错", session_id)
        with _LOCK:
            _CANCEL.pop(session_id, None)
            _JOBS.pop(session_id, None)
            _THINKING.pop(session_id, None)


#: 允许由调用方"预置"进 State 的键（白名单）。
#:
#: ⚠️ 为什么必须白名单而不是 `state.update(seed)` 全收：
#:   `seed` 来自请求体，而 State 里有 `run_context`（决定**以谁的身份**读设置、
#:   用谁的 API Key、产物写谁的目录）。无白名单时攻击者可传
#:   `{"run_context": {"user_id": "受害者"}}` 直接改写身份（已实测）。
#:   除身份外，`images`/`scripts`/`character_reference_images` 等键也会影响
#:   真实调用（例如让服务端去下载任意地址），所以同样不能任意注入。
#:
#: 这里只留**测试/调试真正需要**的几个中性键；其余一律丢弃并记警告。
_ALLOWED_SEED_KEYS = frozenset({
    "user_input", "topic", "style", "aspect_ratio", "video_seconds",
    "mock", "dry_run", "feedback", "notes",
})


def _sanitize_seed(seed: dict) -> dict:
    """只保留白名单内的 seed 键；拒绝 `run_context` 等敏感键并记录。

    白名单之外的键**直接丢弃**（不报错——seed 本身是可选调试入口，
    为了一个未知键让整次运行失败不划算），但会留下 warning 便于排查
    "为什么我的 seed 没生效"。
    """
    if not isinstance(seed, dict):
        logger.warning("[task] seed 不是 dict（%s），已忽略", type(seed).__name__)
        return {}
    allowed = {k: v for k, v in seed.items() if k in _ALLOWED_SEED_KEYS}
    rejected = sorted(set(seed) - _ALLOWED_SEED_KEYS)
    if rejected:
        logger.warning("[task] seed 里有 %d 个键被白名单拒绝：%s",
                       len(rejected), ", ".join(rejected[:8]))
    return allowed


def start_run(
    session_id: str,
    user_input: str,
    user_id: str = "anon",
    seed: dict | None = None,
    project: str = "",
) -> None:
    """异步启动一次创作（可带 seed：已有中间产物则跳过对应环节）。"""
    store = get_session_store()
    with _LOCK:
        # 原子占用：检查与置位必须在**同一把锁**内完成。
        # 否则两个并发请求都会通过检查、各自起一个线程、用同一个 thread_id
        # 跑同一张图 → 下游节点（含真实生图/生视频）重复执行＝重复烧钱，
        # 且 LangGraph 的 MemorySaver 无锁，checkpoint 会互相覆盖。
        if _join_job_locked(session_id):
            logger.warning("[task] 会话 %s 已在运行，忽略重复启动", session_id)
            return
        _THINKING.pop(session_id, None)  # 新会话清空思考日志
        _CANCEL.pop(session_id, None)    # 清空取消标记
    store.save(session_id, user_id, user_input, "running", "start", None)
    # run_context 进 State：此后"谁在跑"随状态传递，不依赖 thread-local
    from app.core.context import RunContext

    state = {
        "user_input": user_input,
        "session_id": session_id,
    }
    if seed:
        state.update(_sanitize_seed(seed))
    # ⚠️ run_context 必须**最后**写，且不接受 seed 里的值。
    #
    # 原先的顺序是 `state["run_context"] = RunContext(user_id=user_id, ...)`
    # 再 `state.update(seed)` —— 而 seed 来自请求体（`payload["seed"]`）。
    # 于是攻击者可以传
    #     seed={"run_context": {"user_id": "受害者"}}
    # 覆盖掉服务端刚设好的身份，下游 `context.bind()` 会
    # `rs.set_context("受害者", ...)`，造成：
    #   · 以受害者的设置/API Key 发起真实请求（花他的钱）；
    #   · 产物写进受害者的输出目录；
    #   · 还能借 `images`/`character_reference_images` 等键影响流程。
    # 实测确认过这条链（产物确实落在了他人的 uid 目录下）。
    # 身份只能由服务端按**已认证用户**决定，任何"客户端可指定身份"的口子都要堵死。
    state["run_context"] = RunContext(
        user_id=user_id, project=project, session_id=session_id
    ).as_dict()
    t = threading.Thread(
        target=_run_stream,
        args=(state, session_id, user_id, user_input, project),
        daemon=True,
    )
    with _LOCK:
        _JOBS[session_id] = t
    t.start()


def project_from_checkpoint(session_id: str) -> str:
    """从图 checkpointer 的 State 里取回**工程名**。

    为什么从这里取，而不是让前端在续跑时再传一次：

      · 续跑是**新线程**，thread-local 不继承，所以不能靠 `capture()`；
      · `start_run` 已经把 `run_context`（含 `project`）写进了 State，
        而 `resume_run` 本来就要校验 checkpoint 存在——顺手读回来最稳，
        不依赖前端记得带、也不给"改个参数就把资产写到别人项目下"留口子。
      · 读不到就返回空串，由 `asset_paths.current_project()` 兜底成
        「未命名工程」——绝不能因为工程名缺失而让落盘失败。
    """
    try:
        snap = current_graph().get_state({"configurable": {"thread_id": session_id}})
        values = getattr(snap, "values", None) or {}
        ctx = values.get("run_context") or {}
        if isinstance(ctx, dict):
            return str(ctx.get("project") or "")
    except Exception:  # noqa: BLE001
        pass
    return ""


def has_checkpoint(session_id: str) -> bool:
    """该会话在图 checkpointer 里是否还有可续跑的状态。

    背景（01-usage §6.1 实测）：`MemorySaver` 是**进程内存**，服务重启后
    `sessions.status` 仍写着 `review`，但 checkpointer 里已无对应线程；
    此时 `Command(resume=...)` 会在**空 checkpoint 上从头重放整张图**——
    不报错、却把已完成的工作重做一遍（真实 provider 下等于重复烧钱）。

    用 `get_state()` 的 `created_at` 判定：不存在时该字段为 None。
    """
    try:
        snap = current_graph().get_state({"configurable": {"thread_id": session_id}})
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
    # 工程名从 checkpoint 的 State 里取回（见 `project_from_checkpoint` 的说明）。
    # 续跑是新线程，thread-local 不继承，所以不能靠 `capture()`；
    # 读不到就空串，由 `asset_paths` 兜底成「未命名工程」，绝不因此让落盘失败。
    project = project_from_checkpoint(session_id)

    # 关键：从"读会话状态"到"占用 _JOBS"必须**全程持同一把锁**。
    #
    # 否则两个并发 resume（双击「通过」、两个标签页、脚本并发调用）会：
    #   1. 双双读到 status == "review" 并通过校验；
    #   2. 各自起一个线程，用**同一个 thread_id** 跑同一张图；
    #   3. 下游节点（含真实生图/生视频）重复执行＝重复烧钱，
    #      LangGraph 的 MemorySaver 存储无锁，checkpoint 互相覆盖。
    # 把判据与占用放进临界区后，第二个请求会看到"已在运行"而被拒。
    with _LOCK:
        existing = store.get(session_id) or {}

        # 前置校验 1：会话必须存在
        if not existing:
            return False, f"会话不存在：{session_id}"

        # 前置校验 2：必须**停在确认点**才能续跑。
        #
        # 这就是防重复执行的**唯一权威判据**——不要再用"线程是否存活"来判：
        # 停在确认点时上一个 `_run_stream` 已经退出，但线程收尾与测试轮询之间
        # 有毫秒级窗口，用存活判据会把合法的连续 approve 误拒（实测踩到）。
        # 而 `status == "review"` 是持久的、语义明确的：只有人工确认点才算。
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

        # 到这一步才算占用成功：置状态 + 登记线程占位（真正 start 在锁外做，
        # 避免持锁执行耗时操作）。登记前先清掉已退出的旧线程条目。
        _join_job_locked(session_id)
        store.save(session_id, user_id, existing.get("user_input", ""), "running", "resume", None)
        command = Command(resume={"decision": decision, "feedback": feedback, "content": content})
        t = threading.Thread(
            target=_run_stream,
            # ⚠️ 续跑必须把工程名一起带上：`_run_stream` 是新线程，
            # thread-local 不继承；不带的话确认点之后产出的资产就会
            # 掉进「未命名工程」目录，同一个项目的资产被劈成两半。
            args=(command, session_id, user_id, existing.get("user_input", ""), project),
            daemon=True,
        )
        _JOBS[session_id] = t

    t.start()
    return True, "accepted"
