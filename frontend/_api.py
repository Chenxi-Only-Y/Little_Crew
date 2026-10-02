"""小剧组 — 后端 / 本地 数据来源统一封装（含用户认证 token 透传）。"""

import os
import pathlib
import sys

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 后端地址：Docker 内通过 FRONTEND_API=http://api:8000 注入，本机默认本机端口
API = os.environ.get("FRONTEND_API", "http://127.0.0.1:8000")

# 非本机、且为带点的外部域名时才警示明文 HTTP（Docker 内部 service 名如 "api" 属内网，不误报）。
_API_HOST = API.split("//")[-1].split(":")[0]
if (API.lower().startswith("http://")
        and _API_HOST not in ("127.0.0.1", "localhost")
        and "." in _API_HOST):
    print(f"[warn] FRONTEND_API={API} 为明文 HTTP 且非本机，token/密钥将以明文传输，建议改用 https。")


def _auth_headers(token: str | None) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def backend_available() -> bool:
    try:
        requests.get(f"{API}/health", timeout=3)
        return True
    except requests.RequestException:
        return False


# ------------------- 认证 -------------------
def auth_register(username: str, password: str) -> dict:
    try:
        r = requests.post(f"{API}/api/auth/register", json={"username": username, "password": password}, timeout=15)
        return r.json()
    except requests.RequestException:
        return {"status": "error", "message": "后端不可达"}


def auth_login(username: str, password: str) -> dict:
    try:
        r = requests.post(f"{API}/api/auth/login", json={"username": username, "password": password}, timeout=15)
        return r.json()
    except requests.RequestException:
        return {"status": "error", "message": "后端不可达"}


# ------------------- 后端调用（带 token） -------------------
def backend_run(user_input: str, session_id: str, token: str | None = None, seed: dict | None = None) -> dict | None:
    try:
        body = {"user_input": user_input, "session_id": session_id}
        if seed:
            body["seed"] = seed
        r = requests.post(f"{API}/api/crew/run", json=body, headers=_auth_headers(token), timeout=30)
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        return None


def backend_resume(session_id: str, decision: str, feedback: dict, token: str | None = None,
                   content: dict | None = None) -> dict | None:
    try:
        body = {"session_id": session_id, "decision": decision, "feedback": feedback}
        if content is not None:
            body["content"] = content
        r = requests.post(f"{API}/api/crew/resume", json=body, headers=_auth_headers(token), timeout=30)
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        return None


def backend_status(session_id: str, token: str | None = None) -> dict:
    try:
        r = requests.get(f"{API}/api/crew/status/{session_id}", headers=_auth_headers(token), timeout=15)
        return r.json()
    except requests.RequestException:
        return {"status": "error", "message": "后端不可达"}


def backend_cancel(session_id: str, token: str | None = None) -> dict:
    """中途打断：停止正在生成的会话。"""
    try:
        r = requests.post(f"{API}/api/crew/cancel/{session_id}", headers=_auth_headers(token), timeout=15)
        return r.json()
    except requests.RequestException:
        return {"status": "error", "message": "后端不可达"}


def backend_history(user_id: str, token: str | None = None) -> list:
    try:
        r = requests.get(f"{API}/api/crew/history/{user_id}", headers=_auth_headers(token), timeout=15)
        return r.json().get("items", [])
    except requests.RequestException:
        return []


def backend_history_delete(session_id: str, token: str | None = None) -> dict:
    try:
        r = requests.delete(f"{API}/api/crew/history/{session_id}", headers=_auth_headers(token), timeout=15)
        return r.json()
    except requests.RequestException:
        return {"status": "error", "message": "后端不可达"}


def backend_video(session_id: str, token: str | None = None) -> dict:
    try:
        r = requests.post(f"{API}/api/crew/video/{session_id}", headers=_auth_headers(token), timeout=180)
        return r.json()
    except requests.RequestException:
        return {"status": "error", "message": "后端不可达"}


def backend_reassess(session_id: str, token: str | None = None) -> dict:
    """对已有会话重新评估质量/一致性，返回更新后的 quality_report。"""
    try:
        r = requests.post(f"{API}/api/crew/reassess/{session_id}", headers=_auth_headers(token), timeout=60)
        return r.json()
    except requests.RequestException:
        return {"status": "error", "message": "后端不可达"}


def backend_retry(session_id: str, shot_ids: list, token: str | None = None) -> dict:
    """精确重排指定镜头（其它保持原图），返回更新后的 quality_report。"""
    try:
        r = requests.post(f"{API}/api/crew/retry/{session_id}", json={"shot_ids": shot_ids},
                          headers=_auth_headers(token), timeout=120)
        return r.json()
    except requests.RequestException:
        return {"status": "error", "message": "后端不可达"}


def backend_review(session_id: str, token: str | None = None) -> dict:
    """对已有会话的剧本重新评审，返回更新后的 critic_report。"""
    try:
        r = requests.post(f"{API}/api/crew/review/{session_id}", headers=_auth_headers(token), timeout=60)
        return r.json()
    except requests.RequestException:
        return {"status": "error", "message": "后端不可达"}


def backend_review_history(session_id: str, token: str | None = None) -> dict:
    """读取某会话持久化的剧评/修改历史（跨会话）。"""
    try:
        r = requests.get(f"{API}/api/crew/review/history/{session_id}", headers=_auth_headers(token), timeout=15)
        return r.json()
    except requests.RequestException:
        return {"status": "error", "message": "后端不可达"}


def backend_scene(session_id: str, scene_ids: list | None = None, token: str | None = None) -> dict:
    """重新生成场景氛围参考图；scene_ids 为空则重做全部。"""
    try:
        r = requests.post(f"{API}/api/crew/scene/{session_id}", json={"scene_ids": scene_ids or []},
                          headers=_auth_headers(token), timeout=60)
        return r.json()
    except requests.RequestException:
        return {"status": "error", "message": "后端不可达"}


def backend_rhythm(session_id: str, overrides: dict | None = None, token: str | None = None) -> dict:
    """重新规划镜头节奏；overrides 为 {shot_id: 时长} 手动调整。"""
    try:
        r = requests.post(f"{API}/api/crew/rhythm/{session_id}", json={"overrides": overrides or {}},
                          headers=_auth_headers(token), timeout=60)
        return r.json()
    except requests.RequestException:
        return {"status": "error", "message": "后端不可达"}


def backend_continuity(session_id: str, token: str | None = None) -> dict:
    """对已有会话重新做连续性(穿帮)检查，返回 continuity_report。"""
    try:
        r = requests.post(f"{API}/api/crew/continuity/{session_id}", headers=_auth_headers(token), timeout=60)
        return r.json()
    except requests.RequestException:
        return {"status": "error", "message": "后端不可达"}


def backend_publish(session_id: str, platforms: list | None = None, meta: dict | None = None,
                    token: str | None = None) -> dict:
    """发布到选定平台；platforms 缺省则全部。"""
    try:
        r = requests.post(f"{API}/api/crew/publish/{session_id}", json={"platforms": platforms or [],
                                                                       "meta": meta or {}},
                          headers=_auth_headers(token), timeout=60)
        return r.json()
    except requests.RequestException:
        return {"status": "error", "message": "后端不可达"}


def backend_link_platform(platform: str, token: str | None = None) -> dict:
    """平台的 OAuth 链接入口（骨架，保存占位 token）。"""
    try:
        r = requests.post(f"{API}/api/crew/publish/link/{platform}", json={},
                          headers=_auth_headers(token), timeout=30)
        return r.json()
    except requests.RequestException:
        return {"status": "error", "message": "后端不可达"}


def backend_settings(token: str | None = None) -> dict:
    try:
        r = requests.get(f"{API}/api/settings", headers=_auth_headers(token), timeout=10)
        return r.json()
    except requests.RequestException:
        return {"status": "error", "message": "后端不可达"}


def backend_settings_update(payload: dict, token: str | None = None) -> dict:
    try:
        r = requests.put(f"{API}/api/settings", json=payload, headers=_auth_headers(token), timeout=10)
        return r.json()
    except requests.RequestException:
        return {"status": "error", "message": "后端不可达"}


def backend_settings_test(payload: dict, token: str | None = None) -> dict:
    """测试通道：对给定 provider/model/api_key 做连通测试，返回错误分类。"""
    try:
        r = requests.post(f"{API}/api/settings/test", json=payload, headers=_auth_headers(token), timeout=30)
        return r.json()
    except requests.RequestException:
        return {"status": "error", "message": "后端不可达"}


def backend_cost_reminder(provider: str, model: str = "", capability: str = "image_gen",
                          units: float = 1, token: str | None = None) -> dict:
    """云端费用提醒（只提醒不拦截）：对一次将要执行的操作给出预估与门槛提示。"""
    try:
        r = requests.post(f"{API}/api/cost/reminder",
                          json={"provider": provider, "model": model,
                                "capability": capability, "units": units},
                          headers=_auth_headers(token), timeout=15)
        return r.json()
    except requests.RequestException:
        return {"status": "error", "message": "后端不可达"}


def backend_cost_audit(token: str | None = None) -> dict:
    """汇总当前配置里哪些项会走云端（含 .env 默认回退）。"""
    try:
        r = requests.get(f"{API}/api/cost/audit", headers=_auth_headers(token), timeout=15)
        return r.json()
    except requests.RequestException:
        return {"status": "error", "message": "后端不可达"}


def backend_providers(capability: str | None = None,
                      include_unavailable: bool = False) -> list:
    """取 provider 清单（唯一数据源；设置页不再自带 PROVIDER_OPTIONS）。

    公开端点，无需 token；后端不可达时返回空列表，调用方回退到内置兜底清单。
    """
    try:
        r = requests.get(f"{API}/api/providers",
                         params={"capability": capability or "",
                                 "include_unavailable": include_unavailable},
                         timeout=10)
        return r.json().get("providers", [])
    except requests.RequestException:
        return []


def backend_generation_modes() -> list:
    """取"云端 / 本地"模式设置项（必须先选的 5 项）。

    后端不可达时返回内置兜底（与 app/services/providers.py 保持一致）
    """
    try:
        r = requests.get(f"{API}/api/providers/modes", timeout=10)
        return r.json().get("modes", [])
    except requests.RequestException:
        return [
            {"capability": "llm", "mode_key": "llm_provider_mode",
             "provider_key": "llm_provider", "mode_options": ["unset", "cloud", "local"],
             "mode_labels": {"cloud": "云端 API", "local": "本地推理", "unset": "未选择"},
             "mode_defaults": {"cloud": "deepseek", "local": "ollama"}},
            {"capability": "image_gen", "mode_key": "image_provider_mode",
             "provider_key": "image_provider", "mode_options": ["unset", "cloud", "local"],
             "mode_labels": {"cloud": "云端 API", "local": "本地推理", "unset": "未选择"},
             "mode_defaults": {"cloud": "dashscope", "local": "local"}},
            {"capability": "video_gen", "mode_key": "video_provider_mode",
             "provider_key": "video_provider", "mode_options": ["unset", "cloud", "local"],
             "mode_labels": {"cloud": "云端 API", "local": "本地推理", "unset": "未选择"},
             "mode_defaults": {"cloud": "minimax", "local": "local"}},
        ]


def backend_asset_upload(name: str, data_base64: str, ext: str, token: str | None = None) -> dict:
    """上传图片资产为 base64，后端保存到本地并返回可被 AI 消费的路径。"""
    try:
        r = requests.post(f"{API}/api/asset/upload",
                          json={"name": name, "data_base64": data_base64, "ext": ext},
                          headers=_auth_headers(token), timeout=60)
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        return {"status": "error", "message": "后端不可达"}


def backend_profile(token: str | None = None) -> dict:
    try:
        r = requests.get(f"{API}/api/user/profile", headers=_auth_headers(token), timeout=10)
        return r.json()
    except requests.RequestException:
        return {"status": "error", "message": "后端不可达"}


def backend_profile_update(payload: dict, token: str | None = None) -> dict:
    try:
        r = requests.put(f"{API}/api/user/profile", json=payload, headers=_auth_headers(token), timeout=20)
        return r.json()
    except requests.RequestException:
        return {"status": "error", "message": "后端不可达"}


def local_run(user_input: str, session_id: str, seed: dict | None = None) -> dict:
    """本地回退：进程内运行（非 HITL），返回 {status, session_id, payload}。"""
    from app.workflows.main_workflow import run_once

    state = {"user_input": user_input, "session_id": session_id}
    if seed:
        state.update(seed)
    result = run_once(state)
    return {"status": "done", "session_id": session_id, "payload": result.get("result", result)}
