"""小剧组 — 各职业控制台结果渲染器（供 _console 各页复用）。"""

import json
from html import escape

import streamlit as st

SEV_LABEL = {"high": ("🔴 高", "#e5484d"), "medium": ("⚠️ 中", "#ffb23d"), "low": ("🟢 低", "#46c060")}


def _score_frac(v):
    return max(0.0, min(1.0, float(v) / 100.0)) if v is not None else 0.0


def render_outline(payload: dict):
    outline = payload.get("outline") or {}
    if not outline:
        err = payload.get("error")
        st.warning("⚠️ 暂无大纲（策划环节未产出）。请检查右上「🧠 思考过程」里的异常说明，或重试一次。")
        return
    st.markdown(f'<div class="lc-card"><div class="lc-label">{escape(str(outline.get("genre","")))}</div>'
                f"<h2>{escape(str(outline.get('title','未命名')))}</h2>"
                f"<p style='color:var(--s-muted);'>{escape(str(outline.get('logline','')))}</p>"
                f"<p style='font-size:13px;color:var(--s-muted);'>画风：{escape(str(outline.get('style','') or '—'))}</p></div>",
                unsafe_allow_html=True)


def render_script(payload: dict):
    script = payload.get("script") or {}
    st.markdown(f'<div class="lc-card"><pre>{escape(json.dumps(script, ensure_ascii=False, indent=2))}</pre></div>',
                unsafe_allow_html=True)


def render_storyboard(payload: dict):
    shots = payload.get("storyboard") or []
    st.caption(f"{len(shots)} 个镜头")
    for s in shots:
        st.markdown(
            f'<div class="lc-card" style="padding:10px 18px;margin-bottom:6px;">'
            f'<b>{escape(str(s.get("shot_id")))}</b> · {escape(str(s.get("shot_type","")))} · {escape(str(s.get("camera","")))}'
            f" · {escape(str(s.get('duration_s','')))}s · {escape(str(s.get('tempo','')))}</div>",
            unsafe_allow_html=True,
        )
        st.text(str(s.get("description","")))
        if s.get("dialogue"):
            st.caption("台词：" + str(s.get("dialogue")))


def render_images(payload: dict):
    images = payload.get("images") or []
    if not images:
        st.info("暂无图片。")
        return
    cols = st.columns(min(4, max(1, len(images))))
    for i, im in enumerate(images):
        with cols[i % len(cols)]:
            try:
                url = im.get("local_path") or im.get("image_url")
                if url:
                    st.image(url, caption=f"{im.get('shot_id')} · 质量 {im.get('quality', '—')}")
            except Exception:
                pass


def render_quality(payload: dict):
    report = payload.get("quality_report") or {}
    if not report.get("shots"):
        st.info("暂无质检报告。")
        return
    overall = report.get("overall_score")
    color = "var(--s-lime)" if (overall or 0) >= 75 else "#ffb23d" if (overall or 0) >= 60 else "#e5484d"
    st.markdown(
        f'<div class="lc-card"><div class="lc-label">整体评分</div>'
        f"<h2>{overall or '—'} / 100</h2>"
        f'<div class="lc-smooth-track"><div class="lc-smooth-fill" style="left:0;width:{_score_frac(overall)*100:.0f}%;background:{color};"></div></div>'
        f"<p style='color:var(--s-muted);font-size:13px;'>{len(report.get('shots') or [])} 镜头</p></div>",
        unsafe_allow_html=True,
    )
    for s in report.get("shots") or []:
        st.markdown(
            f'<div class="lc-card" style="padding:10px 18px;margin-bottom:6px;">'
            f"<b>{s.get('shot_id')}</b> · 质量 {round(float(s.get('quality') or 0)*100):.0f} · "
            f"一致性 {s.get('consistency') or '—'}</div>",
            unsafe_allow_html=True,
        )


def render_characters(payload: dict):
    profiles = payload.get("character_profiles") or {}
    refs = payload.get("character_reference_images") or {}
    if not profiles and not refs:
        st.info("暂无角色资产。")
        return
    cols = st.columns(min(4, max(1, len(profiles or refs))))
    keys = list(dict.fromkeys(list(profiles.keys()) + list(refs.keys())))
    for i, name in enumerate(keys):
        with cols[i % len(cols)]:
            ref = refs.get(name, "")
            if ref:
                try:
                    st.image(ref, width=140)
                except Exception:
                    pass
            prof = profiles.get(name, {}) or {}
            st.markdown(
                f'<div class="lc-card" style="padding:10px 18px;"><div class="lc-label">{name}</div>'
                f"<p style='font-size:12px;color:var(--s-muted);'>{prof.get('appearance','')}</p></div>",
                unsafe_allow_html=True,
            )


def render_scene(payload: dict):
    refs = payload.get("scene_reference_images") or {}
    profs = payload.get("scene_design_profiles") or {}
    if not refs and not profs:
        st.info("暂无场景资产。")
        return
    cols = st.columns(min(3, max(1, len(refs or profs))))
    keys = list(dict.fromkeys(list(profs.keys()) + list(refs.keys())))
    for i, k in enumerate(keys):
        with cols[i % len(cols)]:
            if refs.get(k):
                try:
                    st.image(refs[k], width=280)
                except Exception:
                    pass
            prof = profs.get(k) or {}
            st.markdown(
                f'<div class="lc-card" style="padding:10px 18px;"><div class="lc-label">场景 {k}</div>'
                f"<p style='font-size:13px;color:var(--s-muted);'>{prof.get('setting','')}</p></div>",
                unsafe_allow_html=True,
            )


def render_continuity(payload: dict):
    report = payload.get("continuity_report") or []
    if not report:
        st.info("暂无穿帮风险。")
        return
    order = {"high": 0, "medium": 1, "low": 2}
    for r in sorted(report, key=lambda x: order.get(x.get("severity", "low"), 2)):
        label, color = SEV_LABEL.get(r.get("severity", "low"), ("", "var(--s-muted)"))
        shots = " → ".join(str(x) for x in (r.get("shots") or []))
        st.markdown(
            f'<div class="lc-card" style="padding:12px 18px;margin-bottom:6px;border-left:4px solid {color};">'
            f'<div style="font-size:12px;color:{color};">规则 {r.get("rule","-")} · {label} · {shots}</div>'
            f"<p style='margin:6px 0;'>{r.get('risk','')}</p>"
            f"<p style='font-size:12px;color:var(--s-muted);'>建议：{r.get('suggestion','')}</p></div>",
            unsafe_allow_html=True,
        )


def render_critic(payload: dict):
    report = payload.get("critic_report") or {}
    scores = report.get("scores") or {}
    st.markdown(
        f'<div class="lc-card"><div class="lc-label">综合评分</div>'
        f"<h2>{scores.get('overall') or '—'} / 100</h2></div>", unsafe_allow_html=True)
    for label, key in [("剧情逻辑", "logic"), ("冲突张力", "tension"), ("角色动机", "motive")]:
        st.markdown(
            f'<div class="lc-card"><div class="lc-label">{escape(label)}</div><h3>{escape(str(scores.get(key, "—")))}</h3></div>',
            unsafe_allow_html=True,
        )
    for s in report.get("suggestions") or []:
        st.markdown(f"<p style='color:var(--s-muted);'>• {escape(str(s))}</p>", unsafe_allow_html=True)


def render_rhythm(payload: dict):
    plan = payload.get("rhythm_plan") or []
    storyboard = payload.get("storyboard") or []
    if not plan:
        plan = [{"shot_id": s.get("shot_id"), "duration": s.get("duration_s", 2.5),
                 "tempo": s.get("tempo", "medium")} for s in storyboard]
    total = sum(p.get("duration", 0) for p in plan)
    st.markdown(f'<div class="lc-card"><div class="lc-label">总时长预估</div><h2>{total:.1f} 秒</h2>'
                f"<p style='color:var(--s-muted);font-size:13px;'>{len(plan)} 镜头</p></div>",
                unsafe_allow_html=True)
    for p in plan:
        st.markdown(
            f'<div class="lc-card" style="padding:10px 18px;margin-bottom:6px;">'
            f"<b>{escape(str(p.get('shot_id')))}</b> · {p.get('duration',0):.1f}s · {escape(str(p.get('tempo','')))}</div>",
            unsafe_allow_html=True,
        )


def render_prompts(payload: dict):
    prompts = payload.get("prompts") or []
    if not prompts:
        st.info("暂无提示词。")
        return
    for p in prompts:
        st.markdown(
            f'<div class="lc-card" style="padding:12px 18px;margin-bottom:6px;">'
            f'<div class="lc-label">{escape(str(p.get("shot_id","?")))} · 角色 {escape(str(p.get("character_name","") or "—"))}</div>'
            f"<p style='font-size:13px;'>{escape(str(p.get('prompt','')))}</p>"
            f"<p style='font-size:12px;color:var(--s-muted);'>负向：{escape(str(p.get('negative_prompt','') or '—'))}</p></div>",
            unsafe_allow_html=True,
        )


def render_video(payload: dict):
    import _api
    video = payload.get("video") or {}
    session_id = payload.get("session_id") or st.session_state.get("session_id")
    token = st.session_state.get("_token")
    images = payload.get("images") or []
    if video.get("produced") == 1:
        st.success("视频已生成：" + str(video.get("dir_or_path")))
        try:
            st.video(video.get("dir_or_path"))
        except Exception:
            pass
    else:
        if not images:
            st.warning("⚠️ 当前会话还没有画面（无图片）。需先让流程跑到「摄影师出图」完成，才能生成视频。"
                       "可减少勾选的检查点，或把终点设到「摄影师/质检」后重跑。")
        elif video.get("dir_or_path"):
            st.info("占位模式（MOCK_VIDEO=1）：未真正合成视频。")
        if session_id:
            if st.button("🎬 生成视频", key=f"mkvideo_{session_id}"):
                with st.spinner("视频生成中…（MiniMax-H3 需几分钟，请稍候）"):
                    _api.backend_video(session_id, token)
                st.session_state["payload"] = None
                st.rerun()


def render_publish(payload: dict):
    results = payload.get("publish_results") or {}
    if not results:
        st.info("暂无发布状态。")
        return
    for pk, r in results.items():
        status = r.get("status")
        st.markdown(
            f'<div class="lc-card" style="padding:10px 18px;margin-bottom:6px;">'
            f"<b>{escape(str(r.get('name', pk)))}</b> · {escape(str(status))} · {escape(str(r.get('title','')))}</div>",
            unsafe_allow_html=True,
        )
