"""`graph_manager` 的测试 —— A2「运行时换图」的**安全保证**。

## 这个模块要守住的最重要一条

**换图不能丢掉在途会话的断点。**

原先 `compile_graph()` 每次 `MemorySaver()` 都新建一个，于是"换图"必然
"换记忆"：正在确认点等用户点「通过」的会话会突然变成"检查点丢失"。
这个模块的全部意义就是把这件事隔开 —— 图可以换，**记忆不能换**。

所以这里的核心测试是：跑一个会话到确认点（停在 review）→ 换一张新图 →
**确认那个会话的断点还在、还能续跑**。

其余测试覆盖：版本递增、历史可追溯、回滚、惰性安装、以及
"`task_runner` 拿到的确实是当前那张图"（而不是导入时绑定的第一张）。
"""

from __future__ import annotations

import pytest


# ------------------------------------------------------------------
# 基本语义
# ------------------------------------------------------------------

def test_shared_checkpointer_is_stable_across_builds():
    """共享 checkpointer 必须**是同一个对象**，否则换图就丢记忆。"""
    from app.workflows.graph_manager import MANAGER

    cp1 = MANAGER.checkpointer()
    cp2 = MANAGER.checkpointer()
    assert cp1 is cp2, "两次取到不同 checkpointer —— 换图会丢在途会话"

    # compile_graph() 默认也要用这一个（不是自己新建）
    from app.workflows.main_workflow import compile_graph

    assert compile_graph().checkpointer is cp1, (
        "compile_graph() 自己新建了 checkpointer —— 换图会丢在途会话"
    )


def test_swap_bumps_version_and_keeps_history():
    from app.workflows.graph_manager import GraphManager
    from app.workflows.main_workflow import build_graph

    m = GraphManager()
    cp = m.checkpointer()
    g1 = build_graph().compile(checkpointer=cp)
    m.install_initial(g1)
    assert m.version == 1

    # 第一次 install 之后不允许再 install（会丢版本历史）
    with pytest.raises(RuntimeError):
        m.install_initial(g1)

    g2 = build_graph().compile(checkpointer=cp)
    v = m.swap(g2, reason="测试换图", source="unit-test", proposal={"k": "v"})
    assert v == 2
    assert m.current_graph() is g2

    hist = m.history()
    assert [h["version"] for h in hist] == [1, 2]
    assert hist[-1]["reason"] == "测试换图"
    assert hist[-1]["proposal"] == {"k": "v"}
    assert hist[-1]["has_previous"] is True

    # 换图**不能**换 checkpointer
    assert m.current_graph().checkpointer is cp


def test_rollback_returns_to_previous_graph():
    from app.workflows.graph_manager import GraphManager
    from app.workflows.main_workflow import build_graph

    m = GraphManager()
    cp = m.checkpointer()
    g1 = build_graph().compile(checkpointer=cp)
    m.install_initial(g1)
    g2 = build_graph().compile(checkpointer=cp)
    m.swap(g2, reason="第二版")

    assert m.can_rollback() is True
    m.rollback(reason="回滚测试")
    assert m.current_graph() is g1, "回滚没有回到上一张图"
    assert m.history()[-1]["source"] == "rollback"


def test_rollback_without_history_raises():
    from app.workflows.graph_manager import GraphManager

    m = GraphManager()
    with pytest.raises(RuntimeError):
        m.rollback()


def test_swap_rejects_none():
    from app.workflows.graph_manager import GraphManager

    m = GraphManager()
    with pytest.raises(ValueError):
        m.swap(None, reason="空图")


def test_snapshot_shape():
    from app.workflows.graph_manager import MANAGER

    snap = MANAGER.snapshot()
    for k in ("version", "nodes", "checkpointer", "can_rollback", "history"):
        assert k in snap, f"snapshot 缺 {k}"
    assert isinstance(snap["history"], list)
    # 新的在前
    if len(snap["history"]) >= 2:
        assert snap["history"][0]["version"] > snap["history"][1]["version"]


# ------------------------------------------------------------------
# 惰性安装（不靠导入副作用）
# ------------------------------------------------------------------

def test_lazy_install_works_in_a_fresh_process():
    """**没有任何代码 import main_workflow 时也要能拿到图。**

    这是一个真踩过的坑：初始图原本靠 `main_workflow` 模块底部的导入副作用装进来，
    而 `task_runner` 现在只导入 `graph_manager`（不再导入 main_workflow），
    注册表模块又用了 `TYPE_CHECKING` 以避免循环导入 —— 三者凑齐，
    `current_graph()` 就抛"图还没装入"（`test_checkpoint_resume` 当场红了）。

    所以在**干净子进程**里验证惰性安装：只导入 graph_manager，直接取图。
    """
    import subprocess
    import os
    import sys  # 函数内导入：模块级导入会破坏 pytest 的输出捕获

    py = os.path.join(os.path.dirname(sys.executable), "python.exe")
    if not os.path.exists(py):
        pytest.skip("找不到同环境的 python")
    code = (
        "import sys; sys.path.insert(0, r'D:\\AI\\ds.6')\n"
        "from app.workflows.graph_manager import current_graph\n"
        "g = current_graph()\n"
        "print('NODES', len(g.nodes))\n"
    )
    out = subprocess.run([py, "-c", code], capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    assert out.returncode == 0, f"惰性安装失败：{out.stderr[-500:]}"
    assert "NODES" in out.stdout, out.stdout


# ------------------------------------------------------------------
# 最核心的一条：换图不丢在途会话
# ------------------------------------------------------------------

def test_swap_does_not_lose_in_flight_session(monkeypatch):
    """跑一个会话停在确认点 → 换图 → **断点仍在、仍可续跑**。

    这就是"换图 = 换 checkpointer"那个坑的回归测试。

    ⚠️ 必须**显式**把 hitl 打开：有别的测试把 `settings.hitl` 直接改成了 False
    且没还原（`test_character_consistency_e2e.py` 用的是裸赋值、不是 monkeypatch），
    于是这条测试在全套里跑到时会"一路跑完"，`status` 变成 `done` 而不是 `review`
    —— 单独跑却通过。这类"单跑绿、全套红"极难查，所以这里自己把前提设死。
    """
    import time

    from app.core.config import settings
    from app.services import task_runner as tr
    from app.workflows.graph_manager import MANAGER
    from app.workflows.main_workflow import build_graph

    monkeypatch.setattr(settings, "mock_llm", True)
    monkeypatch.setattr(settings, "mock_image", True)
    monkeypatch.setattr(settings, "mock_quality", True)
    monkeypatch.setattr(settings, "mock_critics", True)
    monkeypatch.setattr(settings, "hitl", True)   # ← 必须停在确认点

    sid = f"t_swap_{int(time.time() * 1000)}"
    tr.start_run(sid, "换图不丢会话", "tester")

    # 等它停在确认点
    deadline = time.time() + 25
    status = None
    while time.time() < deadline:
        row = tr.get_session_store().get(sid) or {}
        status = row.get("status")
        if status in ("review", "done", "error"):
            break
        time.sleep(0.1)
    assert status == "review", f"没停在确认点（status={status}）"
    assert tr.has_checkpoint(sid) is True

    # --- 换一张**新图**（模拟 A2）---
    cp = MANAGER.checkpointer()
    new_graph = build_graph().compile(checkpointer=cp)
    old = MANAGER.current_graph()
    try:
        MANAGER.swap(new_graph, reason="测试：在途会话期间换图",
                     source="unit-test:in-flight")
        # 断点必须在（共享 checkpointer 的全部意义）
        assert tr.has_checkpoint(sid) is True, (
            "换图后断点丢了 —— 说明 checkpointer 没共享"
        )
        # 而且必须还能续跑
        started, reason = tr.resume_run(sid, "tester", "approve", {})
        assert started is True, f"换图后无法续跑：{reason}"
    finally:
        # 会话已推进，但图也要还原，避免影响其它测试
        monkeypatch.setattr(MANAGER, "_graph", old)
        monkeypatch.setattr(MANAGER, "_version", MANAGER.version - 1)
