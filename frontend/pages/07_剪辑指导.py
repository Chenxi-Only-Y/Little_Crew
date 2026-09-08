"""剪辑指导（RhythmDirector）— 独立职业页。

读取当前会话(session_state)里 rhythm_plan / storyboard，展示：
  - 时间轴视图：所有镜头按顺序排列，每镜显示时长与节奏标签
  - 总时长预估
功能：
  - 「重新规划」：调用后端 /api/crew/rhythm 重排（按情绪映射）
  - 「手动调整」：每镜滑动条改时长，提交后写回
没有数据时显示「暂无节奏规划」占位提示。
"""

import sys
import pathlib
from html import escape

import streamlit as st

FRONTEND = pathlib.Path(__file__).resolve().parent.parent
if str(FRONTEND) not in sys.path:
    sys.path.insert(0, str(FRONTEND))

from _api import backend_rhythm, backend_available  # noqa: E402
from _org import DEPT_BY_KEY, role_dept_key  # noqa: E402
from _styles import handle_nav, inject_css, nav_go, render_nav  # noqa: E402

st.set_page_config(page_title="剪辑指导 · 小剧组", layout="wide")
inject_css()
handle_nav()
render_nav(current="create")

TEMPO_LABEL = {"fast": ("⚡ 快", "#e5484d"), "medium": ("△ 中", "#ffb23d"), "slow": ("〇 慢", "#46c060")}


def _load() -> tuple:
    payload = st.session_state.get("payload") or {}
    return payload.get("rhythm_plan") or [], payload.get("storyboard") or []


st.markdown(
    '<div class="lc-eyebrow">导演分镜部 · 剪辑指导</div>'
    '<div class="lc-title" style="font-size:2.2rem;">剪辑指导</div>'
    '<p style="color:var(--s-muted);">把控叙事节奏，为每个镜头分配时长与节奏标签。</p>',
    unsafe_allow_html=True,
)

sid = st.session_state.get("session_id")
token = st.session_state.get("_token")


def _replan(overrides: dict | None = None):
    if not sid:
        st.session_state["rd_msg"] = "请先创作一个会话。"
        return
    res = backend_rhythm(sid, overrides, token)
    if res.get("status") == "ok":
        payload = dict(st.session_state.get("payload") or {})
        payload["rhythm_plan"] = res.get("rhythm_plan") or []
        payload["storyboard"] = res.get("storyboard") or payload.get("storyboard", [])
        st.session_state["payload"] = payload
        total = sum(p.get("duration", 0) for p in (res.get("rhythm_plan") or []))
        st.session_state["rd_msg"] = f"已重新规划 · {len(res.get('rhythm_plan') or [])} 镜头 · 总时长 {total:.1f}s"
        st.rerun()
    else:
        st.session_state["rd_msg"] = res.get("message", "规划失败")


def _apply_sliders(plan: list):
    overrides = {}
    for p in plan:
        val = st.session_state.get(f"rd_slider_{p['shot_id']}")
        if val is not None:
            overrides[p["shot_id"]] = float(val)
    _replan(overrides)


st.button("🔄 重新规划", on_click=lambda: _replan(None), key="rd_replan",
          use_container_width=True, type="primary")
if st.session_state.get("rd_msg"):
    st.caption(st.session_state["rd_msg"])

plan, storyboard = _load()
if not plan and not storyboard:
    st.info("暂无节奏规划：请先创作一个会话（分镜完成后会自动规划），或点击「重新规划」。")
    st.stop()

# 用 plan 优先；无 plan 时从 storyboard 兜底
if not plan:
    plan = [{"shot_id": s.get("shot_id"), "duration": s.get("duration_s", 2.5),
             "tempo": s.get("tempo", "medium")} for s in storyboard]

total = sum(p.get("duration", 0) for p in plan)
st.markdown(f'<div class="lc-divider"></div>', unsafe_allow_html=True)
st.markdown(
    f'<div class="lc-card"><div class="lc-label">总时长预估</div>'
    f"<h2 style='margin:2px 0;'>{total:.1f}<span style='font-size:16px;color:var(--s-muted);'> 秒</span></h2>"
    f"<p style='color:var(--s-muted);font-size:13px;'>{len(plan)} 个镜头</p></div>",
    unsafe_allow_html=True,
)

# 时间轴视图：镜头按顺序排列，每镜一段
st.markdown('<div class="lc-divider"></div><div class="lc-label">时间轴</div>', unsafe_allow_html=True)
st.caption("每格为一个镜头，长度随时长比例。下方可拖动调整该镜头时长。")
# 简易时间轴：等宽块 + 时长标签
cols = st.columns(len(plan))
for i, p in enumerate(plan):
    tempo = p.get("tempo", "medium")
    label, color = TEMPO_LABEL.get(tempo, ("", "var(--s-muted)"))
    with cols[i % len(cols)]:
        st.markdown(
            f'<div class="lc-home-card" style="min-height:64px;display:flex;flex-direction:column;'
            f'justify-content:center;align-items:center;border-top:3px solid {color};">'
            f"<div style='font-size:12px;color:var(--s-muted);'>{escape(str(p.get('shot_id','?')))}</div>"
            f"<b>{p.get('duration',0):.1f}s</b>"
            f"<div style='font-size:12px;color:{color};'>{label}</div></div>",
            unsafe_allow_html=True,
        )

# 手动调整：每镜滑动条
st.markdown('<div class="lc-divider"></div><div class="lc-label">手动调整时长（秒）</div>',
            unsafe_allow_html=True)
for p in plan:
    cur = float(p.get("duration", 2.5))
    tempo = p.get("tempo", "medium")
    lo, hi = {"fast": (0.5, 5.0), "medium": (0.5, 8.0), "slow": (0.5, 10.0)}.get(tempo, (0.5, 8.0))
    st.slider(f"{p.get('shot_id','?')} · {TEMPO_LABEL.get(tempo,('',''))[0]}",
              min_value=0.5, max_value=hi, value=cur, step=0.5,
              key=f"rd_slider_{p['shot_id']}")
if st.button("应用手动调整", key="rd_apply", use_container_width=True, type="primary"):
    _apply_sliders(plan)

# 返回部门
st.markdown('<div style="height:16px"></div>', unsafe_allow_html=True)
st.button("← 返回导演分镜部", key="rd_back", on_click=nav_go,
          args=("pages/20_导演分镜部.py",), use_container_width=True)
