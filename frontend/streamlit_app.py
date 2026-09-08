"""小剧组 (Little Crew) — 主页（按部门划分的组织入口）。"""

import streamlit as st

import _api
from _org import DEPARTMENTS
from _styles import handle_nav, inject_css, nav_go, render_nav

st.set_page_config(page_title="小剧组 Little Crew", layout="wide")
inject_css()
handle_nav()
render_nav(current="home")

backend_ok = _api.backend_available()
st.caption("数据源：" + ("API（支持人工确认点）" if backend_ok else "本地回退（自动放行）"))

st.markdown(
    '<div class="lc-title">小剧组&nbsp;·&nbsp;Little&nbsp;Crew</div>'
    '<div class="lc-sub">一句话，你就是一整个剧组</div>'
    '<div class="lc-divider"></div>',
    unsafe_allow_html=True,
)

# 快捷入口（放在部门标题之前）：一句话全流程 / 我的创作 / 设置
c1, c2, c3 = st.columns(3)
with c1:
    st.markdown(
        '<div class="lc-home-card"><div class="lc-label">一句话全流程</div>'
        "<h3>从创意直达成片</h3>"
        "<p>由 AI 依次扮演五个部门，一条龙出漫剧。</p></div>", unsafe_allow_html=True,
    )
    st.button("开始创作 →", on_click=nav_go, args=("pages/01_策划总监.py",), use_container_width=True,
              type="primary", key="home_full")
with c2:
    st.markdown(
        '<div class="lc-home-card"><div class="lc-label">我的创作</div>'
        "<h3>回顾作品</h3>"
        "<p>查看你生成过的漫剧、剧本与画面。</p></div>", unsafe_allow_html=True,
    )
    st.button("查看 →", on_click=nav_go, args=("pages/7_我的创作.py",), use_container_width=True,
              key="home_history")
with c3:
    st.markdown(
        '<div class="lc-home-card"><div class="lc-label">设置</div>'
        "<h3>模型配置</h3>"
        "<p>切换各职业模型与 API Key。</p></div>", unsafe_allow_html=True,
    )
    st.button("进入", on_click=nav_go, args=("pages/9_设置.py",), use_container_width=True,
              key="home_settings")

st.markdown('<div style="height:24px"></div>', unsafe_allow_html=True)

st.markdown(
    '<div class="lc-eyebrow">五部门 · 十三职位</div>'
    '<p style="color:var(--s-muted);">点进部门 → 选择职业 → 从那一环开始，之后的环节由 AI 完成。</p>',
    unsafe_allow_html=True,
)

# 按部门划分的卡片：进部门页
cols = st.columns(3)
for i, dept in enumerate(DEPARTMENTS):
    with cols[i % len(cols)]:
        n_roles = len(dept["roles"])
        ready = sum(1 for r in dept["roles"] if r.get("ready"))
        st.markdown(
            f'<div class="lc-home-card" style="min-height:150px;">'
            f'<div class="lc-label">{dept["name"]}</div>'
            f"<h3 style='font-size:1rem;'>{n_roles} 个职位</h3>"
            f"<p style='font-size:13px;color:var(--s-muted);'>{dept['desc']}</p></div>",
            unsafe_allow_html=True,
        )
        st.button(f"进「{dept['name']}」", key=f"home_dept_{dept['key']}",
                  on_click=nav_go, args=(f"pages/20_{dept['name']}.py",), use_container_width=True)
