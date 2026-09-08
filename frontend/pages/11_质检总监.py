"""质检总监（QualityAssessor）— 独立评估结果展示页。

读取当前会话(session_state)里最新生成的评估报告，可视化展示：
  - 整体评分（仪表盘/进度条）
  - 每个镜头的评分详情（列表）
  - 一致性最差的镜头（高亮标注）
  - 不达标镜头列表 + 「一键重试」

提供：
  - 「重新评估」按钮：调用后端 reassess 接口，对当前所有图片重跑 QualityAssessor
  - 「一键重试」按钮：针对不达标镜头

没有数据时显示「暂无评估报告」占位提示。
"""

import sys
import pathlib
from datetime import datetime
from html import escape

import streamlit as st

FRONTEND = pathlib.Path(__file__).resolve().parent.parent
if str(FRONTEND) not in sys.path:
    sys.path.insert(0, str(FRONTEND))

from _api import backend_reassess, backend_retry, backend_status, backend_available  # noqa: E402
from _org import DEPT_BY_KEY, role_dept_key  # noqa: E402
from _styles import handle_nav, inject_css, nav_go, render_nav  # noqa: E402

st.set_page_config(page_title="质检总监 · 小剧组", layout="wide")
inject_css()
handle_nav()
render_nav(current="create")


def _qual_level_css(level: str) -> str:
    """等级颜色：attention=红，severe=橙，moderate=黄，ok=绿，pending=灰。"""
    color = {
        "attention": "#e5484d", "severe": "#ff8b3d", "moderate": "#f5d90a",
        "ok": "#46c060", "pending": "var(--s-violet-mid)",
    }.get(level, "var(--s-muted)")
    return color


def _score_frac(score) -> float:
    """把 0-100 分数映射到 0~1 进度（None 则 0）。"""
    if score is None:
        return 0.0
    return max(0.0, min(1.0, float(score) / 100.0))


def _load_report() -> dict:
    """从 session_state 读取最新质量报告。"""
    payload = st.session_state.get("payload") or {}
    report = payload.get("quality_report") or {}
    # 若 payload 没有 report，但 images 有 quality，则从 images 临时聚合
    if not report and payload.get("images"):
        report = _aggregate_from_images(payload.get("images") or [])
    return report


def _aggregate_from_images(images: list) -> dict:
    """兜底：没有 quality_report 时，从 images 的 quality/status 聚合一个展示用报告。"""
    shots = []
    for im in images:
        q = im.get("quality")
        status = im.get("status", "ok")
        level = "ok" if status == "ok" else "attention"
        shots.append({"shot_id": im.get("shot_id"), "quality": q, "consistency": None,
                      "status": status, "level": level})
    qs = [s["quality"] for s in shots if s["quality"] is not None]
    return {"overall_score": round(sum(qs) / len(qs), 2) if qs else None,
            "shots": shots, "lowest_shots": [], "failed_shots": [s for s in shots if s["level"] == "attention"],
            "method": "from_images", "generated_at": None}


st.markdown(
    '<div class="lc-eyebrow">技术制作部 · 质检总监</div>'
    '<div class="lc-title" style="font-size:2.2rem;">质检总监</div>'
    '<p style="color:var(--s-muted);">展示图片质量与角色一致性评估结果。不自动修改流程，仅查看与手动触发。</p>',
    unsafe_allow_html=True,
)

# 操作区：重新评估 / 一键重试
sid = st.session_state.get("session_id")
token = st.session_state.get("_token")


def _reassess():
    if not sid:
        st.session_state["qc_msg"] = "请先创作一个会话，再评估。"
        return
    res = backend_reassess(sid, token)
    if res.get("status") == "ok":
        qr = res.get("quality_report") or {}
        # 写回 session payload，供本页与结果页共用
        payload = dict(st.session_state.get("payload") or {})
        payload["quality_report"] = qr
        st.session_state["payload"] = payload
        st.session_state["qc_msg"] = f"已重新评估 · 整体 {qr.get('overall_score') or '—'}"
    else:
        st.session_state["qc_msg"] = res.get("message", "评估失败")


def _retry_selected(shot_ids: list):
    """精确重排用户勾选的镜头（异步）。置为重排中状态，由页面轮询进度。"""
    if not sid:
        st.session_state["qc_msg"] = "请先创作一个会话。"
        return
    if not shot_ids:
        st.session_state["qc_msg"] = "请先勾选要重排的镜头。"
        return
    st.session_state["qc_msg"] = ""
    st.session_state["qc_retrying"] = True  # 进入重排中状态
    res = backend_retry(sid, shot_ids, token)
    if res.get("status") == "accepted":
        st.session_state["qc_msg"] = f"已启动，正在重排 {len(shot_ids)} 个镜头…"
    else:
        st.session_state["qc_msg"] = res.get("message", "重排启动失败")
        st.session_state["qc_retrying"] = False


c1, c2 = st.columns([2, 3])
with c1:
    st.button("🔄 重新评估", on_click=_reassess, key="qc_reassess", use_container_width=True, type="primary")
with c2:
    if st.session_state.get("qc_msg"):
        st.caption(st.session_state["qc_msg"])

# ---- 重排进度反馈（轮询 status 的 _retry_progress，异步重排时显示） ----
def _poll_retry_progress() -> bool:
    """轮询后端重排进度；返回 True 表示仍在进行/需刷新。"""
    if not st.session_state.get("qc_retrying"):
        return False
    stat = backend_status(sid, token) if sid else {}
    prog = (stat.get("payload") or {}).get("_retry_progress") or {}
    state = prog.get("state", "running")
    total = prog.get("total") or 1
    done = prog.get("done") or 0
    frac = min(1.0, done / total) if total else 0.0
    st.markdown(
        f'<div class="lc-card"><div class="lc-label">重排进度</div>'
        f'<div class="lc-smooth-track" style="height:12px;">'
        f'<div class="lc-smooth-fill" style="left:0;width:{frac*100:.0f}%;"></div></div>'
        f"<p style='color:var(--s-muted);font-size:13px;'>{escape(str(prog.get('message','')))} · {done}/{total}</p></div>",
        unsafe_allow_html=True,
    )
    if state == "done":
        st.session_state["qc_retrying"] = False
        st.session_state["qc_msg"] = "✅ 重排完成"
        # 刷新本地 payload（含最新 images / quality_report）
        payload = dict(st.session_state.get("payload") or {})
        payload["images"] = (stat.get("payload") or {}).get("images") or payload.get("images", [])
        payload["quality_report"] = (stat.get("payload") or {}).get("quality_report") or payload.get("quality_report", {})
        st.session_state["payload"] = payload
        st.rerun()
        return False
    if state == "error":
        st.session_state["qc_retrying"] = False
        st.session_state["qc_msg"] = "⚠ " + (prog.get("message") or "重排失败")
        return False
    # 仍在进行：稍等后自动刷新
    import time
    time.sleep(1.2)
    st.rerun()
    return True


_poll_retry_progress()

report = _load_report()
if not report or not report.get("shots"):
    st.info("暂无评估报告：请先完成一次创作（生成图片后会自动评估），或点击「重新评估」。")
    st.stop()

st.markdown('<div style="height:8px"></div>', unsafe_allow_html=True)

# ---- 整体评分仪表盘 ----
overall = report.get("overall_score")
st.markdown(
    f'<div class="lc-card"><div class="lc-label">整体评分</div>'
    f"<h2 style='margin:4px 0;'>{escape(str(overall)) if overall is not None else '—'}<span style='font-size:16px;color:var(--s-muted);'> / 100</span></h2>"
    f'<div class="lc-smooth-track" style="height:10px;">'
    f'<div class="lc-smooth-fill" style="left:0;width:{_score_frac(overall)*100:.0f}%;'
    f'background:{"var(--s-lime)" if (overall or 0) >= 75 else "#ffb23d" if (overall or 0) >= 60 else "#e5484d"};"></div>'
    f"</div>"
    f"<p style='color:var(--s-muted);font-size:13px;'>评估方式：{escape(str(report.get('method') or '—'))} · "
    f"镜头 {len(report.get('shots') or [])} 个</p></div>",
    unsafe_allow_html=True,
)

st.markdown('<div class="lc-divider"></div>', unsafe_allow_html=True)

# ---- 逐镜评分列表 ----
st.markdown('<div class="lc-label">逐镜详情</div>', unsafe_allow_html=True)
shots = report.get("shots") or []
_LEVEL_LABEL = {"attention": "⚑ 需关注", "severe": "⚠ 严重", "moderate": "△ 中度",
                "ok": "✓ 合格", "pending": "…"}
for s in shots:
    q = s.get("quality")
    cons = s.get("consistency")
    level = s.get("level", "ok")
    color = _qual_level_css(level)
    qs = "—" if q is None else f"{round(float(q)*100,1):.0f}"
    cs = "—" if cons is None else f"{cons:.0f}"
    badge = _LEVEL_LABEL.get(level, str(level))
    st.markdown(
        f'<div class="lc-card" style="padding:14px 20px;margin-bottom:8px;">'
        f'<div style="display:flex;justify-content:space-between;align-items:center;">'
        f'<div><b style="color:var(--s-white);">{escape(str(s.get("shot_id","?")))}</b>'
        f'<span style="color:var(--s-muted);"> 质量 {qs} · 一致性 {cs}</span></div>'
        f'<span style="color:{color};font-size:12px;font-weight:600;">{escape(str(badge))}</span>'
        f"</div></div>",
        unsafe_allow_html=True,
    )

# ---- 一致性最差镜头（高亮标注） ----
lowest = report.get("lowest_shots") or []
if lowest:
    st.markdown('<div class="lc-divider"></div><div class="lc-label">一致性最差的镜头</div>',
                unsafe_allow_html=True)
    for ls in lowest:
        color = _qual_level_css(ls.get("level"))
        st.markdown(
            f'<div class="lc-card" style="padding:12px 20px;margin-bottom:8px;'
            f'border-left:4px solid {color};">'
            f"<b>{escape(str(ls.get('shot_id')))}</b> · 角色「{escape(str(ls.get('character')))}」 · 分 {escape(str(ls.get('score')))} "
            f"<span style='color:{color};'>({escape(str(ls.get('level')))})</span></div>",
            unsafe_allow_html=True,
        )

# ---- 不达标镜头 + 一键重试 ----
failed = report.get("failed_shots") or []
st.markdown('<div class="lc-divider"></div><div class="lc-label">不达标镜头（需处理）</div>',
            unsafe_allow_html=True)
if failed:
    for f in failed:
        st.markdown(
            f'<div class="lc-card" style="padding:12px 20px;margin-bottom:8px;'
            f'border-left:4px solid #e5484d;">'
            f"<b>{escape(str(f.get('shot_id')))}</b> · 质量 {round(float(f.get('quality') or 0)*100):.0f} · "
            f"一致性 {escape(str(f.get('consistency') or '—'))}</div>",
            unsafe_allow_html=True,
        )
else:
    st.markdown("<p style='color:var(--s-muted);'>无不达标镜头。</p>", unsafe_allow_html=True)

# ---- 选图 · 精确只重排（多选镜头，只重排勾选的，其余保留） ----
st.markdown('<div class="lc-divider"></div><div class="lc-label">选图 · 精确重排（多选）</div>',
            unsafe_allow_html=True)
st.caption("勾选要重新生成的镜头，其余保持原图。适合只重排失败的几格，而不是整批重跑。")
default_failed = [s["shot_id"] for s in (report.get("failed_shots") or [])]
retrying = st.session_state.get("qc_retrying", False)
picked = st.multiselect(
    "选择要重排的镜头",
    options=[s["shot_id"] for s in shots],
    default=default_failed,
    key="qc_pick_shots",
    disabled=retrying,
)
st.button(f"🚀 只重排已选 {len(picked)} 个镜头", key="qc_retry_selected",
          on_click=_retry_selected, args=(picked,), use_container_width=True,
          type="primary", disabled=retrying)
st.session_state.setdefault("qc_retrying", False)
st.session_state.setdefault("raw_input", "")
st.session_state.setdefault("_token", None)

# 返回部门/主页
st.markdown('<div style="height:16px"></div>', unsafe_allow_html=True)
st.button("← 返回技术制作部", key="qc_back", on_click=nav_go,
          args=("pages/20_技术制作部.py",), use_container_width=True)
