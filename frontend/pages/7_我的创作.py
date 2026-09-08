"""小剧组 — 我的创作（历史回顾）。"""

import pathlib
import sys
from datetime import datetime
from html import escape

import streamlit as st

FRONTEND = pathlib.Path(__file__).resolve().parent.parent
if str(FRONTEND) not in sys.path:
    sys.path.insert(0, str(FRONTEND))

from _api import backend_available, backend_history, backend_history_delete, backend_profile, backend_profile_update  # noqa: E402
from _styles import handle_nav, inject_css, logout_user, render_nav  # noqa: E402

st.set_page_config(page_title="我的创作 · 小剧组", layout="wide")
inject_css()
handle_nav()
render_nav(current="history")

st.markdown(
    '<div class="lc-eyebrow">我的创作</div>'
    '<div class="lc-title" style="font-size:2rem;">回顾你的漫剧</div>',
    unsafe_allow_html=True,
)

user_id = st.session_state.get("_user") or "local"
token = st.session_state.get("_token")

# ------------------------------------------------------------------
# 个人资料（显示名 / 头像）
# ------------------------------------------------------------------
if user_id:
    import base64

    prof = backend_profile(token).get("profile", {})
    display_name = prof.get("display_name") or user_id
    avatar = prof.get("avatar")

    def logout():
        logout_user()
        st.session_state["_nav_to"] = "pages/8_登录.py"

    # ---- 资料卡：头像 | 显示名（一左一右），下方小按钮（也是一左一右） ----
    c_av, c_name = st.columns([1, 2])
    with c_av:
        if avatar:
            try:
                st.image(avatar, width=112)
            except Exception:
                st.write("头像")
        else:
            st.markdown(f'<div class="lc-avatar-big lc-avatar-xl">{escape(user_id[0].upper())}</div>',
                        unsafe_allow_html=True)
    with c_name:
        st.markdown(f'<div class="lc-display-name" style="text-align:left;">{escape(display_name)}</div>',
                    unsafe_allow_html=True)
        st.markdown(f"<p style='font-size:14px;color:var(--s-muted);margin:2px 0 0 0;'>账号：{escape(user_id)}</p>",
                    unsafe_allow_html=True)

    # 下方小按钮（一左一右）
    b1, b2 = st.columns([1, 1])
    with b1:
        with st.popover("更换头像", use_container_width=True):
            st.caption("上传新图片更新头像")
            up = st.file_uploader("选择图片", type=["png", "jpg", "jpeg", "webp"], key="avatar_pop")
            if up is not None:
                data = up.getvalue()
                uri = f"data:{getattr(up, 'type', None) or 'image/png'};base64,{base64.b64encode(data).decode()}"
                st.image(uri, width=96)
                if st.button("确认更换", key="avatar_confirm", use_container_width=True):
                    backend_profile_update({"avatar": uri}, token)
                    st.session_state["prof_msg"] = "头像已更新"
                    st.rerun()
    with b2:
        with st.popover("改名", use_container_width=True):
            new_n = st.text_input("显示名", value=display_name, key="new_disp")
            if st.button("保存名字", key="save_disp", use_container_width=True):
                backend_profile_update({"display_name": st.session_state.get("new_disp") or user_id}, token)
                st.session_state["prof_msg"] = "名字已更新"
                st.rerun()

    st.button("登出", on_click=logout, key="logout", use_container_width=True)

    if st.session_state.get("prof_msg"):
        st.info(st.session_state["prof_msg"])
    st.markdown('<div style="height:12px"></div><div class="lc-divider"></div>', unsafe_allow_html=True)

if not backend_available():
    st.info("需要后端服务才能读取创作历史：`python scripts/run_server.py`。")
    st.stop()

items = backend_history(user_id, token)

if not items:
    st.info("你还没有创作记录，去「生成」创作一部漫剧吧。")
    st.stop()


# ------------------------------------------------------------------
# 穿帮/连续性风险面板（复用：场记 continuity_report + 节奏 rhythm_plan）
# ------------------------------------------------------------------
_SEV_LABEL = {"high": ("🔴 高", "#e5484d"), "medium": ("⚠️ 中", "#ffb23d"), "low": ("🟢 低", "#46c060")}


def _render_risk_panel(payload: dict) -> None:
    report = payload.get("continuity_report") or []
    rhythm = payload.get("rhythm_plan") or []
    scenes = payload.get("scene_reference_images") or {}
    if not report and not rhythm and not scenes:
        return
    with st.expander("🧩 穿帮/连续性风险", expanded=False):
        if report:
            order = {"high": 0, "medium": 1, "low": 2}
            report_sorted = sorted(report, key=lambda r: order.get(r.get("severity", "low"), 2))
            n_high = sum(1 for r in report_sorted if r.get("severity") == "high")
            st.caption(f"发现 {len(report_sorted)} 项风险（其中高 {n_high}）")
            for r in report_sorted:
                label, color = _SEV_LABEL.get(r.get("severity", "low"), ("", "var(--s-muted)"))
                shots = " → ".join(str(s) for s in (r.get("shots") or []))
                st.markdown(
                    f'<div style="border-left:3px solid {color};padding-left:10px;margin:6px 0;">'
                    f'<div style="font-size:12px;color:{color};">规则 {escape(str(r.get("rule","-")))} · {label} · <span style="color:var(--s-muted);">{escape(shots)}</span></div>'
                    f"<div style='font-size:13px;'>{escape(str(r.get('risk','')))}</div>"
                    f"<div style='font-size:12px;color:var(--s-muted);'>建议：{escape(str(r.get('suggestion','')))}</div></div>",
                    unsafe_allow_html=True,
                )
        else:
            st.caption("暂无穿帮风险。")
        if rhythm:
            total = sum(p.get("duration", 0) for p in rhythm)
            st.caption(f"⏱️ 节奏：{len(rhythm)} 镜头 · 总时长 {total:.1f}s")
        if scenes:
            st.caption(f"🏞️ 场景参考图：{len(scenes)} 个场景")


for it in items:
    payload = it.get("payload") or {}
    outline = payload.get("outline") or {}
    images = payload.get("images") or []
    shots = payload.get("storyboard") or []
    ts = it.get("created")
    tstr = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "—"
    sid = it.get("session_id")

    def _del(sid=sid):
        backend_history_delete(sid, st.session_state.get("_token"))

    st.button("删除", on_click=_del, key=f"del_{sid}", use_container_width=True)
    st.markdown(
        '<div class="lc-card">'
        f'<div class="lc-label">{escape(tstr)} · 状态 {escape(str(it.get("status","done")))}</div>'
        f"<h3>{escape(str(outline.get('title','未命名')))}</h3>"
        f"<p style='color:var(--s-muted);'>镜头 {len(shots)} 个 · 图片 {len(images)} 张</p>"
        f"<p style='color:var(--s-muted);'>{escape(str(it.get('user_input','') or outline.get('logline','')))}</p>"
        "</div>", unsafe_allow_html=True,
    )
    if images:
        cols = st.columns(min(4, max(1, len(images))))
        for i, im in enumerate(images):
            path = im.get("local_path") or im.get("image_url")
            with cols[i % len(cols)]:
                try:
                    st.image(path)
                except Exception:
                    pass

    # ---- 穿帮/连续性风险面板（场记） ----
    _render_risk_panel(payload)

