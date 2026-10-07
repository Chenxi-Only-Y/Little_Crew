"""守卫：**节点返回的每个状态键都必须在 CrewState 里声明**。

## 为什么需要这条测试

2026-10-07 的节点排查发现一批真 bug：`inpainter` / `motion_compositor` /
`video_assessor` / `distributor` / `voice_director` 返回的键（`video_report`、
`fixed_images`、各种 `*_note`）**根本不在 CrewState 里**。

而 LangGraph 对**未声明的 channel 是静默丢弃的**：

    langgraph/pregel/_algo.py:
        logger.warning("Task %s … wrote to unknown channel %s, ignoring it.")

后果实测：
  · `video_report` → 视频质检唯一产出全丢；
  · `*_note` → 节点设计成"优雅跳过并说明"，说明本身被丢掉；
  · `fixed_images` → 注册表声明的产物永远落不了地。

**这类 bug 不会让任何测试变红**：单测都是直接调服务函数、断言返回的 dict，
而那条路径**没有经过图**，所以键还在。只有把节点放进 `StateGraph(CrewState)`
跑一遍才会发现键被吃掉。

所以这条测试的做法是：**真的用 `StateGraph(CrewState)` 跑节点**，
然后断言"节点声称写了的键"确实出现在最终 state 里。
"""

from __future__ import annotations

import sys
import io

import pytest


def _run_node_through_graph(node_key: str, state: dict) -> dict:
    """把某个已注册的节点函数放进最小 StateGraph 跑一遍，返回最终 state。"""
    from langgraph.graph import END, StateGraph

    from app.core.state import CrewState
    from app.workflows import main_workflow as mw

    fn = getattr(mw, f"node_{node_key}", None)
    if fn is None:
        pytest.skip(f"没有 node_{node_key} 这个实现")

    g = StateGraph(CrewState)
    g.add_node("n", fn)
    g.set_entry_point("n")
    g.add_edge("n", END)
    return g.compile().invoke(state)


#: 每个节点一个**能触发它写出主要键**的最小输入。
#: 刻意用"跳过"路径（不需要真调模型/服务），因为跳过时才会写那些 note ——
#: 而 note 正是被丢掉的那批键。
CASES: dict[str, dict] = {
    # 没有分镜 → 走 skipped 分支，写 video_assessor_note
    "voice_director": {"storyboard": []},
    # 没有会话 → 写 motion_note
    "motion_compositor": {},
    # 没有视频 → 写 video_assessor_note
    "video_assessor": {},
    # 占位节点 → 写 inpainter_note + fixed_images
    "inpainter": {"images": [{"shot_id": "1"}]},
    # 没有成片 → 写 distributor_note
    "distributor": {},
}


@pytest.mark.parametrize("node_key,state", sorted(CASES.items()))
def test_node_return_keys_are_declared(node_key, state):
    """节点经真实图跑完后，它写的主要状态键必须**还在**。

    若某个键不在 `CrewState` 里，LangGraph 会丢掉它 —— 这条测试就会红。
    """
    from app.core.state import CrewState

    declared = set(CrewState.__annotations__)
    before = set(state)
    after = _run_node_through_graph(node_key, state)

    # 用节点的原始返回拿到"它声称写了什么"
    from app.workflows import main_workflow as mw

    fn = getattr(mw, f"node_{node_key}")
    raw = fn(dict(state)) or {}
    claimed = {k for k in raw if k not in ("current_phase",)}

    undeclared = {k for k in claimed if k not in declared}
    assert not undeclared, (
        f"节点 {node_key} 返回了未在 CrewState 声明的键 {sorted(undeclared)} —— "
        f"LangGraph 会**静默丢弃**它们。请在 app/core/state.py 补上字段。")

    dropped = {k for k in claimed if k not in after and k not in before}
    assert not dropped, (
        f"节点 {node_key} 声称写了 {sorted(dropped)}，但经图跑完后这些键不见了 —— "
        f"说明它们没有落点（要在 CrewState 里声明）。")


def test_the_specific_keys_that_were_dropped_are_now_declared():
    """钉死这次查出来的具体字段，防止有人"清理无用字段"时又删掉。"""
    from app.core.state import CrewState

    for field in ("video_report", "fixed_images", "motion_note",
                  "video_assessor_note", "inpainter_note", "distributor_note",
                  "voice_track_note", "video_regenerate"):
        assert field in CrewState.__annotations__, (
            f"{field} 必须留在 CrewState —— 删了它，对应节点的产出就会被静默丢弃")


def test_quality_report_and_consistency_report_are_dicts():
    """`_DICT_FIELDS` 失效时按类型清空 —— 这两个是 dict，不能清成 []。

    实测过：编辑剧本后 `quality_report` 被清成 `[]`（类型错误），
    而 `review_quality_node` 的 `if not state.get("quality_report")` 会把 `[]`
    当成"没有报告"→ **自动放行该确认点**。
    """
    from app.core.state import CrewState

    hints = CrewState.__annotations__
    assert hints["quality_report"] is dict
    assert hints["character_consistency_report"] is dict
