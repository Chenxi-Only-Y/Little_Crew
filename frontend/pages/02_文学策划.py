"""文学策划（ScriptCritic）— 独立审查报告展示页。

读取当前会话(session_state)里剧本与审查报告，卡片 + 标签页展示：
  - 评分概览：逻辑 / 冲突 / 动机 三维进度条 + 整体分
  - 详细建议：修改建议列表（可展开/折叠）+ 强项/问题
  - 修改历史：历次审查时间线（跨会话持久化，读自 critic_history）

操作：
  - 「重新审查」：调用后端 /api/crew/review 手动触发 ScriptCritic
  - 建议条目可点击，跳转到编剧页进一步修改

没有数据时显示「暂无审查报告」占位提示。
"""

import sys
import pathlib
from datetime import datetime
from html import escape

import streamlit as st

FRONTEND = pathlib.Path(__file__).resolve().parent.parent
if str(FRONTEND) not in sys.path:
    sys.path.insert(0, str(FRONTEND))

from _api import backend_review, backend_review_history, backend_available  # noqa: E402
from _org import DEPT_BY_KEY, role_dept_key  # noqa: E402
from _styles import handle_nav, inject_css, nav_go, render_nav  # noqa: E402

st.set_page_config(page_title="文学策划 · 小剧组", layout="wide")
inject_css()
handle_nav()
render_nav(current="create")


def _load_report() -> dict:
    payload = st.session_state.get("payload") or {}
    return payload.get("critic_report") or {}


def _load_history(session_id: str) -> list:
    """优先读持久化的跨会话历史，其次用 session 内 critic_report.history。"""
    if session_id:
        res = backend_review_history(session_id, st.session_state.get("_token"))
        if res.get("status") == "ok" and res.get("history"):
            return res["history"]
    return (_load_report().get("history") or [])


def _score_frac(v) -> float:
    if v is None:
        return 0.0
    return max(0.0, min(1.0, float(v) / 100.0))


st.markdown(
    '<div class="lc-eyebrow">创作策划部 · 文学策划</div>'
    '<div class="lc-title" style="font-size:2.2rem;">文学策划 · 剧本审查</div>'
    '<p style="color:var(--s-muted);">展示剧情逻辑、冲突张力、角色动机评审。仅查看与手动触发，不改自动流程。</p>',
    unsafe_allow_html=True,
)

sid = st.session_state.get("session_id")
token = st.session_state.get("_token")


def _review():
    if not sid:
        st.session_state["lc_msg"] = "请先创作一个会话，再审查。"
        return
    res = backend_review(sid, token)
    if res.get("status") == "ok":
        payload = dict(st.session_state.get("payload") or {})
        payload["script_review"] = res.get("script_review") or {}
        payload["critic_report"] = res.get("critic_report") or {}
        st.session_state["payload"] = payload
        sc = (res.get("critic_report") or {}).get("scores") or {}
        st.session_state["lc_msg"] = f"已重新审查 · 综合 {sc.get('overall') or '—'}"
        st.rerun()
    else:
        st.session_state["lc_msg"] = res.get("message", "审查失败")


st.button("🔄 重新审查", on_click=_review, key="lc_review", use_container_width=True, type="primary")

if st.session_state.get("lc_msg"):
    st.caption(st.session_state["lc_msg"])

report = _load_report()
if not report or not report.get("scores"):
    st.info("暂无审查报告：请先创作一个会话（编剧完成后会自动评审），或点击「重新审查」。")
    # 仍是当前会话无报告；若历史存在也提示可看历史
    if sid:
        hist = _load_history(sid)
        if hist:
            st.caption(f"已找到 {len(hist)} 条跨会话审查历史（见下方历史标签）。")
    st.stop()

scores = report.get("scores") or {}
overall = scores.get("overall")
history = _load_history(sid) or report.get("history") or []
suggestions = report.get("suggestions") or []
issues = report.get("issues") or []
strengths = report.get("strengths") or []

# 标签页：评分概览 / 详细建议 / 修改历史
tab1, tab2, tab3 = st.tabs(["📊 评分概览", "📝 详细建议", "🕘 修改历史"])

with tab1:
    st.markdown(
        f'<div class="lc-card"><div class="lc-label">综合评分</div>'
        f"<h2 style='margin:4px 0;'>{escape(str(overall)) if overall is not None else '—'}<span style='font-size:16px;color:var(--s-muted);'> / 100</span></h2>"
        f'<div class="lc-smooth-track" style="height:10px;">'
        f'<div class="lc-smooth-fill" style="left:0;width:{_score_frac(overall)*100:.0f}%;'
        f'background:{"var(--s-lime)" if (overall or 0)>=75 else "#ffb23d" if (overall or 0)>=60 else "#e5484d"};"></div>'
        f"</div></div>",
        unsafe_allow_html=True,
    )
    st.markdown('<div class="lc-divider"></div>', unsafe_allow_html=True)
    dims = [("剧情逻辑", "logic", scores.get("logic")), ("冲突张力", "tension", scores.get("tension")),
            ("角色动机", "motive", scores.get("motive"))]
    cols = st.columns(3)
    for i, (label, key, val) in enumerate(dims):
        with cols[i]:
            color = ("var(--s-lime)" if (val or 0) >= 75 else "#ffb23d" if (val or 0) >= 60 else "#e5484d")
            st.markdown(
                f'<div class="lc-card"><div class="lc-label">{escape(label)}</div>'
                f"<h3 style='margin:2px 0;'>{escape(str(val)) if val is not None else '—'}</h3>"
                f'<div class="lc-smooth-track" style="height:8px;">'
                f'<div class="lc-smooth-fill" style="left:0;width:{_score_frac(val)*100:.0f}%;background:{color};"></div>'
                f"</div></div>",
                unsafe_allow_html=True,
            )

with tab2:
    st.markdown('<div class="lc-label">修改建议</div>', unsafe_allow_html=True)
    if suggestions:
        with st.expander("展开全部修改建议", expanded=False):
            for i, s in enumerate(suggestions, 1):
                st.markdown(
                    f'<div class="lc-card" style="padding:12px 20px;margin-bottom:8px;">'
                    f"<p style='margin:0;'>{i}. {escape(str(s))}</p></div>",
                    unsafe_allow_html=True,
                )
                if st.button(f"✏️ 去编剧页修改：{s[:14]}…", key=f"lc_sugg_{i}",
                             use_container_width=True):
                    st.session_state["_nav_to"] = "pages/03_编剧.py"
    else:
        st.markdown("<p style='color:var(--s-muted);'>无修改建议。</p>", unsafe_allow_html=True)

    st.markdown('<div class="lc-divider"></div><div class="lc-label">问题</div>', unsafe_allow_html=True)
    if issues:
        for i, it in enumerate(issues, 1):
            st.markdown(f"<p style='color:var(--s-muted);'>• {escape(str(it))}</p>", unsafe_allow_html=True)
    else:
        st.markdown("<p style='color:var(--s-muted);'>无明显问题。</p>", unsafe_allow_html=True)

    st.markdown('<div class="lc-divider"></div><div class="lc-label">强项</div>', unsafe_allow_html=True)
    if strengths:
        for s in strengths:
            st.markdown(f"<p style='color:var(--s-lime);'>✓ {escape(str(s))}</p>", unsafe_allow_html=True)
    else:
        st.markdown("<p style='color:var(--s-muted);'>无。</p>", unsafe_allow_html=True)

with tab3:
    st.markdown('<div class="lc-label">审查历史（最近 {n} 次 · 跨会话）</div>'.format(n=len(history)),
                unsafe_allow_html=True)
    if history:
        for h in reversed(history):
            ts = h.get("time")
            tstr = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "—"
            st.markdown(
                f'<div class="lc-card" style="padding:12px 20px;margin-bottom:8px;">'
                f'<div class="lc-label">{escape(tstr)} · {escape(str(h.get("note") or "评审"))}</div>'
                f"<p style='margin:0;'>综合 {escape(str(h.get('overall_score')))} · 逻辑 {escape(str(h.get('logic')))} · "
                f"冲突 {escape(str(h.get('tension')))} · 动机 {escape(str(h.get('motive')))}</p></div>",
                unsafe_allow_html=True,
            )
    else:
        st.markdown("<p style='color:var(--s-muted);'>暂无历史记录。</p>", unsafe_allow_html=True)

# 返回部门
st.markdown('<div style="height:16px"></div>', unsafe_allow_html=True)
st.button("← 返回创作策划部", key="lc_back", on_click=nav_go,
          args=("pages/20_创作策划部.py",), use_container_width=True)
