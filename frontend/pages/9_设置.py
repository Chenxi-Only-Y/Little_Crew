"""小剧组 — 设置：模型配置（文字 / 图片 / 视频），每个职业可独立选模型。"""

import pathlib
import sys
from html import escape

import streamlit as st

FRONTEND = pathlib.Path(__file__).resolve().parent.parent
if str(FRONTEND) not in sys.path:
    sys.path.insert(0, str(FRONTEND))

from _api import (backend_settings, backend_settings_update, backend_settings_test,  # noqa: E402
                  backend_cost_audit, backend_cost_reminder, backend_providers,
                  backend_generation_modes)
from _styles import handle_nav, inject_css, render_nav  # noqa: E402

# 时长档位的**唯一来源**在后端（app/services/providers.py）；
# 这里只引入默认值常量，避免前端再抄一份档位列表。
from app.services.providers import DEFAULT_DURATION  # noqa: E402

st.set_page_config(page_title="设置 · 小剧组", layout="wide")
inject_css()
handle_nav()
render_nav(current="settings")

st.markdown(
    '<div class="lc-eyebrow">设置</div>'
    '<div class="lc-title" style="font-size:2rem;">模型配置</div>'
    "<p style='color:var(--s-muted);'>分「文字 / 图片 / 视频」三类；文字模型下每个职业可独立选择。"
    "保存立即生效。选「(自动)」用默认。</p>",
    unsafe_allow_html=True,
)

token = st.session_state.get("_token")
cur = backend_settings(token).get("settings", {})

# ------------------------------------------------------------------
# 第一步：选「云端 API」还是「本地推理」
# ------------------------------------------------------------------
# 设计决定（2026-10-02）：**不默认本地、也不默认云端**。
# 未选时后端会明确报错而不是猜——避免"没部署本地却走本地"或"不知情产生云端费用"。
st.markdown(
    '<div class="lc-eyebrow">第一步 · 必选</div>'
    '<div class="lc-title" style="font-size:1.5rem;">用云端还是本地？</div>',
    unsafe_allow_html=True,
)
st.markdown(
    "<p style='color:var(--s-muted);font-size:13px;margin:0 0 8px 0;'>"
    "这里没有默认值：请显式选择。选完仍可在下方为每个职业单独指定具体提供者。</p>",
    unsafe_allow_html=True,
)

_CAP_LABEL = {
    "llm": "文字模型（策划/编剧/分镜/场记等）",
    "image_gen": "图片生成（角色/场景/摄影师）",
    "video_gen": "视频生成（剪辑师）",
    "lora_train": "LoRA 训练（角色一致性）",
    "tts": "配音（配音指导）",
}
_MODE_LABEL = {"unset": "— 请选择 —", "cloud": "云端 API", "local": "本地推理（零成本）"}

_modes = backend_generation_modes()
if not _modes:
    st.info("后端不可达，无法读取模式设置项。", icon="ℹ️")
else:
    _mode_cols = st.columns(min(3, max(1, len(_modes))))
    for _i, _m in enumerate(_modes):
        with _mode_cols[_i % len(_mode_cols)]:
            _cur_mode = str(cur.get(_m["mode_key"]) or "unset")
            _opts = _m["mode_options"]
            _defaults = _m.get("mode_defaults") or {}
            _hint = "；".join(
                f"{_MODE_LABEL.get(k, k)} → {v}" for k, v in _defaults.items()
            )
            st.selectbox(
                _CAP_LABEL.get(_m["capability"], _m["capability"]),
                _opts,
                index=_opts.index(_cur_mode) if _cur_mode in _opts else 0,
                key=f"mode_{_m['capability']}",
                format_func=lambda k: _MODE_LABEL.get(k, k),
                help=_hint,
            )
    _unset = [
        _CAP_LABEL.get(m["capability"], m["capability"])
        for m in _modes
        if str(st.session_state.get(f"mode_{m['capability']}") or "unset") == "unset"
    ]
    if _unset:
        st.warning(
            "以下能力尚未选择云端/本地，运行到该环节会明确报错：\n\n"
            + "、".join(_unset),
            icon="⚠️",
        )


def _save_modes():
    """保存模式选择。"""
    payload = {}
    for m in _modes:
        v = st.session_state.get(f"mode_{m['capability']}")
        if v:
            payload[m["mode_key"]] = v
    if payload:
        backend_settings_update(payload, token)


st.button("保存模式选择", key="save_modes", on_click=_save_modes, type="primary")
st.markdown('<div class="lc-divider"></div>', unsafe_allow_html=True)


def _cost_line(kind: str, provider: str, model: str, units: float = 1.0):
    """在配置行下方渲染一条"云端/本地"费用提醒（只提醒，不阻止保存）。"""
    if not provider or provider == "(自动)":
        st.markdown(
            "<p style='font-size:11px;color:var(--s-violet-mid);margin:2px 0 0 0;'>"
            "未指定：将按默认提供者执行（用上方「当前生效」总览核对）</p>",
            unsafe_allow_html=True,
        )
        return
    cap = {"llm": "llm", "image": "image_gen", "video": "video_gen"}.get(kind, "image_gen")
    r = backend_cost_reminder(provider, model, cap, units, token)
    level = r.get("level", "unknown")
    if level == "free":
        icon, color = "🟢", "var(--s-lime)"
    elif level in ("cloud", "cloud_subscription"):
        icon, color = "🔴", "#ffb23d"
    else:
        icon, color = "🟡", "var(--s-violet-mid)"
    badges = " · ".join(r.get("badges") or [])
    st.markdown(
        f"<p style='font-size:11px;color:{color};margin:2px 0 0 0;'>"
        f"{icon} {escape(str(r.get('title','')))}"
        f"<span style='color:var(--s-muted);'> · {escape(str(r.get('detail','')))}</span>"
        + (f"<br><span style='color:var(--s-violet-mid);'>{escape(badges)}</span>" if badges else "")
        + "</p>",
        unsafe_allow_html=True,
    )


# ---- 云端使用总览（只提醒，不拦截）----
_audit = backend_cost_audit(token)
if _audit.get("status") == "ok":
    if _audit.get("has_cloud"):
        _cloud = _audit.get("cloud_items") or []
        _lines = "；".join(
            f"{it['key']}={it['provider']}" + ("（你已设置）" if it.get("from_user_setting") else "（默认）")
            for it in _cloud
        )
        st.warning(f"💳 {_audit.get('summary')}\n\n将走云端：{_lines}", icon="💳")
    else:
        st.success(f"🟢 {_audit.get('summary')}", icon="🟢")

# ---- 文字模型：综合默认 + 各职业（每个职业独立选） ----
TEXT_ROWS = [
    ("llm_default", "综合 · 默认", "llm"),
    ("writer", "作家（策划）", "llm"),
    ("critics", "文学策划（剧评）", "llm"),
    ("screenwriter", "编剧", "llm"),
    ("storyboarder", "分镜师", "llm"),
    ("director", "技术指导", "llm"),
    ("photographer", "摄影师", "llm"),
    ("continuity", "场记", "llm"),
]
# ---- 图片模型：按职业拆 ----
IMAGE_ROWS = [
    ("image_default", "图像生成 · 默认", "image"),
    ("character_designer", "角色设计师", "image"),
    ("scene_designer", "场景设计师", "image"),
    ("photographer", "摄影师", "image"),
]
# ---- 视频模型：按职业拆 ----
VIDEO_ROWS = [
    ("video_default", "视频合成 · 默认", "video"),
    ("editor", "剪辑师", "video"),
]

# 提供者选项：**优先从后端注册表取（唯一数据源）**；下面的静态表仅作后端不可达时的兜底。
# 见 docs/recon/02-refactor-plan.md §2.6：provider 清单原先散在 3 处，现已收敛。
_FALLBACK_OPTIONS = {
    "llm": ["(自动)", "deepseek", "zhipu", "openai", "qwen", "moonshot", "claude", "gemini", "doubao", "ernie", "ollama", "vllm", "custom"],
    "image": ["(自动)", "local", "dashscope", "replicate", "minimax"],
    "video": ["(自动)", "ffmpeg", "local", "minimax"],
}
_FALLBACK_LABEL = {
    "deepseek": "DeepSeek", "zhipu": "智谱 GLM", "openai": "OpenAI", "qwen": "阿里通义千问",
    "moonshot": "月之暗面 Kimi", "claude": "Anthropic Claude", "gemini": "Google Gemini",
    "doubao": "字节豆包(火山方舟)", "ernie": "百度文心(千帆)",
    "ollama": "本地 Ollama", "vllm": "本地 vLLM", "custom": "自定义",
    "dashscope": "阿里百炼(DashScope)", "replicate": "Replicate", "minimax": "MiniMax",
    "ffmpeg": "FFmpeg(本地合成)",
    "local": "本地推理 local-gen(零成本)",
}
_FALLBACK_DEFAULT_MODEL = {
    "deepseek": "deepseek-v4-flash", "zhipu": "glm-4", "openai": "gpt-4o-mini",
    "qwen": "qwen-plus", "moonshot": "moonshot-v1-8k", "claude": "claude-3-5-sonnet-latest",
    "gemini": "gemini-1.5-pro", "doubao": "doubao-pro-32k", "ernie": "ernie-4.0-8k",
    "dashscope": "z-image-turbo", "replicate": "sdxl", "minimax": "image-01",
    "ffmpeg": "MiniMax-H3",
}

# 前端「行类别」 -> 后端注册表「能力类别」
_CAP_OF_KIND = {"llm": "llm", "image": "image_gen", "video": "video_gen"}


def _build_provider_tables() -> tuple[dict, dict, dict]:
    """从 `/api/providers` 构建 (options, label, default_model) 三张表。

    后端不可达或某能力为空时，回退到内置静态表，保证设置页始终可用。
    """
    options, label, default_model = {}, {}, {}
    for kind, cap in _CAP_OF_KIND.items():
        items = backend_providers(cap)
        if not items:
            options[kind] = list(_FALLBACK_OPTIONS[kind])
            for k in _FALLBACK_OPTIONS[kind]:
                label.setdefault(k, _FALLBACK_LABEL.get(k, k))
                if k in _FALLBACK_DEFAULT_MODEL:
                    default_model.setdefault(k, _FALLBACK_DEFAULT_MODEL[k])
            continue
        options[kind] = ["(自动)"] + [p["key"] for p in items]
        for p in items:
            label[p["key"]] = p["label"]
            if p.get("default_model"):
                default_model[p["key"]] = p["default_model"]
        label.setdefault("local", _FALLBACK_LABEL["local"])
    return options, label, default_model


PROVIDER_OPTIONS, PROVIDER_LABEL, PROVIDER_DEFAULT_MODEL = _build_provider_tables()

# 行 -> 实际设置键 (provider, model, api_key)
KEYMAP = {
    "llm_default": ("llm_provider", "llm_model", "llm_api_key"),
    "writer": ("llm_provider_writer", "llm_model_writer", "llm_api_key_writer"),
    "critics": ("llm_provider_critics", "llm_model_critics", "llm_api_key_critics"),
    "screenwriter": ("llm_provider_screenwriter", "llm_model_screenwriter", "llm_api_key_screenwriter"),
    "storyboarder": ("llm_provider_storyboarder", "llm_model_storyboarder", "llm_api_key_storyboarder"),
    "director": ("llm_provider_director", "llm_model_director", "llm_api_key_director"),
    "photographer": ("llm_provider_photographer", "llm_model_photographer", "llm_api_key_photographer"),
    "continuity": ("llm_provider_continuity", "llm_model_continuity", "llm_api_key_continuity"),
    "image_default": ("image_provider", "image_model", "image_api_key"),
    "character_designer": ("image_provider_character_designer", "image_model_character_designer", "image_api_key_character_designer"),
    "scene_designer": ("image_provider_scene_designer", "image_model_scene_designer", "image_api_key_scene_designer"),
    "photographer": ("image_provider_photographer", "image_model_photographer", "image_api_key_photographer"),
    "video_default": ("video_provider", "video_model", "video_api_key"),
    "editor": ("video_provider_editor", "video_model_editor", "video_api_key_editor"),
}

ALL_ROWS = TEXT_ROWS + IMAGE_ROWS + VIDEO_ROWS


def row_widgets(row_key, label, kind):
    pk, mk, ak = KEYMAP[row_key]
    prog = PROVIDER_OPTIONS[kind]
    ns = f"{kind}_{row_key}"  # 避免不同 tab 下同名 row（如 photographer）的 widget key 冲突
    c1, c2, c3, c4 = st.columns([2, 2, 2, 3])
    with c1:
        st.caption(label)
    with c2:
        sel_prov = st.selectbox("提供者", prog, key=f"prov_{ns}",
                                index=prog.index(cur.get(pk)) if cur.get(pk) in prog else 0,
                                format_func=lambda k: PROVIDER_LABEL.get(k, k))
    with c3:
        # 选提供者后，模型输入框自动带出该提供者默认模型（用户可改，留空=不用填）
        default_m = PROVIDER_DEFAULT_MODEL.get(sel_prov, "")
        current_m = cur.get(mk, "")
        st.text_input("模型", key=f"model_{ns}",
                      value=current_m or default_m,
                      placeholder=default_m,
                      help="选提供者后会自动填默认模型；留空=用该提供者官方默认")
    with c4:
        # 不回填完整密钥到输入框（避免密钥出现在 DOM）；占位符提示是否已配置。
        has_key = bool(cur.get(ak))
        st.text_input("API Key", key=f"key_{ns}", type="password",
                      value="", placeholder="已配置，留空则沿用" if has_key else "粘贴你的 API Key",
                      help="为安全，这里不回显已保存的密钥；留空=沿用已配置，重新填写=替换")

    # 测试按钮：对该行当前 provider/model/api_key 做连通测试
    if st.button("测试", key=f"test_{ns}"):
        r = backend_settings_test({
            "type": kind, "provider": st.session_state.get(f"prov_{ns}"),
            "model": st.session_state.get(f"model_{ns}", ""),
            "api_key": st.session_state.get(f"key_{ns}", ""),
        }, token)
        st.session_state[f"testres_{ns}"] = r
    res = st.session_state.get(f"testres_{ns}")
    if res:
        ok = res.get("ok")
        et = res.get("error_type", "")
        icon = "✅" if ok else "❌"
        color = "var(--s-lime)" if ok else "#e5484d"
        et_label = {"auth": "鉴权失败", "network": "网络/连接", "model": "模型不存在",
                    "rate_limit": "限流/配额", "unwired": "未接入", "other": "异常"}.get(et, et)
        st.markdown(
            f"<p style='font-size:12px;color:{color};'>"
            f"{icon} {'通畅' if ok else '异常'} · {et_label} · {res.get('detail','')[:120]}</p>",
            unsafe_allow_html=True,
        )

    # 费用提醒：告诉你这一行选的是云端还是本地（只提醒，不阻止保存）
    _cost_line(kind, st.session_state.get(f"prov_{ns}") or "",
               st.session_state.get(f"model_{ns}", "") or "")


def _save():
    payload = {}
    for rk, _label, _kind in ALL_ROWS:
        pk, mk, ak = KEYMAP[rk]
        ns = f"{_kind}_{rk}"
        p = st.session_state.get(f"prov_{ns}")
        m = st.session_state.get(f"model_{ns}")
        k = st.session_state.get(f"key_{ns}")
        if p and p != "(自动)":
            payload[pk] = p
        if m:
            payload[mk] = m
        if k:
            payload[ak] = k
    # 视频参数
    vd = st.session_state.get("video_max_duration")
    if vd:
        payload["video_max_duration"] = int(vd)
    vr = st.session_state.get("video_resolution")
    if vr:
        payload["video_resolution"] = vr
    # 生成数量 / 质量
    mi = st.session_state.get("max_images")
    if mi:
        payload["max_images"] = int(mi)
    vc = st.session_state.get("video_count")
    if vc is not None:
        payload["video_count"] = int(vc)
    iq = st.session_state.get("image_quality")
    if iq:
        payload["image_quality"] = iq
    vq = st.session_state.get("video_quality")
    if vq:
        payload["video_quality"] = vq
    r = backend_settings_update(payload, token)
    st.session_state["set_msg"] = "已保存并生效" if r.get("status") == "ok" else r.get("message", "保存失败")


t1, t2, t3, t4 = st.tabs(["🔤 文字模型", "🖼 图片模型", "🎬 视频模型", "⚙️ 生成数量 / 质量"])

with t1:
    if st.button("保存文字模型", key="save_text", use_container_width=True, type="primary"):
        _save()
    st.markdown('<div class="lc-label">文字模型</div>', unsafe_allow_html=True)
    # 第一行「综合 · 默认」：其下方小字说明「每个职业可选不同模型」
    for rk, label, kind in TEXT_ROWS[:1]:
        row_widgets(rk, label, kind)
    st.markdown(
        "<p style='color:var(--s-muted);font-size:12px;margin:2px 0 12px 0;'>"
        "通用默认模型。每个职业可选不同模型覆盖它；下方各职业行的设置优先于这里（职业模型 > 综合默认）。</p>",
        unsafe_allow_html=True,
    )
    for rk, label, kind in TEXT_ROWS[1:]:
        row_widgets(rk, label, kind)

with t2:
    if st.button("保存图片模型", key="save_image", use_container_width=True, type="primary"):
        _save()
    st.markdown('<div class="lc-label">图片模型 · 按职业可选</div>', unsafe_allow_html=True)
    for rk, label, kind in IMAGE_ROWS[:1]:
        row_widgets(rk, label, kind)
    st.markdown(
        "<p style='color:var(--s-muted);font-size:12px;margin:2px 0 12px 0;'>"
        "通用默认图片模型。角色设计师 / 场景设计师 / 摄影师可各选不同模型（职业 > 默认）。</p>",
        unsafe_allow_html=True,
    )
    for rk, label, kind in IMAGE_ROWS[1:]:
        row_widgets(rk, label, kind)

with t3:
    if st.button("保存视频模型", key="save_video", use_container_width=True, type="primary"):
        _save()
    st.markdown('<div class="lc-label">视频模型 · 按职业可选</div>', unsafe_allow_html=True)
    for rk, label, kind in VIDEO_ROWS[:1]:
        row_widgets(rk, label, kind)
    st.markdown(
        "<p style='color:var(--s-muted);font-size:12px;margin:2px 0 12px 0;'>"
        "通用默认视频模型。剪辑师可独立选择（职业 > 默认）。选 MiniMax 即用图生视频（每段≤15s、≤768P）。</p>",
        unsafe_allow_html=True,
    )
    # 视频参数：每段时长 / 分辨率
    # 档位与上限**由后端 provider 声明**（app/services/providers.py）：
    #   本地 8GB 只到 9s（超过 10s 必 OOM），云端可到 15s；1080P 两个提供者都不支持。
    _vprov = str(cur.get("video_provider") or st.session_state.get("prov_video_video_default") or "minimax")
    _vopts = backend_providers("video_gen")
    _spec = next((p for p in _vopts if p["key"] == _vprov), None)
    _presets = (_spec or {}).get("presets") or {}
    _durations = _presets.get("duration") or [5, 7, 9, 12, 15]
    _resolutions = _presets.get("resolution") or ["768P", "2K"]

    c_d, c_r = st.columns(2)
    with c_d:
        _cur_d = int(cur.get("video_max_duration") or DEFAULT_DURATION)
        if _cur_d not in _durations:
            _cur_d = max([d for d in _durations if d <= _cur_d] or _durations)
        st.selectbox(
            "每段时长（秒）", _durations, key="video_max_duration",
            index=_durations.index(_cur_d),
            format_func=lambda v: f"{v} 秒" + ("（默认）" if v == DEFAULT_DURATION else ""),
            help="分辨率不影响时长上限；本地 8GB 超过 10s 必 OOM（降分辨率无效）",
        )
        st.caption(
            f"「{_vprov}」可选：{'/'.join(str(d) for d in _durations)} 秒"
            + ("　（本地上限 10s，12/15 已隐藏）" if "local" == _vprov else "")
        )
    with c_r:
        _cur_r = str(cur.get("video_quality") or cur.get("video_resolution") or _resolutions[0]).upper()
        if _cur_r not in _resolutions:
            _cur_r = _resolutions[0]
        st.selectbox("视频分辨率", _resolutions, key="video_quality",
                     index=_resolutions.index(_cur_r))
        st.caption(f"「{_vprov}」可选：{'/'.join(_resolutions)}（1080P 提供者不支持）")
    for rk, label, kind in VIDEO_ROWS[1:]:
        row_widgets(rk, label, kind)

with t4:
    if st.button("保存生成参数", key="save_gen", use_container_width=True, type="primary"):
        _save()
    st.markdown('<div class="lc-label">生成数量 / 质量（与模型配置平级）</div>', unsafe_allow_html=True)
    st.markdown(
        "<p style='color:var(--s-muted);font-size:12px;margin:2px 0 12px 0;'>"
        "控制一次生成多少图/视频、以及图/视频质量。调小数量、调低质量能明显提速与降成本。</p>",
        unsafe_allow_html=True,
    )
    qc1, qc2 = st.columns(2)
    with qc1:
        st.number_input("图片数量（全片最多镜头/图数）", min_value=1, max_value=20, step=1,
                        key="max_images", value=int(cur.get("max_images") or 8))
        st.number_input("视频数量（每张图生成一段，0=跟图片数）", min_value=0, max_value=20, step=1,
                        key="video_count", value=int(cur.get("video_count") or 0))
    with qc2:
        st.selectbox("图片质量", ["standard", "high", "ultra"], key="image_quality",
                     index=["standard", "high", "ultra"].index(cur.get("image_quality") or "standard"),
                     format_func=lambda k: {"standard": "标准", "high": "高清", "ultra": "超清"}.get(k, k))
        st.selectbox("视频质量", ["768P", "1080P", "2K"], key="video_quality",
                     index=["768P", "1080P", "2K"].index(cur.get("video_quality") or "768P"))

if st.session_state.get("set_msg"):
    st.info(st.session_state["set_msg"])
