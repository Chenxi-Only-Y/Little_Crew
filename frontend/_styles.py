"""小剧组 — 共享 Sentry 设计系统（Streamlit CSS）。"""

import streamlit as st
from streamlit_cookies_manager import CookieManager

SENTRY_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Rubik:wght@400;500;600;700&display=swap');

:root{
  --s-primary:#150f23;         /* midnight violet */
  --s-ink-plum:#1f1633;        /* ink-deep / dark canvas */
  --s-lime:#c2ef4e;            /* electric lime */
  --s-pink:#fa7faa;            /* hot pink */
  --s-violet:#6a5fc1;
  --s-violet-deep:#422082;
  --s-violet-mid:#79628c;
  --s-night:#150f23;
  --s-hairline:#362d59;        /* hairline-violet */
  --s-hairline-cool:#cfcfdb;
  --s-white:#ffffff;
  --s-muted:rgba(255,255,255,0.72);
  --s-faint:rgba(255,255,255,0.18);
}

.stApp{
  background:
    radial-gradient(circle at 15% 15%, rgba(255,255,255,0.03) 0, transparent 32%),
    radial-gradient(circle at 85% 20%, rgba(194,239,78,0.05) 0, transparent 30%),
    radial-gradient(circle at 50% 90%, rgba(106,95,193,0.09) 0, transparent 38%),
    var(--s-primary);
  font-family:'Rubik', -apple-system, system-ui, 'Segoe UI', Helvetica, Arial, sans-serif;
  color:var(--s-white);
}

/* starfield */
.stApp::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:0;
  background-image:
    radial-gradient(1px 1px at 12% 20%, rgba(255,255,255,0.32) 0, transparent 100%),
    radial-gradient(1px 1px at 32% 64%, rgba(255,255,255,0.25) 0, transparent 100%),
    radial-gradient(1.5px 1.5px at 58% 18%, rgba(255,255,255,0.28) 0, transparent 100%),
    radial-gradient(1px 1px at 74% 48%, rgba(255,255,255,0.20) 0, transparent 100%),
    radial-gradient(1.5px 1.5px at 88% 76%, rgba(255,255,255,0.24) 0, transparent 100%),
    radial-gradient(1px 1px at 44% 88%, rgba(255,255,255,0.22) 0, transparent 100%);
}

.block-container{padding-top:3.5rem;max-width:1152px;}

/* 隐藏 Streamlit 自带页头/工具栏/菜单/页脚 */
[data-testid="stHeader"]{display:none!important;}
[data-testid="stToolbar"]{visibility:hidden;}
#MainMenu{visibility:hidden;}
footer{visibility:hidden;}

/* 隐藏侧边菜单（改用主页小卡片导航） */
[data-testid="stSidebar"]{display:none!important;}
[data-testid="stSidebarCollapsedControl"]{display:none!important;}
[data-testid="stSidebarNav"]{display:none!important;}

/* 侧边菜单（Sentry 风格） */
[data-testid="stSidebar"]{background:var(--s-primary);}
[data-testid="stSidebarNav"] a{color:var(--s-white)!important;font-family:'Rubik',sans-serif;font-size:15px;}
[data-testid="stSidebarNav"] a:hover{color:var(--s-lime)!important;}
[data-testid="stSidebarNav"] a[aria-current="page"]{color:var(--s-lime)!important;}
[data-testid="stSidebar"] .stMarkdown p{color:var(--s-muted);}

/* typography */
h1,h2,h3{font-family:'Rubik',sans-serif;color:var(--s-white);font-weight:500;letter-spacing:0;}
.lc-eyebrow{font-size:15px;font-weight:500;letter-spacing:0.2px;text-transform:uppercase;color:var(--s-lime);margin-bottom:6px;}
.lc-title{font-size:3.2rem;line-height:1.15;font-weight:700;margin-bottom:8px;}
.lc-sub{color:var(--s-muted);font-size:16px;line-height:2.0;max-width:760px;}
.lc-chip{background:var(--s-lime);color:var(--s-ink-plum);border-radius:4px;padding:0 12px;font-weight:700;display:inline-block;line-height:1.3;}

/* buttons: button-inverted（强制覆盖 Streamlit emotion 样式）*/
button[data-testid^="stBaseButton"], [data-testid="stBaseButton"], [data-testid="stButton"] button, .stButton button, .stButton > button {
  background:var(--s-white) !important;
  color:var(--s-ink-plum) !important;
  border:1px solid var(--s-white) !important;
  border-radius:8px !important;
  font-family:'Rubik',sans-serif !important;
  font-size:14px !important;
  font-weight:700 !important;
  letter-spacing:0.2px !important;
  text-transform:uppercase !important;
  padding:11px 14px !important;
  min-height:40px !important;
  box-shadow:0 2px 8px rgba(0,0,0,0.08) !important;
}
[data-testid="stBaseButton"] { background:var(--s-white) !important; }
.stButton>button:hover{background:var(--s-white);}
.stButton>button:active{background:#efefef;color:#1a1a1a;}

/* inputs */
[data-testid="stTextArea"] textarea,.stTextInput input,[data-testid="stTextInput"] input{
  background:#ffffff;color:var(--s-ink-plum);border:1px solid var(--s-hairline-cool);border-radius:6px;
  font-family:'Rubik',sans-serif;font-size:16px;font-weight:500;padding:8px 12px;}

/* cards */
.lc-card{background:var(--s-ink-plum);border:1px solid var(--s-hairline);border-radius:18px;padding:24px;
  margin-bottom:18px;color:var(--s-white);}
.lc-card h3{margin-top:0;}
.lc-card .lc-label{font-size:15px;font-weight:500;letter-spacing:0.2px;text-transform:uppercase;color:var(--s-lime);margin-bottom:8px;}
.lc-card pre{background:var(--s-night);border-radius:8px;padding:16px;color:var(--s-white);
  font-family:'Monaco',Menlo,'Ubuntu Mono',monospace;font-size:14px;overflow:auto;}

/* home cards */
.lc-home-card{background:var(--s-ink-plum);border:1px solid var(--s-hairline);border-radius:18px;padding:20px;
  min-height:180px;display:flex;flex-direction:column;justify-content:space-between;color:var(--s-white);}
.lc-home-card .lc-label{font-size:13px;font-weight:600;letter-spacing:0.25px;text-transform:uppercase;color:var(--s-violet-mid);margin-bottom:10px;}
.lc-home-card h3{margin:0 0 6px 0;}
.lc-home-card p{color:var(--s-muted);font-size:14px;line-height:1.6;margin:0;}

/* 分段卡片选择器：统一尺寸（固定高度/居中） */
.lc-seg-card{background:var(--s-ink-plum);border:1px solid var(--s-hairline);border-radius:14px;
  margin-bottom:10px;display:flex;align-items:center;justify-content:center;height:56px;}
.lc-seg-inner{font-size:14px;font-weight:600;text-align:center;padding:0 8px;white-space:normal;}
/* 统一按钮高度（含分段 1×12 卡片），文字自动换行也不改变卡片高度 */
.stButton>button[data-testid^="stBaseButton"]{min-height:52px !important;height:52px !important;
  display:flex;align-items:center;justify-content:center;padding:4px 6px !important;
  white-space:normal;line-height:1.2;}

/* 字母头像 */
.lc-avatar-big{width:72px;height:72px;border-radius:50%;background:var(--s-violet-mid);color:var(--s-white);
  display:flex;align-items:center;justify-content:center;font-size:30px;font-weight:700;margin-bottom:8px;}
.lc-avatar-xl{width:112px;height:112px;font-size:46px;}

/* 最大显示名 */
.lc-display-name{font-size:36px;font-weight:800;color:var(--s-white);text-align:center;
  line-height:1.25;margin:10px 0 4px 0;}

/* 区间条（平滑进度条：填充部分=选中区间，带丝滑流光） */
.lc-smooth-track{position:relative;height:10px;border-radius:9999px;background:var(--s-ink-plum);
  border:1px solid var(--s-hairline);margin:8px 0 4px 0;}
.lc-smooth-fill{position:absolute;top:0;bottom:0;border-radius:9999px;overflow:hidden;
  background:linear-gradient(90deg,#b8e64a,#c2ef4e 45%,#a8e43e);
  box-shadow:0 0 10px rgba(194,239,78,.5);
  transition:left .6s cubic-bezier(.4,0,.2,1), width .6s cubic-bezier(.4,0,.2,1);}
.lc-smooth-fill::after{content:"";position:absolute;top:0;bottom:0;left:-100%;width:55%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.5),transparent);
  animation:lc-shimmer 2.2s ease-in-out infinite;}
@keyframes lc-shimmer{0%{left:-100%}60%{left:130%}100%{left:130%}}

/* 阶段进度步骤条 */
.lc-steps{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0 8px 0;}
.lc-step{flex:1;text-align:center;padding:6px 4px;border-radius:6px;font-size:12px;font-weight:600;
  border:1px solid var(--s-hairline);}
.lc-step-done{background:var(--s-lime);color:var(--s-ink-plum);}
.lc-step-cur{background:var(--s-violet-mid);color:var(--s-white);box-shadow:0 0 8px rgba(121,98,140,.6);}
.lc-step-wait{background:var(--s-ink-plum);color:var(--s-muted);}

/* images */
.stImage img{border-radius:18px;border:1px solid var(--s-hairline);}

/* progress */
.stProgress>div>div{background:var(--s-lime);}
.stProgress>div>div>div{background:var(--s-lime);}

.lc-divider{height:3px;border-radius:2px;background:var(--s-lime);opacity:0.85;margin:8px 0 24px 0;width:120px;}

/* ---- 顶部导航栏（预留；占位用） ---- */
.lc-navbar{display:flex;justify-content:space-between;align-items:center;padding:12px 0 14px;
  margin-bottom:6px;border-bottom:1px solid var(--s-hairline);}
.lc-navbar-brand{font-size:18px;font-weight:600;color:var(--s-white);letter-spacing:0.2px;}
.lc-navbar-links{display:flex;gap:28px;}
.lc-nav-link{font-size:14px;font-weight:500;letter-spacing:0.2px;text-transform:uppercase;color:var(--s-muted);}
.lc-nav-link.lc-nav-active{color:var(--s-lime);}
</style>
"""


def inject_css() -> None:
    """注入 Sentry 设计系统 CSS。"""
    st.markdown(SENTRY_CSS, unsafe_allow_html=True)


# ------------------------------------------------------------------
# 顶部导航（可点击：主页 / 生成漫剧 / 我的创作）
#   回调只设置 _nav_to 标志；由各页顶部的 handle_nav() 在回调外做 switch_page，
#   避免「回调内 rerun 是 no-op」的警告。
# ------------------------------------------------------------------


def nav_go(target: str) -> None:
    """导航回调：只设置目标标志。"""
    st.session_state["_nav_to"] = target


def render_nav(current: str = "home") -> None:
    """渲染顶部导航（按页面路径跳）。current 用于高亮。"""
    user = st.session_state.get("_user")
    if user:
        avatar = user[0].upper()
        short = user[:8] + ("…" if len(user) > 8 else "")
        label, path, target = f"{avatar}·{short}", "pages/7_我的创作.py", "history"
    else:
        label, path, target = "登录", "pages/8_登录.py", "login"

    items = [
        ("主页", "streamlit_app.py", "home"),
        ("生成", "pages/01_策划总监.py", "create"),
        ("设置", "pages/9_设置.py", "settings"),
        (label, path, target),
    ]
    left, right = st.columns([3, 3])
    with left:
        st.markdown('<div class="lc-navbar-brand">小剧组&nbsp;·&nbsp;Little&nbsp;Crew</div>', unsafe_allow_html=True)
    with right:
        cols = st.columns(len(items))
        for col, (label_, p, t) in zip(cols, items):
            with col:
                st.button(label_, key=f"nav_{t}", on_click=nav_go, args=(p,),
                          type="primary" if current == t else "secondary", use_container_width=True)


def _get_cookies() -> CookieManager:
    """返回本会话的 CookieManager（每个 run 最多创建一个，避免组件 key 重复）。

    CookieManager 是前端组件，首次运行时还没把浏览器 cookie 读回来，此时
    ready() 为 False；每次调用都会重新渲染组件读最新 cookie。这里在「未 ready」
    时重建，便于组件回传后再拿到最新值。
    """
    cm = st.session_state.get("_cookie_man")
    if cm is None or not cm.ready():
        cm = CookieManager()
        st.session_state["_cookie_man"] = cm
    return cm


def _restore_token_from_cookie() -> None:
    """从浏览器 cookie 恢复登录态：刷新/重开浏览器后不掉线。

    st.session_state 是服务端的、绑定在浏览器 WebSocket 会话上，F5 刷新会重建
    会话导致登录态丢失。这里把 token/用户名存进浏览器 cookie，载入时读回。

    注意：这是普通 cookie（JS 可读），非 HttpOnly。本地/演示够用；若要强安全，
    请改用「服务端会话 + HttpOnly Cookie」。
    """
    if st.session_state.get("_token"):
        return
    try:
        cookies = _get_cookies()
    except Exception:
        return
    if not cookies.ready():
        # 组件首次运行还没把 cookie 读回来：显示加载态并重跑（限次数），避免白屏。
        n = int(st.session_state.get("_cookie_try", 0)) + 1
        st.session_state["_cookie_try"] = n
        if n <= 10:
            with st.spinner("正在载入登录状态…"):
                st.rerun()
        st.session_state["_cookie_try"] = 0  # 超过上限则放弃
        return  # 仍不 ready：走未登录流程（handle_nav 会跳登录页）
    st.session_state["_cookie_try"] = 0
    t = cookies.get("_token")
    u = cookies.get("_user") or "local"
    if t:
        st.session_state["_token"] = t
        st.session_state["_user"] = u


def logout_user() -> None:
    """统一登出：清 session_state 并删 cookie，避免刷新被 _restore_token_from_cookie 重新登回。

    所有页面的「登出」都应调用它，保证清得彻底、行为一致。
    """
    st.session_state["_token"] = None
    st.session_state["_user"] = None
    try:
        cookies = _get_cookies()
        if cookies.ready():
            for k in ("_token", "_user"):
                if k in cookies:
                    del cookies[k]
            cookies.save()
    except Exception:
        pass


def handle_nav(allow_anon: bool = False) -> None:
    """在页面顶部调用：根据 _nav_to 页面路径跳转（回调外）。

    allow_anon=False（默认）时，未登录一律重定向到登录页，实现「必须先登录才可用」。
    """
    _restore_token_from_cookie()
    # 硬性门槛：未登录时跳到登录页（登录页传 allow_anon=True 例外）。
    if not allow_anon and not st.session_state.get("_token"):
        st.switch_page("pages/8_登录.py")
        st.stop()
    target = st.session_state.pop("_nav_to", None)
    if target:
        st.switch_page(target)
