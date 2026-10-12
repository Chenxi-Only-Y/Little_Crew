"""后台线程必须自带运行时上下文。

⚠️ 为什么不设会出事：`runtime_settings` 的"当前用户"存在 threading.local 里，
**后台线程不继承请求上下文**。`_run_video` / `_do_retry` 都是新起线程干重活，
不显式 `set_context` 的话这一线程读到的是 `_global`，于是：
  · 用**全局**（而非该用户）的 API Key 与本地服务端点；
  · 产物按 `_global` 归类，落进错误的输出目录；
  · "按用户"的缓存键退化成同一个值（可能命中别人的缓存）。
实测出现过"产物落 `_global/`"这类症状。
"""

import threading

from app.services import runtime_settings as rs


def test_set_context_isolates_thread_and_can_be_reset():
    """线程内设身份 → 只影响本线程；复位后回到 `_global`。"""
    from app.main import _rs_reset_for_thread

    seen = {}

    def worker():
        rs.set_context("alice", "sess1")
        seen["during"] = (rs.user(), getattr(rs._local, "session_id", None))
        _rs_reset_for_thread()
        seen["after"] = rs.user()

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert seen["during"][0] == "alice", f"线程内身份不对：{seen['during']}"
    assert seen["during"][1] == "sess1"
    assert seen["after"] == "_global", f"复位后应为 _global，实际 {seen['after']!r}"
    # 主线程不受子线程影响
    assert rs.user() == "_global"


def test_video_and_retry_threads_set_context():
    """`_run_video` 与 `_do_retry` 必须设置上下文（读源码断言）。

    这类"新起线程忘了 set_context"的 bug 很隐蔽：功能表面正常，
    只是悄悄用了错的身份——所以用源码断言守住，而不是等出问题再查。
    """
    import re
    import pathlib

    # 直接读 main.py 源码：`_do_retry` 是嵌套函数，inspect.getsource 拿不到。
    src = pathlib.Path(__file__).resolve().parent.parent.joinpath(
        "app", "main.py").read_text(encoding="utf-8")
    lines = src.splitlines()

    def _body(fn: str) -> str:
        """按缩进取 `fn` 的函数体，直到出现同级或更外层的定义为止。"""
        start = None
        for i, ln in enumerate(lines):
            if ln.lstrip().startswith(f"def {fn}("):
                start = i
                break
        assert start is not None, f"找不到 {fn}"
        indent = len(lines[start]) - len(lines[start].lstrip())
        out = [lines[start]]
        for ln in lines[start + 1:]:
            if not ln.strip():
                out.append(ln)
                continue
            cur = len(ln) - len(ln.lstrip())
            if cur <= indent:
                break
            out.append(ln)
        return "\n".join(out)

    for fn in ("_run_video", "_do_retry"):
        seg = _body(fn)
        assert len(seg) > 200, f"{fn} 的函数体只取到 {len(seg)} 字符，取体逻辑不对"
        assert "set_context" in seg, (
            f"{fn} 没有 set_context —— 后台线程会用 _global 的设置"
        )
        assert "_rs_reset_for_thread" in seg, (
            f"{fn} 没有在 finally 里复位上下文"
        )

    # 视频线程的入口确实带着 user_id / session_id
    assert re.search(r"target=_run_video", src), "视频线程入口变了，请复核上下文传递"
