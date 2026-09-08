"""策划总监（作家）— 独立控制台页（脚手架驱动）。"""
import sys, pathlib
from html import escape
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from _console import render_console_v2


def _render_result(payload: dict):
    import streamlit as st
    outline = payload.get("outline") or {}
    if not outline:
        st.info("暂无大纲结果。")
        return
    st.markdown('<div class="lc-label">故事大纲</div>', unsafe_allow_html=True)
    st.markdown(
        f'<div class="lc-card"><div class="lc-label">{escape(str(outline.get("genre","")))}</div>'
        f"<h2 style='margin:4px 0;'>{escape(str(outline.get('title','未命名')))}</h2>"
        f"<p style='color:var(--s-muted);'>{escape(str(outline.get('logline','')))}</p>"
        f"<p style='font-size:13px;color:var(--s-muted);'>画风：{escape(str(outline.get('style','') or '—'))}</p></div>",
        unsafe_allow_html=True,
    )
    chars = outline.get("characters") or []
    if chars:
        st.markdown('<div class="lc-label">人物</div>', unsafe_allow_html=True)
        cc = st.columns(min(4, max(1, len(chars))))
        for i, ch in enumerate(chars):
            with cc[i % len(cc)]:
                st.markdown(
                    f'<div class="lc-home-card" style="min-height:88px;">'
                    f'<div class="lc-label">{escape(str(ch.get("name","?")))} · {escape(str(ch.get("role","")))}</div>'
                    f"<p style='font-size:12px;color:var(--s-muted);'>{escape(str(ch.get('description','')))}</p></div>",
                    unsafe_allow_html=True,
                )
    beats = outline.get("plot_beats") or []
    if beats:
        st.markdown('<div class="lc-label">情节点</div>', unsafe_allow_html=True)
        for b in beats:
            st.markdown(f"<p style='color:var(--s-muted);'>{escape(str(b.get('order','-')))}. {escape(str(b.get('summary','')))}</p>",
                        unsafe_allow_html=True)


render_console_v2(
    role_key="writer", start_key="writer",
    eyebrow="创作策划部 · 策划总监", title="策划总监",
    subtitle="一句话，打磨成可执行的故事大纲。",
    input_label="一句话创意", placeholder="一只会说话的小猫想当侦探……", input_height=100,
    render_result=_render_result,
    dept_page="pages/20_创作策划部.py",
)
