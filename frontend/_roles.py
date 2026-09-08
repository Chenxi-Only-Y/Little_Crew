"""小剧组 — 六环节定义 + 角色切换器（每个职业页顶部/主页共用）。"""

import streamlit as st

from _styles import nav_go, render_nav, handle_nav, inject_css  # noqa: F401

# 六环节（role_key 对应 start_stage 的环节名）
ROLES = [
    ("作家", "一句话 → 大纲", "writer", "pages/01_策划总监.py"),
    ("编剧", "大纲 → 剧本", "screenwriter", "pages/03_编剧.py"),
    ("分镜师", "剧本 → 镜头", "storyboarder", "pages/06_分镜师.py"),
    ("技术指导", "分镜 → 提示词", "director", "pages/09_技术指导.py"),
    ("摄影师", "提示词 → 画面", "photographer", "pages/10_摄影师.py"),
    ("剪辑师", "画面 → 成片", "editor", "pages/12_剪辑师.py"),
]
ROLE_KEYS = [r[2] for r in ROLES]


def render_role_switcher(current_key: str = "") -> None:
    """渲染六环节卡片切换器：点击跳转到对应职业页，当前环高亮。"""
    st.markdown('<div class="lc-divider" style="margin:4px 0 12px 0;"></div>', unsafe_allow_html=True)
    cols = st.columns(len(ROLES))
    for i, (role, what, key, path) in enumerate(ROLES):
        cur = (key == current_key)
        with cols[i]:
            st.markdown(
                f'<div class="lc-home-card" style="min-height:96px;padding:14px;">'
                f'<div class="lc-label" style="color:{("#c2ef4e" if cur else "var(--s-violet-mid)")}">'
                f"{i+1} · {role}{'（当前）' if cur else ''}</div>"
                f"<p style='font-size:12px;'>{what}</p></div>",
                unsafe_allow_html=True,
            )
            st.button(role, on_click=nav_go, args=(path,), key=f"sw_{key}",
                      use_container_width=True, type="primary" if cur else "secondary")
