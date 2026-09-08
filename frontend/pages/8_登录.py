"""小剧组 — 登录/注册页（标签页区分）。"""

import pathlib
import sys
from html import escape

import streamlit as st

FRONTEND = pathlib.Path(__file__).resolve().parent.parent
if str(FRONTEND) not in sys.path:
    sys.path.insert(0, str(FRONTEND))

import _api  # noqa: E402
from _styles import handle_nav, inject_css, logout_user, render_nav  # noqa: E402

st.set_page_config(page_title="登录 · 小剧组", layout="wide")
inject_css()
handle_nav(allow_anon=True)
render_nav(current="login")


def _do_login():
    u = (st.session_state.get("login_user") or "").strip()
    p = st.session_state.get("login_pwd") or ""
    res = _api.auth_login(u, p)
    if res.get("status") == "ok":
        _persist_token(res.get("token"), res.get("username"))
        st.session_state["_token"] = res.get("token")
        st.session_state["_user"] = res.get("username")
        st.session_state["auth_msg"] = ""
        st.session_state["_nav_to"] = "streamlit_app.py"
        # 清掉密码等敏感 widget 状态，避免长期留存
        for k in ("login_pwd", "login_user", "reg_pwd", "reg_pwd2"):
            st.session_state.pop(k, None)
    else:
        st.session_state["auth_msg"] = res.get("message", "登录失败")


def _do_register():
    u = (st.session_state.get("reg_user") or "").strip()
    p = st.session_state.get("reg_pwd") or ""
    c = st.session_state.get("reg_pwd2") or ""
    if p != c:
        st.session_state["auth_msg"] = "两次密码不一致"
        return
    res = _api.auth_register(u, p)
    if res.get("status") == "ok":
        _persist_token(res.get("token"), res.get("username"))
        st.session_state["_token"] = res.get("token")
        st.session_state["_user"] = res.get("username")
        st.session_state["auth_msg"] = ""
        st.session_state["_nav_to"] = "streamlit_app.py"
        for k in ("reg_user", "reg_pwd", "reg_pwd2", "login_pwd"):
            st.session_state.pop(k, None)
    else:
        st.session_state["auth_msg"] = res.get("message", "注册失败")


def _persist_token(token: str, username: str) -> None:
    """把 token/用户名写进浏览器 cookie，刷新/重开后由 _styles._restore_token_from_cookie 读回。"""
    try:
        from _styles import _get_cookies

        cookies = _get_cookies()
        cookies["_token"] = token or ""
        cookies["_user"] = username or ""
        cookies.save()
    except Exception:
        pass


def _logout():
    logout_user()


st.session_state.setdefault("_token", None)
st.session_state.setdefault("_user", None)
st.session_state.setdefault("auth_msg", "")
st.session_state.setdefault("login_user", "")
st.session_state.setdefault("login_pwd", "")
st.session_state.setdefault("reg_user", "")
st.session_state.setdefault("reg_pwd", "")
st.session_state.setdefault("reg_pwd2", "")

st.markdown(
    '<div class="lc-eyebrow">账户</div>'
    '<div class="lc-title" style="font-size:2rem;">登录 / 注册</div>',
    unsafe_allow_html=True,
)

if st.session_state.get("_user"):
    st.markdown(
        '<div class="lc-card"><div class="lc-label">当前用户</div>'
        f"<h3>{escape(str(st.session_state['_user']))}</h3>"
        "<p style='color:var(--s-muted);'>欢迎回来，去「生成」创作你的漫剧吧。</p></div>",
        unsafe_allow_html=True,
    )
    st.button("登出", on_click=_logout, key="logout")
else:
    tab_login, tab_register = st.tabs(["登录", "注册"])
    with tab_login:
        st.text_input("用户名", key="login_user")
        st.text_input("密码", key="login_pwd", type="password")
        st.button("登录", on_click=_do_login, key="do_login", use_container_width=True)
    with tab_register:
        st.text_input("用户名", key="reg_user", placeholder="用于区分你的创作记录")
        st.text_input("密码", key="reg_pwd", type="password", placeholder="至少 4 位")
        st.text_input("确认密码", key="reg_pwd2", type="password")
        st.button("注册", on_click=_do_register, key="do_register", use_container_width=True)
    if st.session_state.get("auth_msg"):
        st.info(st.session_state["auth_msg"])
