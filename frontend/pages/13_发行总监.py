"""发行总监（Distributor）— 独立职业页。

读取当前会话(session_state)里项目元数据 + 发布状态，展示：
  - 每个平台一个卡片（未发布/发布中/成功/失败）
  - 支持勾选目标平台
  - 展示自动生成的标题/描述/标签（可编辑）
  - 发布成功后显示链接（可点击跳转）
  - 「一键发布所有选定平台」按钮
  - OAuth 链接入口（骨架）
Mock 模式返回模拟发布链接。
"""

import sys
import pathlib
import re
from html import escape

import streamlit as st

FRONTEND = pathlib.Path(__file__).resolve().parent.parent
if str(FRONTEND) not in sys.path:
    sys.path.insert(0, str(FRONTEND))

from _api import backend_publish, backend_link_platform, backend_available  # noqa: E402
from _org import DEPT_BY_KEY, role_dept_key  # noqa: E402
from _styles import handle_nav, inject_css, nav_go, render_nav  # noqa: E402

st.set_page_config(page_title="发行总监 · 小剧组", layout="wide")
inject_css()
handle_nav()
render_nav(current="create")

# 平台注册表（与后端 Distributor.PLATFORMS 对齐；优先 抖音）
PLATFORMS = [
    ("bilibili", "B站", "https://www.bilibili.com"),
    ("douyin", "抖音", "https://www.douyin.com"),
    ("youtube", "YouTube", "https://www.youtube.com"),
    ("kuaishou", "快手", "https://www.kuaishou.com"),
    ("xiaohongshu", "小红书", "https://www.xiaohongshu.com"),
]
STATUS_LABEL = {"draft": "未发布", "publishing": "发布中", "success": "成功", "failed": "失败"}
STATUS_COLOR = {"draft": "var(--s-violet-mid)", "publishing": "#ffb23d", "success": "#46c060", "failed": "#e5484d"}


def _load() -> tuple:
    payload = st.session_state.get("payload") or {}
    return payload.get("publish_results") or {}, payload.get("outline") or {}, payload.get("video") or {}


st.markdown(
    '<div class="lc-eyebrow">后期合成分发部 · 发行总监</div>'
    '<div class="lc-title" style="font-size:2.2rem;">发行总监</div>'
    '<p style="color:var(--s-muted);">把成片一键发布到各平台。优先支持 B站/抖音，其余为占位。</p>',
    unsafe_allow_html=True,
)

sid = st.session_state.get("session_id")
token = st.session_state.get("_token")

pub, outline, video = _load()


def _default_meta() -> dict:
    return {
        "title": str(outline.get("title") or "小剧组漫剧"),
        "description": str(outline.get("logline") or ""),
        "tags": [outline.get("genre"), "AI漫剧"],
    }


meta = _default_meta()

# ---- 元数据编辑区 ----
st.markdown('<div class="lc-label">发布元数据（可编辑）</div>', unsafe_allow_html=True)
m_tab1, m_tab2, m_tab3 = st.tabs(["标题", "描述", "标签"])
with m_tab1:
    meta["title"] = st.text_input("标题", value=meta["title"], key="pub_title")
with m_tab2:
    meta["description"] = st.text_area("描述", value=meta["description"], key="pub_desc", height=80)
with m_tab3:
    tags_str = st.text_input("标签(逗号分隔)", value=",".join([t for t in meta["tags"] if t]),
                             key="pub_tags")
    meta["tags"] = [t.strip() for t in re.split(r"[，,]", tags_str) if t.strip()]

# ---- 平台卡片 + 多选 ----
st.markdown('<div class="lc-divider"></div><div class="lc-label">目标平台</div>', unsafe_allow_html=True)
cols = st.columns(len(PLATFORMS))
selected = []
for i, (pk, name, home) in enumerate(PLATFORMS):
    res = pub.get(pk) or {}
    status = res.get("status") or "draft"
    color = STATUS_COLOR.get(status, "var(--s-muted)")
    check = st.checkbox(name, key=f"pub_sel_{pk}")
    with cols[i % len(cols)]:
        if check:
            selected.append(pk)
        st.markdown(
            f'<div class="lc-home-card" style="min-height:110px;border-top:3px solid {color};">'
            f'<div class="lc-label">{escape(name)}</div>'
            f"<p style='font-size:13px;'>状态：<b style='color:{color};'>{escape(str(STATUS_LABEL.get(status, status)))}</b></p>"
            f"<p style='font-size:12px;color:var(--s-muted);'>{escape(str(res.get('title',''))[:20])}</p></div>",
            unsafe_allow_html=True,
        )
        if res.get("url"):
            st.markdown(f'<a href="{escape(str(res["url"]))}" target="_blank" style="color:{color}">查看链接</a>',
                        unsafe_allow_html=True)
        # OAuth 链接入口
        if st.button(f"连接 {name}", key=f"pub_link_{pk}", use_container_width=True):
            r = backend_link_platform(pk, token)
            st.session_state["pub_msg"] = f"{name} 已连接：{r.get('token','')}"

if st.session_state.get("pub_msg"):
    st.caption(st.session_state["pub_msg"])


def _publish():
    if not sid:
        st.session_state["pub_msg"] = "请先创作一个会话。"
        return
    if not selected:
        st.session_state["pub_msg"] = "请勾选至少一个平台。"
        return
    res = backend_publish(sid, selected, meta, token)
    if res.get("status") == "ok":
        payload = dict(st.session_state.get("payload") or {})
        payload["publish_results"] = res.get("publish_results") or {}
        st.session_state["payload"] = payload
        st.session_state["pub_msg"] = f"已发布 {len(selected)} 个平台"
        st.rerun()
    else:
        st.session_state["pub_msg"] = res.get("message", "发布失败")


st.button(f"🚀 一键发布所有选定平台（{len(selected)}）", on_click=_publish,
          key="pub_launch", use_container_width=True, type="primary", disabled=not selected)

if not video.get("dir_or_path") and not video.get("produced"):
    st.caption("提示：当前会话尚无视频产物，发布结果将显示为「未发布」（draft）。可先到剪辑师页生成视频。")

# 返回部门
st.markdown('<div style="height:16px"></div>', unsafe_allow_html=True)
st.button("← 返回后期合成分发部", key="pub_back", on_click=nav_go,
          args=("pages/20_后期合成分发部.py",), use_container_width=True)

# 让 selected 在回调外可用（st.session_state 存）
st.session_state["pub_selected_platforms"] = selected
