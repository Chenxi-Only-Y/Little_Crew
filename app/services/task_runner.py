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

    set_context(user_id, session_id)  # 供 llm/image/视频 按该用户+会话读取与归类输出
    store = get_session_store()
    config = {"configurable": {"thread_id": session_id}}
    try:
        interrupted = False
        result = None
        phase = "start"
        review = None
        for chunk in app_workflow.stream(job_input, config=config, stream_mode="updates"):
            if _CANCEL.get(session_id):
                # 中途被打断：保存 cancelled 并退出
                store.save(session_id, user_id, user_input, "cancelled", phase or "cancelled", result or {})
                with _LOCK:
                    _CANCEL.pop(session_id, None)
                return
            for node, data in chunk.items():
                if node == "__interrupt__":
                    interrupted = True
                    review = _extract_review(data)
                    store.save(session_id, user_id, user_input, "review", "review", review)
                elif isinstance(data, dict):
                    phase = data.get("current_phase", node)
                    # 实时累积「AI 思考过程」
                    tl = data.get("thinking_log")
                    if tl:
                        with _LOCK:
                            _THINKING.setdefault(session_id, []).extend(tl)
                    store.save(session_id, user_id, user_input, "running", phase, None)
                    if "result" in data:
                        result = data["result"]
            # mock 模式：每环节稍作停顿，让前端可见地看到进度推进
            if settings.mock_llm or settings.mock_image:
                time.sleep(0.4)
        if _CANCEL.get(session_id):
            store.save(session_id, user_id, user_input, "cancelled", phase or "cancelled", result or {})
            with _LOCK:
                _CANCEL.pop(session_id, None)
        elif not interrupted:
            store.save(session_id, user_id, user_input, "done", phase or "done", result or {})
    except Exception as exc:  # noqa: BLE001
        store.save(session_id, user_id, user_input, "error", "error", {"error": redact(str(exc))})


def start_run(session_id: str, user_input: str, user_id: str = "anon", seed: dict | None = None) -> None:
    """异步启动一次创作（可带 seed：已有中间产物则跳过对应环节）。"""
    store = get_session_store()
    with _LOCK:
        _THINKING.pop(session_id, None)  # 新会话清空思考日志
        _CANCEL.pop(session_id, None)    # 清空取消标记
    store.save(session_id, user_id, user_input, "running", "start", None)
    state = {"user_input": user_input, "session_id": session_id}
    if seed:
        state.update(seed)
    t = threading.Thread(target=_run_stream, args=(state, session_id, user_id, user_input), daemon=True)
    with _LOCK:
        _JOBS[session_id] = t
    t.start()


def resume_run(session_id: str, user_id: str, decision: str, feedback: dict, content=None) -> None:
    """对暂停的确认点/人工接管段续跑（approve / redo+feedback / 提供 content）。"""
    store = get_session_store()
    existing = store.get(session_id) or {}
    store.save(session_id, user_id, existing.get("user_input", ""), "running", "resume", None)
    command = Command(resume={"decision": decision, "feedback": feedback, "content": content})
    t = threading.Thread(target=_run_stream, args=(command, session_id, user_id, existing.get("user_input", "")), daemon=True)
    with _LOCK:
        _JOBS[session_id] = t
    t.start()
