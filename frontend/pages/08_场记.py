"""场记（ContinuityChecker）— 独立职业页。

读取当前会话(session_state)里连续性报告，展示穿帮/一致性风险：
  - 按严重程度排序：🔴高 / ⚠️中 / 🟢低
  - 每个风险项：涉及镜头（可跳转）、风险描述、建议修正
功能：
  - 「重新检查」：调用后端 /api/crew/continuity 重新检查
没有数据时显示「暂无连续性报告」占位提示（或无风险）。
"""

import sys
import pathlib
from html import escape

import streamlit as st

FRONTEND = pathlib.Path(__file__).resolve().parent.parent
if str(FRONTEND) not in sys.path:
    sys.path.insert(0, str(FRONTEND))

from _api import backend_continuity, backend_available  # noqa: E402
from _org import DEPT_BY_KEY, role_dept_key  # noqa: E402
from _styles import handle_nav, inject_css, nav_go, render_nav  # noqa: E402

st.set_page_config(page_title="场记 · 小剧组", layout="wide")
inject_css()
handle_nav()
render_nav(current="create")

SEV_LABEL = {"high": ("🔴 高", "#e5484d"), "medium": ("⚠️ 中", "#ffb23d"), "low": ("🟢 低", "#46c060")}


def _load() -> list:
    payload = st.session_state.get("payload") or {}
    return payload.get("continuity_report") or []


st.markdown(
    '<div class="lc-eyebrow">导演分镜部 · 场记</div>'
    '<div class="lc-title" style="font-size:2.2rem;">场记 · 连续性检查</div>'
    '<p style="color:var(--s-muted);">检查相邻镜头间道具、位置、服装是否一致，防止穿帮。仅检查，不改动分镜。</p>',
    unsafe_allow_html=True,
)

sid = st.session_state.get("session_id")
token = st.session_state.get("_token")


def _check():
    if not sid:
        st.session_state["ct_msg"] = "请先创作一个会话。"
        return
    res = backend_continuity(sid, token)
    if res.get("status") == "ok":
        payload = dict(st.session_state.get("payload") or {})
        payload["continuity_report"] = res.get("continuity_report") or []
        st.session_state["payload"] = payload
        n = len(res.get("continuity_report") or [])
        st.session_state["ct_msg"] = f"已重新检查 · 发现 {n} 个风险"
        st.rerun()
    else:
        st.session_state["ct_msg"] = res.get("message", "检查失败")


st.button("🔄 重新检查", on_click=_check, key="ct_check", use_container_width=True, type="primary")
if st.session_state.get("ct_msg"):
    st.caption(st.session_state["ct_msg"])

report = _load()
if not report:
    st.info("暂无连续性报告：请先创作一个会话（分镜与节奏完成后会自动检查），或点击「重新检查」。")
    st.stop()

# 按严重度排序展示
order = {"high": 0, "medium": 1, "low": 2}
report_sorted = sorted(report, key=lambda r: order.get(r.get("severity", "low"), 2))
st.markdown('<div class="lc-divider"></div><div class="lc-label">穿帮风险（{n} 项）</div>'.format(n=len(report_sorted)),
            unsafe_allow_html=True)

for i, r in enumerate(report_sorted):
    sev = r.get("severity", "low")
    label, color = SEV_LABEL.get(sev, ("", "var(--s-muted)"))
    shots = r.get("shots") or []
    shots_str = " → ".join(str(s) for s in shots)
    st.markdown(
        f'<div class="lc-card" style="padding:14px 20px;margin-bottom:10px;'
        f'border-left:4px solid {color};">'
        f'<div style="display:flex;justify-content:space-between;">'
        f'<div class="lc-label">规则 {escape(str(r.get("rule","-")))} · {escape(label)}</div>'
        f"<span style='color:{color};font-size:12px;'>{escape(label)}</span></div>"
        f"<p style='margin:6px 0;'>{escape(str(r.get('risk','')))}</p>"
        f"<p style='font-size:13px;color:var(--s-muted);'>涉及镜头：{escape(shots_str)}</p>"
        f"<p style='font-size:13px;color:var(--s-muted);'>建议：{escape(str(r.get('suggestion','')))}</p></div>",
        unsafe_allow_html=True,
    )
    # 可跳转：跳转到分镜师页查看对应镜头
    if shots:
        st.button(f"去分镜页查看 {shots[0]}", key=f"ct_jump_{i}", on_click=nav_go,
                  args=("pages/06_分镜师.py",), use_container_width=True)

# 返回部门
st.markdown('<div style="height:16px"></div>', unsafe_allow_html=True)
st.button("← 返回导演分镜部", key="ct_back", on_click=nav_go,
          args=("pages/20_导演分镜部.py",), use_container_width=True)
