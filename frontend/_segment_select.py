"""小剧组 — 「AI 执行到哪一环」卡片式终点选择器（可复用）。

卡片单选（点卡片选中/取消），并带一条区间进度条显示"起点 → 终点"。
每个职业页都调用它，作为分段(可选起点/终点)的交互入口。

用法：
    from _segment_select import render_segment_select
    render_segment_select(role_key, start_key)
      自动以 start_key 为起点，列出该环节之后（含起点）的所有可分段环节作为可选终点。
    返回 end_stage(str|None)：选中的终点（None=到尾）
"""

import streamlit as st

# 13 环节完整流水线（与后端 main_workflow._STAGE_ORDER 对齐，仅生成型可作终点）
# 标签取简短名，保证卡片单行不换行、大小一致。
# 说明：consistency_auditor(美术总监) 是工作流内的自动审查环节，不作为用户可选终点。
FULL_ROLES = [
    ("writer", "作家"), ("critics", "文学策划"), ("screenwriter", "编剧"),
    ("character_designer", "角色设计"), ("scene_designer", "场景设计"),
    ("storyboarder", "分镜师"),
    ("rhythm_director", "剪辑指导"), ("continuity_checker", "场记"),
    ("director", "技术指导"), ("photographer", "摄影师"),
    ("quality_assessor", "质检总监"),
]
# 汇总/分发节点不作为分段终点（始终运行）
_END_EXCLUDE = {"aggregator", "distributor"}


def render_segment_select(role_key: str, start_key: str) -> str | None:
    """渲染卡片式终点选择器，返回选中的 end_stage（None=到尾）。

    只列出 start_key 之后（含起点）的可分段环节作为可选终点。
    """
    keys = [k for k, _ in FULL_ROLES if k not in _END_EXCLUDE]
    if start_key in keys:
        start_idx = keys.index(start_key)
    else:
        start_idx = 0
    stage_keys = keys[start_idx:]

    key = f"seg_{role_key}"
    st.session_state.setdefault(key, None)
    selected = st.session_state[key]

    st.markdown('<div class="lc-label">AI 执行到哪一环（点卡片选择 · 默认到尾）</div>', unsafe_allow_html=True)
    # 1×N 单行排版：固定高度卡片（CSS 强制等高），文字换行也不改变高度
    ncols = max(1, len(stage_keys))
    cols = st.columns(ncols)
    for i, k in enumerate(stage_keys):
        active = (selected == k)
        with cols[i % ncols]:
            if st.button(_label(k), key=f"{key}_{k}",
                         type="primary" if active else "secondary", use_container_width=True):
                st.session_state[key] = None if active else k
                st.rerun()

    # 区间进度条
    m = len(stage_keys)
    pos = stage_keys.index(selected) if (selected and selected in stage_keys) else (m - 1)
    fill_w = (pos + 1) / m * 100
    st.markdown(
        f'<div class="lc-smooth-track">'
        f'<div class="lc-smooth-fill" style="left:0%;width:{fill_w:.1f}%"></div>'
        f"</div>"
        f"<p style='color:var(--s-muted);font-size:12px;'>{_label(start_key)} → "
        f"{(_label(selected) if selected else '尾')}</p>",
        unsafe_allow_html=True,
    )
    return selected


def _label(k: str) -> str:
    return dict(FULL_ROLES).get(k, k)
