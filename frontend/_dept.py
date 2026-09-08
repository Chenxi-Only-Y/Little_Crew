"""小剧组 — 部门页渲染器（每个部门一个入口，选职业进入对应职业页）。"""

import streamlit as st

from _org import DEPARTMENTS, DEPT_BY_KEY
from _styles import handle_nav, inject_css, nav_go, render_nav


def render_dept(dept_key: str) -> None:
    """渲染某部门的职业选择页。"""
    dept = DEPT_BY_KEY.get(dept_key)
    if not dept:
        st.error("部门不存在。")
        return

    st.set_page_config(page_title=f"小剧组 · {dept['name']}", layout="wide")
    inject_css()
    handle_nav()
    render_nav(current="create")

    st.markdown(
        f'<div class="lc-eyebrow">组织架构 · {dept["name"]}</div>'
        f'<div class="lc-title" style="font-size:2.2rem;">{dept["name"]}</div>'
        f"<p style='color:var(--s-muted);'>{dept['desc']}</p>"
        f'<div class="lc-divider"></div>',
        unsafe_allow_html=True,
    )

    # 本部门职位卡片（每个职位进对应职业页）
    roles = dept.get("roles", [])
    st.markdown(f'<div class="lc-eyebrow">本部门 · {len(roles)} 个职位</div>', unsafe_allow_html=True)
    cols = st.columns(3)
    for i, r in enumerate(roles):
        with cols[i % len(cols)]:
            st.markdown(
                f'<div class="lc-home-card" style="min-height:120px;">'
                f'<div class="lc-label">{r["title"]}</div>'
                f"<p style='font-size:13px;'>{r['duty']}</p>"
                f"<p style='font-size:12px;color:var(--s-muted);'>Agent：{r['agent']}</p></div>",
                unsafe_allow_html=True,
            )
            # 已实现 -> 进入职业页；未实现 -> 进入预留占位页
            st.button(r["title"], key=f"dept_{dept_key}_{r['key']}",
                      on_click=nav_go, args=(r["page"],), use_container_width=True)

    # 其它部门快捷入口
    st.markdown(f'<div class="lc-divider"></div><div class="lc-label">其它部门</div>', unsafe_allow_html=True)
    others = [d for d in DEPARTMENTS if d["key"] != dept_key]
    cols = st.columns(len(others))
    for i, d in enumerate(others):
        with cols[i % len(cols)]:
            st.button(f'{d["name"]}', key=f"dept_{dept_key}_to_{d['key']}",
                      on_click=nav_go, args=(f"pages/20_{d['name']}.py",), use_container_width=True)

    st.button("← 主页", key=f"dept_{dept_key}_home", on_click=nav_go, args=("streamlit_app.py",),
              use_container_width=True)
