"""小剧组 — 统一职业控制台脚手架。

把所有职业页共用的骨架（输入区 + 分段卡片选择 + 启动/轮询 + 进度 + 审核）抽到一处，
各职业页只需声明自己的：标题/描述/起点环节/结果渲染器，然后调用 render_console_v2()。

这样 13 个职业页都能带「AI 执行到哪一环」的可选终点，且交互/样式一致。
"""

import sys
import pathlib
import uuid
import time
import json
import base64
from html import escape

import streamlit as st

FRONTEND = pathlib.Path(__file__).resolve().parent.parent
if str(FRONTEND) not in sys.path:
    sys.path.insert(0, str(FRONTEND))

from _api import (backend_run, backend_status, backend_history, backend_available,  # noqa: E402
                  local_run, backend_resume, backend_asset_upload, backend_cancel, backend_video)
from _segment_select import render_segment_select  # noqa: E402
from _styles import handle_nav, inject_css, nav_go, render_nav  # noqa: E402

# 环节 -> 阶段进度顺序
_ALL_PHASES = ["story_planner", "character_designer", "consistency_auditor", "screenwriter",
               "critics", "storyboarder", "rhythm_director", "continuity_checker",
               "prompt_compiler", "image_generator", "quality_assessor", "aggregator"]
_STEP_LABELS = ["作家", "角色", "审核", "编剧", "剧评", "分镜", "节奏", "场记",
                "提示词", "画面", "质量", "聚合"]
_PHASE_TO_STEP = {p: i for i, p in enumerate(_ALL_PHASES)}

# ---------------- 「跟随跳转」：环节/确认点 -> 职业页 ----------------
_ROLE_LABEL = {
    "writer": "策划总监", "critics": "文学策划", "screenwriter": "编剧",
    "character_designer": "角色设计师", "scene_designer": "场景设计师",
    "storyboarder": "分镜师", "rhythm_director": "剪辑指导", "continuity_checker": "场记",
    "director": "技术指导", "photographer": "摄影师", "quality_assessor": "质检总监",
    "editor": "剪辑师", "distributor": "发行总监",
}
_ROLE_TO_PAGE = {
    "writer": "pages/01_策划总监.py", "critics": "pages/02_文学策划.py",
    "screenwriter": "pages/03_编剧.py", "character_designer": "pages/04_角色设计师.py",
    "scene_designer": "pages/05_场景设计师.py", "storyboarder": "pages/06_分镜师.py",
    "rhythm_director": "pages/07_剪辑指导.py", "continuity_checker": "pages/08_场记.py",
    "director": "pages/09_技术指导.py", "photographer": "pages/10_摄影师.py",
    "quality_assessor": "pages/11_质检总监.py", "editor": "pages/12_剪辑师.py",
    "distributor": "pages/13_发行总监.py",
}
_PHASE_ROLE = {
    "writer": "writer", "critics": "critics", "screenwriter": "screenwriter",
    "character_designer": "character_designer", "scene_designer": "scene_designer",
    "storyboarder": "storyboarder", "rhythm_director": "rhythm_director",
    "continuity_checker": "continuity_checker", "director": "director",
    "photographer": "photographer", "quality_assessor": "quality_assessor",
}
_REVIEW_ROLE = {1: "screenwriter", 2: "character_designer", 3: "storyboarder", 4: "photographer"}

# 可勾选的人工检查点（职业 key -> 展示名）
_CHECKPOINT_OPTIONS = {
    "critics": "文学策划·剧评检查",
    "script": "编剧·剧本检查",
    "assets": "角色/场景·资产检查",
    "storyboard": "分镜师·分镜检查",
    "rhythm": "剪辑指导·节奏检查",
    "continuity": "场记·连贯性检查",
    "prompts": "技术指导·提示词检查",
    "images": "摄影师·图片检查",
    "quality": "质检总监·质量检查",
}


def _phase_frac(phase: str) -> float:
    return (_ALL_PHASES.index(phase) + 1) / len(_ALL_PHASES) if phase in _ALL_PHASES else 0.05


def _resume(sid, decision, feedback):
    backend_resume(sid, decision, feedback, st.session_state.get("_token"))
    st.session_state["status"] = "running"
    st.session_state["poll_count"] = 0


def _cancel_run(sid):
    backend_cancel(sid, st.session_state.get("_token"))
    st.session_state["status"] = "cancelled"


def _resume_edit(sid, edit_key):
    raw = st.session_state.get(edit_key, "")
    import json as _json
    try:
        content = _json.loads(raw)
    except Exception:
        content = {"raw": raw}
    backend_resume(sid, "approve", {}, st.session_state.get("_token"), content)
    st.session_state["status"] = "running"
    st.session_state["poll_count"] = 0


def _parse_field(raw: str, field: str):
    """把用户粘贴的「已有产物」文本解析为 seed 值。

    优先按 JSON 解析；解析失败则对 dict/list 类字段回退为原始文本。
    图片字段（images/reference_images）若为空则返回空。
    """
    import json

    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # 非 JSON：对 story_outline/script 等 dict 类字段，尝试当文本包成对象
    if field in ("story_outline", "script"):
        return {"raw": raw}
    return raw


def _render_existing(role_key: str, existing_labels: dict) -> None:
    """渲染「已有产物(可选)」折叠区；每项一个 text_area，值将作为 seed[field]。"""
    with st.expander("已有产物（可选，填了则跳过该环节重新生成）", expanded=False):
        for field, label in existing_labels.items():
            st.markdown(f'<div class="lc-label">{label}</div>', unsafe_allow_html=True)
            st.text_area(f"{label} (JSON)", key=f"ex_{field}", height=90,
                         placeholder=f"粘贴已有的 {label} JSON，留空则由 AI 生成")


def _shape_image_value(field: str, items: list) -> object:
    """把上传得到的 [{name,path}] 组装成对应字段的 seed 值形状。

    - images（摄影师画面）：镜头 dict 列表 [{shot_id, local_path, image_url, status}]
    - 参考图字段（角色/场景）：{原始文件名: 本地路径}
    - 其它：路径列表
    """
    if field == "images":
        return [{"shot_id": f"U{i + 1}-up", "local_path": it["path"], "image_url": it["path"],
                 "status": "ok"} for i, it in enumerate(items)]
    if field in ("character_reference_images", "scene_reference_images"):
        return {it["name"]: it["path"] for it in items}
    return [it["path"] for it in items]


def _render_image_uploads(role_key: str, field: str, label: str) -> None:
    """渲染「图片资产上传」：文件上传 → 后端保存到本地路径，组装成 seed[field]。

    已上传的 [{name,path}] 保存在 st.session_state[f"up_items_{field}"]，按文件名去重防重复上传。
    """
    up_key = f"up_{field}"
    files = st.file_uploader(f"{label}（上传后保存到本地路径，可多选）",
                             type=["png", "jpg", "jpeg", "webp"], accept_multiple_files=True, key=up_key)
    items = st.session_state.setdefault(f"up_items_{field}", [])
    if files:
        seen = st.session_state.setdefault(f"seen_{field}", [])
        for f in files:
            if f.name in seen:
                continue
            try:
                data = base64.b64encode(f.getvalue()).decode()
            except Exception:
                continue
            ext = f.name.rsplit(".", 1)[-1].lower() if "." in f.name else "png"
            resp = backend_asset_upload(f.name.rsplit(".", 1)[0], data, ext, st.session_state.get("_token"))
            if resp.get("status") == "ok":
                items.append({"name": resp.get("name") or f.name.rsplit(".", 1)[0], "path": resp.get("path")})
            seen.append(f.name)
    if items:
        st.caption(f"已上传 {len(items)} 张，将注入「{label}」：")
        for it in items:
            st.code(it["path"], language=None)


def _current_role(status: str, phase: str, payload: dict) -> str | None:
    """当前正在处理/停靠的职业 role。确认点优先按 review_point 映射。"""
    if status == "review":
        return _REVIEW_ROLE.get((payload or {}).get("review_point"))
    if status == "running":
        return _PHASE_ROLE.get(phase)
    return None


def _goto_role_page(role: str, sid: str) -> None:
    """「去该职业页」回调：记录跟随会话 + 设置导航目标。"""
    st.session_state["_follow_sid"] = sid
    nav_go(_ROLE_TO_PAGE.get(role, "streamlit_app.py"))


def _render_jump_bar(sid: str, status: str, phase: str, payload: dict, current_key: str = "") -> None:
    """运行中/确认点：显示「当前环节：X」+「去该职业页」按钮。已在对应页则不显示。"""
    role = _current_role(status, phase, payload)
    if not role or role == current_key:
        return
    label = _ROLE_LABEL.get(role, role)
    page = _ROLE_TO_PAGE.get(role)
    st.markdown(
        '<div class="lc-card" style="padding:10px 18px;">'
        f'<div class="lc-label">当前环节 · {label}</div>'
        f"<p style='color:var(--s-muted);font-size:12px;'>阶段：{phase or status}</p></div>",
        unsafe_allow_html=True,
    )
    if page:
        st.button(f"去「{label}」职业页 →", key=f"jump_{sid}_{role}", on_click=_goto_role_page,
                  args=(role, sid), use_container_width=True)


def _render_thinking_panel(thinking_entries, payload: dict) -> None:
    """「AI 思考过程」可展开面板：思考日志 + 中间产物链路。"""
    entries = list(thinking_entries or [])
    if not entries:
        entries = list((payload or {}).get("thinking_log") or [])
    with st.expander("🧠 AI 思考过程（零额外 token）", expanded=False):
        if entries:
            for e in entries:
                st.markdown(f"**{e.get('label', '')}**：{e.get('text', '')}")
        else:
            st.caption("思考日志生成中…")
    if payload:
        _render_products(payload)


def _render_products(payload: dict) -> None:
    """中间产物链路（大纲→剧本→分镜→提示词→资产）。"""
    items = []
    if payload.get("story_outline"):
        items.append(("故事大纲", payload["story_outline"]))
    if payload.get("script"):
        items.append(("剧本", payload["script"]))
    if payload.get("script_review"):
        items.append(("剧评", payload["script_review"]))
    if payload.get("critic_report"):
        items.append(("文学策划报告", payload["critic_report"]))
    if payload.get("character_profiles"):
        items.append(("角色描述", payload["character_profiles"]))
    if payload.get("scene_design_profiles"):
        items.append(("场景描述", payload["scene_design_profiles"]))
    if payload.get("storyboard"):
        items.append(("分镜", payload["storyboard"]))
    if payload.get("rhythm_plan"):
        items.append(("节奏计划", payload["rhythm_plan"]))
    if payload.get("continuity_report"):
        items.append(("场记/连贯性", payload["continuity_report"]))
    if payload.get("prompts"):
        items.append(("提示词", payload["prompts"]))
    if payload.get("quality_report"):
        items.append(("质检报告", payload["quality_report"]))
    with st.expander("📦 中间产物链路", expanded=False):
        if not items:
            st.caption("暂无中间产物。")
        for label, val in items:
            st.markdown(f"**{label}**")
            if isinstance(val, (dict, list)):
                st.json(val)
            else:
                st.code(str(val))


def _render_active_run(token, render_review, render_result, role_key: str, dept_page: str) -> None:
    """渲染当前会话的运行状态（进度/确认点/结果）+ 跟随跳转条 + 返回部门。"""
    sid = st.session_state.get("session_id")
    if not sid:
        return
    s = backend_status(sid, token) if backend_available() else {"status": "error", "message": "后端不可达"}
    status = s.get("status", "error")
    phase = s.get("phase", "")
    payload = s.get("payload") or {}
    st.session_state["status"] = status
    st.session_state["phase"] = phase
    st.session_state["payload"] = payload
    st.session_state["poll_count"] = st.session_state.get("poll_count", 0) + 1

    # 「AI 思考过程」面板（实时思考 + 中间产物）
    _render_thinking_panel(s.get("thinking"), payload)

    if status == "running":
        _render_progress(phase)
        _render_jump_bar(sid, status, phase, payload, role_key)
        if st.button("⏹ 打断（停止生成）", key=f"cancel_{sid}", use_container_width=True,
                     on_click=_cancel_run, args=(sid,)):
            pass
        if st.session_state["poll_count"] > 3000:   # ≈60min，真实模型(文生图~30s-1min/张)整体很慢
            st.error("生成超时（>60 分钟）。可点「打断」停止。")
        else:
            time.sleep(1.2)
            st.rerun()
    elif status == "review":
        _render_jump_bar(sid, status, phase, payload, role_key)
        if render_review:
            render_review(payload)
        else:
            _generic_review(payload)
    elif status == "error":
        st.error("生成出错：" + str(payload.get("error", payload)))
    elif status == "cancelled":
        st.warning("⏹ 已中途打断。当前产物保留，可在「我的创作」查看或重新开始。")
        _render_jump_bar(sid, status, phase, payload, role_key)
    elif status == "done":
        st.markdown('<div class="lc-divider"></div>', unsafe_allow_html=True)
        if render_result:
            render_result(payload)
        else:
            st.json(payload)
        # 任何展示"已完成且出图"的页面都提供「生成视频」
        if payload.get("images"):
            n_img = len(payload["images"]); n_ok = sum(1 for i in payload["images"] if i.get("status") == "ok")
            st.caption(f"共 {n_img} 张图（成功 {n_ok}）。点击生成视频走 MiniMax-H3 图生视频（后台，约 20-30 分钟，计费）。")
            if st.button("🎬 生成视频", key=f"mkv_{sid}"):
                r = backend_video(sid, st.session_state.get("_token")) or {}
                st.session_state["payload"] = None
                if r.get("status") in ("started", "ok"):
                    st.session_state[f"vmsg_{sid}"] = "✅ 视频已在后台开始生成，可先操作其它功能，稍后刷新查看结果。"
                else:
                    st.session_state[f"vmsg_{sid}"] = "⚠️ " + str(r.get("message", "视频生成失败"))
                st.rerun()
              

    if status == "done":
        if st.session_state.get(f"vmsg_{sid}"):
            st.success(st.session_state[f"vmsg_{sid}"])
        elif payload.get("video", {}).get("produced") == 1:
            st.success("视频已生成：" + str(payload["video"].get("dir_or_path")))
        elif payload.get("video", {}).get("error"):
            st.error("视频生成失败：" + str(payload["video"].get("error")))

    st.markdown('<div style="height:16px"></div>', unsafe_allow_html=True)
    st.button("← 返回部门", key=f"back_{role_key}", on_click=nav_go, args=(dept_page,),
              use_container_width=True)


def render_console_v2(role_key: str, start_key: str, eyebrow: str, title: str, subtitle: str,
                      input_label: str = "", placeholder: str = "", input_height: int = 100,
                      render_result=None, render_review=None, dept_page: str = "streamlit_app.py",
                      existing_labels: dict | None = None, image_fields: dict | None = None,
                      art_brief_label: str = ""):
    """渲染一个标准职业控制台页。

    Args:
        role_key: 该职业的唯一 key（用于 session 键）
        start_key: 该职业的环节起点（分段起点固定）
        eyebrow/title/subtitle: 页头
        input_label/placeholder: 输入区（可为空则隐藏输入框）
        render_result: 回调 result(payload) 渲染结果卡
        render_review: 可选回调 review(review) 自定义审核；缺省用通用审核
        dept_page: 返回部门的页路径
        existing_labels: {seed_field: 展示名} — 生成「已有产物(可选)」折叠区，
            每项从 text_area 读取并作为 seed[seed_field] 注入（跳过该环节重复生成）。
        image_fields: {seed_field: 展示名} — 生成「图片资产上传」区，文件上传后保存到本地路径并按字段
            形状（images→镜头 dict 列表；参考图→{名:路径}）作为 seed[seed_field] 注入（跳过该环节重新生成画面）。
        art_brief_label: 展示名（非空则显示「美术设定包」自由文本输入，作为 art_brief 注入）。
            用于美术资产部独立生成：无剧本时，AI 据此解析并设计角色/场景。
    """
    st.set_page_config(page_title=f"{title} · 小剧组", layout="wide")
    inject_css()
    handle_nav()
    render_nav(current="create")

    st.markdown(
        f'<div class="lc-eyebrow">{eyebrow}</div>'
        f'<div class="lc-title" style="font-size:2.2rem;">{title}</div>'
        f"<p style='color:var(--s-muted);'>{subtitle}</p>",
        unsafe_allow_html=True,
    )

    st.session_state.setdefault("session_id", None)
    st.session_state.setdefault("status", "idle")
    st.session_state.setdefault("phase", "")
    st.session_state.setdefault("payload", None)
    st.session_state.setdefault("poll_count", 0)

    # 跟随模式：从其它页点「去该职业页」跳来，直接跟显该会话运行，不再显示新建表单
    if st.session_state.get("_follow_sid"):
        st.session_state["session_id"] = st.session_state["_follow_sid"]
        if st.button("✕ 退出跟随 · 在本页开始新创作", key=f"exit_follow_{role_key}", use_container_width=True):
            st.session_state.pop("_follow_sid", None)
            st.rerun()
        _render_active_run(st.session_state.get("_token"), render_review, render_result, role_key, dept_page)
        return

    # 输入区 + 美术设定包（同一张卡）
    if input_label or art_brief_label:
        st.markdown('<div class="lc-card">', unsafe_allow_html=True)
        if input_label:
            st.markdown(f'<div class="lc-label">{input_label}</div>', unsafe_allow_html=True)
            st.text_area(input_label, key=f"in_{role_key}", placeholder=placeholder, height=input_height)
        if art_brief_label:
            st.markdown(f'<div class="lc-label" style="margin-top:6px;">{art_brief_label}</div>',
                        unsafe_allow_html=True)
            st.text_area(art_brief_label, key="in_art_brief", height=90,
                         placeholder="例：白发剑客，戴斗笠，手持青锋长剑；青蛇化作的少女；竹林雨夜客栈（无剧本时据此设计角色/场景）")

    # 分段：起点固定为本页环节，终点可选
    end_stage = render_segment_select(role_key=role_key, start_key=start_key)

    if input_label or art_brief_label:
        st.markdown('</div>', unsafe_allow_html=True)

    # 「已有产物(可选)」折叠区：用户提供已有产物则作为 seed 注入，跳过该环节
    if existing_labels:
        _render_existing(role_key, existing_labels)

    # 「图片资产上传」区：文件上传→本地路径，注入 seed[field]
    if image_fields:
        st.markdown('<div class="lc-card">', unsafe_allow_html=True)
        for field, label in image_fields.items():
            st.markdown(f'<div class="lc-label">{label}</div>', unsafe_allow_html=True)
            _render_image_uploads(role_key, field, label)
        st.markdown('</div>', unsafe_allow_html=True)

    def _start():
        token = st.session_state.get("_token")
        seed = {"start_stage": start_key}
        if end_stage:
            seed["end_stage"] = end_stage
        # 注入人工检查点（勾选的职业才暂停审核；空列表=全部取消→自动放行；未设置=默认全开）
        cps = st.session_state.get(f"cp_{role_key}")
        if cps is not None:
            seed["enabled_checkpoints"] = list(cps)
        # 注入美术设定包（若填了）
        brief = st.session_state.get("in_art_brief", "")
        if brief and brief.strip():
            seed["art_brief"] = brief.strip()
        # 注入已有产物（若填了）
        if existing_labels:
            for field, _label in existing_labels.items():
                raw = st.session_state.get(f"ex_{field}", "")
                if raw and raw.strip():
                    parsed = _parse_field(raw, field)
                    if parsed is not None:
                        seed[field] = parsed
        # 注入图片资产（若已上传）
        if image_fields:
            for field in image_fields:
                items = st.session_state.get(f"up_items_{field}", [])
                if items:
                    seed[field] = _shape_image_value(field, items)
        raw = st.session_state.get(f"in_{role_key}", "")
        sid = uuid.uuid4().hex[:8]
        st.session_state.pop("_follow_sid", None)
        st.session_state["session_id"] = sid
        st.session_state["status"] = "running"
        st.session_state["phase"] = "start"
        st.session_state["payload"] = None
        st.session_state["poll_count"] = 0
        if backend_available():
            payload = backend_run(raw, sid, token, seed)
            if payload is not None and payload.get("status") == "accepted":
                return
        stub = local_run(raw, sid, seed)
        st.session_state["payload"] = stub["payload"]
        st.session_state["phase"] = "done"
        st.session_state["status"] = "done"

    # 人工检查点选择：执行前勾选，只对勾选的职业暂停审核
    _cp_keys = list(_CHECKPOINT_OPTIONS.keys())
    st.multiselect("🔍 人工检查点（勾选才暂停审核；取消则自动放行）", options=_cp_keys,
                   default=_cp_keys, key=f"cp_{role_key}",
                   format_func=lambda k: _CHECKPOINT_OPTIONS.get(k, k))

    st.button("⚡ 让 AI 执行", on_click=_start, type="primary", key=f"btn_{role_key}",
              use_container_width=True)

    # 渲染本次会话的运行状态（进度/确认点/结果）+ 跟随跳转条
    _render_active_run(st.session_state.get("_token"), render_review, render_result, role_key, dept_page)


def _render_progress(phase: str):
    st.progress(min(1.0, _phase_frac(phase)), text="生成中 · " + str(phase))
    idx = _PHASE_TO_STEP.get(phase, -1)
    cells = "".join(
        f'<div class="lc-step lc-step-{"done" if i < idx else "cur" if i == idx else "wait"}">{s}</div>'
        for i, s in enumerate(_STEP_LABELS))
    st.markdown('<div class="lc-steps">' + cells + '</div>', unsafe_allow_html=True)


def _render_asset_review(content: dict) -> None:
    """资产确认：把角色/场景参考图渲染成图片卡片（不再显示代码）。"""
    chars = content.get("characters") or []
    scenes = content.get("scenes") or []
    if chars:
        st.markdown('<div class="lc-label">角色参考图</div>', unsafe_allow_html=True)
        cols = st.columns(min(4, max(1, len(chars))))
        for i, c in enumerate(chars):
            with cols[i % len(cols)]:
                ref = c.get("reference_image") or c.get("local_path") or c.get("image_url") or ""
                if ref:
                    try:
                        st.image(ref, width=150)
                    except Exception:
                        pass
                st.markdown(f"**{c.get('name', '')}**")
                st.caption((c.get("appearance") or "")[:60])
    if scenes:
        st.markdown('<div class="lc-label">场景参考图</div>', unsafe_allow_html=True)
        cols = st.columns(min(3, max(1, len(scenes))))
        for i, s in enumerate(scenes):
            with cols[i % len(cols)]:
                ref = s.get("reference_image") or s.get("local_path") or s.get("image_url") or ""
                if ref:
                    try:
                        st.image(ref, width=180)
                    except Exception:
                        pass
                st.markdown(f"**{s.get('scene_id', '')}**")
                st.caption((s.get("setting") or "")[:60])
    score = content.get("consistency_score")
    if score is not None:
        st.caption(f"一致性评分：{score}")
    if content.get("suggestions"):
        st.caption("建议： " + "；".join(str(x) for x in content["suggestions"])[:150])


def _generic_review(review: dict):
    rp = review.get("review_point")
    title = review.get("title", "审核")
    st.markdown(f'<div class="lc-card"><div class="lc-label">确认点 {rp} · {title}</div></div>',
                unsafe_allow_html=True)
    content = review.get("content")
    # 资产确认：content={"characters":[...], "scenes":[...], "consistency_score", "suggestions"}
    if isinstance(content, dict) and (content.get("characters") or content.get("scenes")):
        _render_asset_review(content)
    else:
        is_img = isinstance(content, list) and content and all(isinstance(x, dict) for x in content) \
            and any(x.get("local_path") or x.get("image_url") for x in content)
        if is_img:
            cols = st.columns(min(4, max(1, len(content))))
            for i, im in enumerate(content):
                with cols[i % len(cols)]:
                    try:
                        st.image(im.get("local_path") or im.get("image_url"))
                    except Exception:
                        pass
        else:
            st.markdown(f'<div class="lc-card"><pre>{escape(json.dumps(content, ensure_ascii=False, indent=2))}</pre></div>',
                        unsafe_allow_html=True)
    st.text_area("重做反馈（可选）", key=f"fb_{st.session_state.get('session_id','x')}", placeholder="例如：更明亮……")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.button("通过", on_click=lambda: _resume(st.session_state.get("session_id"), "approve", {}),
                  use_container_width=True, type="primary")
    with c2:
        st.button("重做并反馈", on_click=lambda: _resume(st.session_state.get("session_id"), "redo",
                  {"note": st.session_state.get(f"fb_{st.session_state.get('session_id','x')}", "")}),
                  use_container_width=True)
    with c3:
        st.button("← 返回上一步", on_click=lambda: _resume(st.session_state.get("session_id"), "back", {}),
                  use_container_width=True)

    # 编辑后继续：把 content 以 JSON 展示，用户可改后直接继续
    _edit_key = f"edit_{st.session_state.get('session_id','x')}"
    with st.expander("✏️ 编辑后继续（直接改内容）", expanded=False):
        try:
            _prefill = json.dumps(content, ensure_ascii=False, indent=2)
        except Exception:
            _prefill = str(content or "")
        st.text_area("编辑内容（JSON）", key=_edit_key, height=200, value=_prefill)
        sid = st.session_state.get("session_id")
        if st.button("💾 用修改后的内容继续", key=f"save_edit_{sid}",
                     on_click=_resume_edit, args=(sid, _edit_key), use_container_width=True):
            pass
