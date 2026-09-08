"""小剧组 (Little Crew) — FastAPI 服务入口（异步任务 + 状态轮询 + 限流/配额）。

端点：
  GET  /health                   健康检查
  POST /api/crew/run             异步启动一次创作 -> {status, session_id}
  GET  /api/crew/status/{id}     轮询任务状态/进度/结果
  POST /api/crew/resume          对暂停的确认点续跑
  GET  /api/crew/history/{uid}   用户创作历史

生产化：限流/配额、请求日志（脱敏）、统一异常兜底。
"""

import time
import uuid
import threading

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.services.auth import get_user_store
from app.services.logging import redact, setup_logging
from app.services.profile import get_profile_store
from app.services.quota import quota
from app.services.runtime_settings import set_many as rs_set, snapshot as rs_snap
from app.services.session_store import get_session_store
from app.services.task_runner import cancel_run, resume_run, start_run
from app.services.video import get_video_service

logger = setup_logging()

app = FastAPI(
    title="小剧组 Little Crew",
    description="基于 LangGraph 多智能体协作的 AI 漫剧自动生成平台",
    version="0.3.0",
)


_HEALTH_PUBLIC = ("/health", "/api/auth", "/api/asset", "/docs", "/openapi.json", "/redoc")


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """硬性门槛：除公开路径外，必须带有效登录 token（Authorization: Bearer）。"""
    path = request.url.path
    if path.startswith(_HEALTH_PUBLIC):
        return await call_next(request)
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        user = get_user_store().resolve(auth[7:].strip())
        if user:
            return await call_next(request)
    return JSONResponse(status_code=401, content={"status": "error", "message": "未登录，请先登录"})


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """记录请求概要（不含 body/headers，避免泄漏密钥）。"""
    start = time.perf_counter()
    response = await call_next(request)
    dur_ms = (time.perf_counter() - start) * 1000
    client = request.client.host if request.client else "-"
    logger.info("%s %s -> %s (%.0fms) ip=%s", request.method, request.url.path, response.status_code, dur_ms, client)
    return response


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    """全局异常兜底：只回显脱敏后的通用信息，细节进日志。"""
    logger.error("unhandled error on %s: %s", request.url.path, redact(str(exc)), exc_info=exc)
    return JSONResponse(status_code=500, content={"status": "error", "message": "服务内部错误，请稍后重试。"})


@app.get("/")
def root():
    """根路径：返回服务信息。"""
    return {"message": "Little Crew API", "docs": "/docs", "health": "/health"}


@app.get("/health")
def health():
    """健康检查。"""
    return {"status": "ok", "service": "little-crew"}


# ------------------------------------------------------------------
# 认证：注册 / 登录 / 解析当前用户
# ------------------------------------------------------------------
def _auth_user(request: Request, fallback: str = "local") -> str:
    """从 Authorization: Bearer token 解析出用户名；否则用 fallback。"""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        user = get_user_store().resolve(auth[7:].strip())
        if user:
            return user
    return fallback


def _require_owner(session_id: str, request: Request) -> dict:
    """校验当前登录用户对该 session 有属主权限；返回该会话行，否则抛 404/403。

    auth_guard 已保证这些端点带有效 token，故 _auth_user 返回的是认证用户本身。
    任何按 session 操作的端点都应以它为准，否则任意登录用户可读写他人会话(IDOR)。
    """
    row = get_session_store().get(session_id)
    if not row:
        raise HTTPException(status_code=404, detail="会话不存在")
    owner = _auth_user(request, "")
    if owner != row.get("user_id"):
        # 匿名/遗留会话仅允许 "local" 或未登录态访问，登录用户不可越权。
        raise HTTPException(status_code=403, detail="无权访问该会话")
    return row


_MASK_SENTINEL = "••••已配置"


def _is_secret_key(k: str) -> bool:
    low = k.lower()
    return ("api_key" in low) or ("token" in low) or ("password" in low) or ("secret" in low)


def _mask_secrets(d: dict) -> dict:
    """把敏感字段(api_key/token/password)掩码，避免完整密钥回传到前端 DOM。"""
    out = {}
    for k, v in d.items():
        if _is_secret_key(k) and v:
            out[k] = _MASK_SENTINEL
        else:
            out[k] = v
    return out


@app.post("/api/auth/register")
async def register(payload: dict):
    """注册用户，返回 token。"""
    token, msg = get_user_store().register(payload.get("username", ""), payload.get("password", ""))
    if token is None:
        return {"status": "error", "message": msg}
    return {"status": "ok", "token": token, "username": (payload.get("username") or "").strip()}


@app.post("/api/auth/login")
async def login(payload: dict):
    """登录，返回 token。"""
    token, msg = get_user_store().login(payload.get("username", ""), payload.get("password", ""))
    if token is None:
        return {"status": "error", "message": msg}
    return {"status": "ok", "token": token, "username": get_user_store().resolve(token)}


# ------------------------------------------------------------------
# API 设置（运行时可切换提供者/密钥）
# ------------------------------------------------------------------
# 可独立选模型的文字 Agent 职业（llm._resolve(role) 按这些值读 per-role 覆盖）
_ROLES = ["writer", "critics", "screenwriter", "storyboarder", "director", "photographer", "continuity"]
# 可独立选模型的图片/视频职业（image.py/video.py 按 _img_role 读 per-role 覆盖）
_IMAGE_ROLES = ["character_designer", "scene_designer", "photographer"]
_VIDEO_ROLES = ["editor"]
_SETTABLE = ("llm_provider", "llm_api_key", "llm_base_url", "llm_model",
             "image_provider", "image_api_key", "dashscope_api_key", "image_model", "replicate_api_token",
             "minimax_api_key", "minimax_base_url",
             "video_provider", "video_model", "video_api_key", "video_base_url",
             "video_max_duration", "video_resolution",
             "max_images", "video_count", "image_quality", "video_quality",
             *[f"llm_provider_{r}" for r in _ROLES],
             *[f"llm_model_{r}" for r in _ROLES],
             *[f"llm_api_key_{r}" for r in _ROLES],
             *[f"image_provider_{r}" for r in _IMAGE_ROLES],
             *[f"image_model_{r}" for r in _IMAGE_ROLES],
             *[f"image_api_key_{r}" for r in _IMAGE_ROLES],
             *[f"video_provider_{r}" for r in _VIDEO_ROLES],
             *[f"video_model_{r}" for r in _VIDEO_ROLES],
             *[f"video_api_key_{r}" for r in _VIDEO_ROLES])


@app.get("/api/settings")
async def get_settings(request: Request):
    """返回指定用户（默认当前认证用户/local）的运行时可读设置（密钥已掩码）。"""
    uid = _auth_user(request, "local")
    return {"status": "ok", "user_id": uid, "settings": _mask_secrets(rs_snap(uid))}


@app.put("/api/settings")
async def put_settings(payload: dict, request: Request):
    """更新指定用户的运行时 API 配置（传 None=保留，传空串=清除覆盖回退默认）。"""
    uid = _auth_user(request, payload.get("user_id") or "local")
    allowed = {k: payload[k] for k in _SETTABLE if k in payload}
    # 丢弃掩码哨兵，避免把掩码回写覆盖真实密钥；空串=清除覆盖
    cleaned = {k: v for k, v in allowed.items()
               if v is not None and not (_is_secret_key(k) and v == _MASK_SENTINEL)}
    rs_set(uid, cleaned)
    return {"status": "ok", "user_id": uid, "settings": _mask_secrets(rs_snap(uid))}


@app.post("/api/settings/test")
async def test_settings(payload: dict, request: Request):
    """测试通道：对给定 类型(provider/model/api_key) 做一次真实连通测试，识别问题模型。

    body: {type: "llm"|"image"|"video", provider, model, api_key, base_url}
    返回 {status, ok, error_type, detail}，error_type 区分 auth/network/model/rate_limit。
    """
    from app.services.llm import test_llm_provider

    body = payload or {}
    kind = str(body.get("type") or "llm").lower()
    provider = str(body.get("provider") or "")
    model = str(body.get("model") or "")
    api_key = str(body.get("api_key") or "")
    base_url = str(body.get("base_url") or "")

    # 用 body 里的 provider 优先；若不填则按当前运行时设置解析默认 provider
    if not provider:
        from app.core.config import settings as _cfg
        from app.services.runtime_settings import snapshot as rs_snap2
        snap = rs_snap2()
        if kind == "llm":
            from app.services.llm import _resolve
            p, b, m, k = _resolve()
        elif kind == "image":
            p, b, m, k = (snap.get("image_provider", "dashscope"),
                          snap.get("image_base_url", "") or _cfg.minimax_base_url,
                          snap.get("image_model", ""),
                          snap.get("image_api_key") or snap.get("dashscope_api_key")
                          or snap.get("minimax_api_key") or _cfg.minimax_api_key)
        else:
            p, b, m, k = (snap.get("video_provider", "minimax"), "",
                          snap.get("video_model", "") or "MiniMax-H3",
                          snap.get("video_api_key") or snap.get("minimax_api_key")
                          or _cfg.minimax_api_key)
        provider = p or ""
        base_url = base_url or b or ""
        model = model or m or ""
        api_key = api_key or k or ""

    if kind == "llm":
        res = test_llm_provider(provider, base_url or None, model or None, api_key or None)
        return {**res, "status": "ok" if res["ok"] else "error", "type": kind,
                "provider": provider, "model": model}
    # 图片/视频：真实测试通道
    if kind == "image":
        if provider.lower() == "minimax":
            res = _test_minimax_image(api_key, base_url, model)
            return {**res, "status": "ok" if res["ok"] else "error", "type": kind,
                    "provider": provider, "model": model}
        missing = "DASHSCOPE_API_KEY / IMAGE_API_KEY / REPLICATE_API_TOKEN" if not api_key else ""
        detail = (f"图片提供者={provider or '默认'} 模型={model or '默认'}。"
                  + ("缺少 API Key：" + missing if missing else "配置已就绪，但真实图片引擎需 MOCK_IMAGE=0 且正确 key。"))
        return {"status": "error", "ok": False, "error_type": "unwired" if api_key else "auth",
                "detail": detail, "type": kind, "provider": provider, "model": model}
    if kind == "video":
        if provider.lower() == "minimax":
            res = _test_minimax_video(api_key, base_url, model)
            return {**res, "status": "ok" if res["ok"] else "error", "type": kind,
                    "provider": provider, "model": model}
        detail = f"视频提供者={provider or 'FFmpeg'} 模型={model or '默认'}。真实视频需安装 FFmpeg 并设 MOCK_VIDEO=0。"
        return {"status": "error", "ok": False, "error_type": "unwired", "detail": detail,
                "type": kind, "provider": provider, "model": model}


def _test_minimax_image(api_key: str, base_url: str, model: str) -> dict:
    """MiniMax 文生图连通测试（调 /v1/image_generation 出一张小图）。"""
    if not api_key:
        return {"ok": False, "error_type": "auth", "detail": "缺少 MINIMAX_API_KEY"}
    import httpx
    base = (base_url or "https://api.minimaxi.com").rstrip("/")
    body = {"model": model or "image-01", "prompt": "a red apple, studio light",
            "aspect_ratio": "1:1", "n": 1, "prompt_optimizer": False}
    try:
        with httpx.Client(timeout=httpx.Timeout(120, connect=15)) as c:
            r = c.post(f"{base}/v1/image_generation", json=body,
                       headers={"Authorization": f"Bearer {api_key}"})
        d = r.json()
        urls = d.get("data", {}).get("image_urls") or []
        if r.status_code >= 400 or not urls:
            msg = redact(str(d))[:200]
            et = "auth" if ("api key" in msg.lower() or "401" in str(r.status_code)) else "other"
            return {"ok": False, "error_type": et, "detail": msg}
        return {"ok": True, "error_type": "ok", "detail": "MiniMax 文生图调用成功"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error_type": "network", "detail": redact(str(exc))[:200]}


def _test_minimax_video(api_key: str, base_url: str, model: str) -> dict:
    """MiniMax 图生视频连通测试（提交一个极短 T2V 任务，返回 task_id 即视为 provider 可用）。"""
    if not api_key:
        return {"ok": False, "error_type": "auth", "detail": "缺少 MINIMAX_API_KEY"}
    import httpx
    base = (base_url or "https://api.minimaxi.com").rstrip("/")
    body = {"model": model or "MiniMax-H3",
            "content": [{"type": "text", "text": "a gentle pan of a forest"}],
            "resolution": "768P", "duration": 5, "ratio": "16:9"}
    try:
        with httpx.Client(timeout=httpx.Timeout(60, connect=15)) as c:
            r = c.post(f"{base}/v2/video_generation", json=body,
                       headers={"Authorization": f"Bearer {api_key}"})
        d = r.json()
        if r.status_code >= 400 or not d.get("task_id"):
            msg = redact(str(d))[:200]
            et = "auth" if ("api key" in msg.lower() or "401" in str(r.status_code)) else "other"
            return {"ok": False, "error_type": et, "detail": msg}
        return {"ok": True, "error_type": "ok", "detail": f"MiniMax 视频任务已提交 (task_id={d['task_id']})，可轮询。"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error_type": "network", "detail": redact(str(exc))[:200]}
    return {"status": "error", "ok": False, "error_type": "other", "detail": f"未知类型 {kind}"}


# ------------------------------------------------------------------
# 用户个人资料（显示名/头像）
# ------------------------------------------------------------------
@app.get("/api/user/profile")
async def get_profile(request: Request):
    """返回当前用户的个人资料。"""
    uid = _auth_user(request, "local")
    return {"status": "ok", "profile": get_profile_store().get(uid)}


@app.put("/api/user/profile")
async def put_profile(payload: dict, request: Request):
    """更新当前用户的个人资料（display_name / avatar / avatar_color）。"""
    uid = _auth_user(request, payload.get("user_id") or "local")
    prof = get_profile_store().set(
        uid,
        display_name=payload.get("display_name"),
        avatar=payload.get("avatar"),
        avatar_color=payload.get("avatar_color"),
    )
    return {"status": "ok", "profile": prof}


@app.post("/api/crew/run")
async def run_crew(payload: dict, request: Request):
    """异步启动一次创作（后台运行，轮询 /status 取进度）。含限流与每日配额。"""
    user_input = (payload.get("user_input") or "").strip()
    if not user_input:
        return {"status": "error", "message": "缺少 user_input"}
    user_id = _auth_user(request, payload.get("user_id") or "local")
    client_ip = request.client.host if request.client else "unknown"

    ok, msg = quota.check(client_ip, user_id)
    if not ok:
        return JSONResponse(status_code=429, content={"status": "error", "message": msg})

    session_id = payload.get("session_id") or uuid.uuid4().hex[:8]
    start_run(session_id, user_input, user_id, seed=payload.get("seed") or None)
    return {"status": "accepted", "session_id": session_id, "user_id": user_id}


@app.get("/api/crew/status/{session_id}")
async def crew_status(session_id: str, request: Request):
    """查询任务状态 / 阶段 / 结果。"""
    from app.services.task_runner import get_thinking

    row = _require_owner(session_id, request)
    return {
        "status": row["status"],
        "phase": row["phase"],
        "payload": row["payload"],
        "thinking": get_thinking(session_id),   # 实时「AI 思考过程」
        "session_id": session_id,
        "user_id": row["user_id"],
    }


@app.post("/api/asset/upload")
async def upload_asset(payload: dict, request: Request):
    """保存用户上传的图片资产到本地，返回可被 AI 消费的本地路径。

    body: {name: 文件名(不含扩展), data_base64: "base64...", ext: "png|jpg|webp"}
    返回 {status, path}。
    """
    import base64
    import uuid as _uuid

    from app.core.config import settings

    body = payload or {}
    name = str(body.get("name") or "asset")
    ext = str(body.get("ext") or "png").lower()
    if ext not in ("png", "jpg", "jpeg", "webp"):
        ext = "png"
    data_b64 = str(body.get("data_base64") or "")
    if not data_b64:
        return {"status": "error", "message": "缺少图片数据"}
    try:
        raw = base64.b64decode(data_b64.split(",", 1)[-1])
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": f"base64 解析失败：{exc}"}

    out_dir = settings.output_dir_path / "user_assets"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in name if c.isalnum() or c in ("_", "-")) or "asset"
    path = out_dir / f"{safe}_{_uuid.uuid4().hex[:8]}.{ext}"
    path.write_bytes(raw)
    return {"status": "ok", "path": str(path), "name": safe}


@app.post("/api/crew/cancel/{session_id}")
async def cancel_crew(session_id: str, request: Request):
    """中途打断：停止正在生成的会话。"""
    _require_owner(session_id, request)
    ok = cancel_run(session_id)
    return {"status": "ok" if ok else "error", "message": "已打断" if ok else "会话不存在", "session_id": session_id}


@app.post("/api/crew/resume")
async def resume_crew(payload: dict, request: Request):
    """对暂停的确认点/人工接管段续跑（approve / redo+feedback / 提供 content）。"""
    session_id = payload.get("session_id", "")
    decision = payload.get("decision", "approve")
    feedback = payload.get("feedback", {})
    content = payload.get("content")
    row = _require_owner(session_id, request)
    if row["status"] != "review":
        return {"status": "error", "message": "会话不在确认点，无法续跑", "session_id": session_id}
    resume_run(session_id, row["user_id"] or "local", decision, feedback, content)
    return {"status": "accepted", "session_id": session_id}


@app.get("/api/crew/history/{user_id}")
async def history(user_id: str, request: Request):
    """用户创作历史（按时间倒序）。已登录时按 token 用户隔离。"""
    resolved = _auth_user(request, user_id)
    return {"user_id": resolved, "items": get_session_store().list_by_user(resolved)}


@app.delete("/api/crew/history/{session_id}")
async def delete_history(session_id: str, request: Request):
    """删除一条创作历史。"""
    _require_owner(session_id, request)
    get_session_store().delete(session_id)
    return {"status": "ok", "session_id": session_id}


@app.post("/api/crew/video/{session_id}")
async def make_video(session_id: str, request: Request, body: dict | None = None):
    """对已完成会话生成视频（非阻塞：后台线程合成，立即返回，可轮询状态）。

    body 可带 {"video_count": N} 指定只生成前 N 段视频（0/缺省=跟图片数走）。
    """
    from app.core.config import settings

    store = get_session_store()
    row = _require_owner(session_id, request)
    payload = row.get("payload") or {}
    images = payload.get("images") or []
    script = payload.get("script") or {}
    user_id = row.get("user_id") or "local"
    video_count = None
    if isinstance(body, dict) and body.get("video_count") is not None:
        video_count = int(body["video_count"])
    # 已在生成则直接返回，不重复起线程、也不阻塞事件循环
    if session_id in _VIDEO_JOBS and _VIDEO_JOBS[session_id].is_alive():
        return {"status": "ok", "message": "视频正在生成中", "session_id": session_id}
    t = threading.Thread(target=_run_video,
                         args=(session_id, user_id, row.get("user_input", ""), images, script, video_count),
                         daemon=True)
    _VIDEO_JOBS[session_id] = t
    t.start()
    return {"status": "started", "message": "已开始生成视频（后台），可轮询状态", "session_id": session_id}


_VIDEO_JOBS: dict[str, threading.Thread] = {}


def _run_video(session_id: str, user_id: str, user_input: str, images: list, script: dict,
               video_count: int | None = None) -> None:
    """后台线程：合成视频并写回会话 payload（不阻塞事件循环）。"""
    from app.core.config import settings

    store = get_session_store()
    try:
        out, produced = get_video_service().synthesize(
            images, script, str(settings.output_dir_path), session_id, video_count=video_count)
        row = store.get(session_id) or {}
        payload = dict(row.get("payload") or {})
        payload["video"] = {"dir_or_path": out, "produced": produced}
        store.save(session_id, user_id, user_input, row.get("status", "done"),
                   row.get("phase", "done"), payload)
    except Exception as exc:  # noqa: BLE001
        row = store.get(session_id) or {}
        payload = dict(row.get("payload") or {})
        payload["video"] = {"dir_or_path": "", "produced": 0, "error": redact(str(exc))}
        store.save(session_id, user_id, user_input, row.get("status", "done"),
                   row.get("phase", "done"), payload)


@app.post("/api/crew/reassess/{session_id}")
async def reassess_quality(session_id: str, request: Request):
    """对已有会话的图片重新跑一遍 QualityAssessor，更新 quality_report（不重跑工作流）。

    供「质检总监」独立页的『重新评估』按钮调用；仅做展示/手动触发，不改动自动流程。
    """
    from app.agents.quality_assessor import QualityAssessor

    store = get_session_store()
    row = _require_owner(session_id, request)
    payload = row.get("payload") or {}
    if not payload.get("images"):
        return {"status": "error", "message": "该会话没有图片，无法评估", "session_id": session_id}

    user_id = row.get("user_id") or "local"
    # 用当前会话的图片/提示词/角色资产重新评估
    assessor = QualityAssessor()
    out = assessor.run({
        "images": payload.get("images") or [],
        "prompts": payload.get("prompts") or [],
        "character_reference_images": payload.get("character_reference_images") or {},
        "character_profiles": payload.get("character_profiles") or {},
    })
    quality_report = out.get("quality_report") or {}
    payload["quality_report"] = quality_report
    # 同步更新 images 的 quality/status 字段
    payload["images"] = out.get("images") or payload.get("images") or []
    store.save(session_id, user_id, row.get("user_input", ""), row["status"], row["phase"], payload)
    return {"status": "ok", "session_id": session_id, "quality_report": quality_report}


@app.post("/api/crew/retry/{session_id}")
async def retry_shots(session_id: str, request: Request, body: dict | None = None):
    """对已有会话精确重排被点选的镜头（body.shot_ids），其它镜头保持原图。

    异步执行：立即返回 accepted，后台线程逐镜重排并在 payload._retry_progress 更新进度，
    前端可通过 /api/crew/status/{session_id} 轮询到进度与最终 quality_report。
    不改动自动流程、不重跑上游。
    """
    import json
    import threading

    # body 兼容 pydantic dict / 原始 JSON 字符串
    if isinstance(body, str):
        body = json.loads(body or "{}")
    body = body or {}
    shot_ids = [str(s) for s in (body.get("shot_ids") or [])]

    store = get_session_store()
    row = _require_owner(session_id, request)
    payload = row.get("payload") or {}
    if not shot_ids:
        return {"status": "error", "message": "未选择要重排的镜头", "session_id": session_id}
    if not payload.get("prompts"):
        return {"status": "error", "message": "该会话没有可用的提示词", "session_id": session_id}

    # 校验目标镜头在 prompts 中存在
    valid_ids = {p.get("shot_id") for p in (payload.get("prompts") or [])}
    missing = [s for s in shot_ids if s not in valid_ids]
    if missing:
        return {"status": "error", "message": "以下镜头不在可重排范围：" + "、".join(missing),
                "session_id": session_id}

    user_id = row.get("user_id") or "local"
    # 标记开始重排，供前端轮询
    payload["_retry_progress"] = {"state": "running", "total": len(shot_ids), "done": 0,
                                  "shot_ids": shot_ids, "message": "开始重排"}
    store.save(session_id, user_id, row.get("user_input", ""), row["status"], "retry_quality", payload)

    def _do_retry():
        try:
            from app.agents.image_generator import ImageGenerator
            from app.agents.quality_assessor import QualityAssessor

            gen = ImageGenerator()
            from app.services.image import get_image_service
            img_svc = get_image_service()
            from app.services.ip_adapter_service import get_ip_adapter_service
            ip_svc = get_ip_adapter_service()

            prompts = {p.get("shot_id"): p for p in (payload.get("prompts") or [])}
            char_refs = dict(payload.get("character_reference_images") or {})
            known_names = list(char_refs.keys())
            existing = {im["shot_id"]: im for im in (payload.get("images") or [])}
            new_images = dict(existing)

            def _save_progress(done):
                cur = dict(store.get(session_id).get("payload") or {})
                cur["_retry_progress"] = {"state": "running", "total": len(shot_ids), "done": done,
                                          "shot_ids": shot_ids, "message": f"重排 {done}/{len(shot_ids)}"}
                store.save(session_id, user_id, row.get("user_input", ""), "done",
                           "retry_quality", cur)

            done = 0
            for sid in shot_ids:
                p = prompts.get(sid)
                if p:
                    # 逐个镜头重排（复用 ImageGenerator 的生成逻辑）
                    result = gen._generate_one_with_ip(p, img_svc, ip_svc, char_refs, known_names)
                    new_images[sid] = result
                done += 1
                _save_progress(done)

            # 重新评估（更新 quality/consistency/quality_report）
            ordered = [new_images[p.get("shot_id")] for p in (payload.get("prompts") or [])
                       if p.get("shot_id") in new_images]
            assessor = QualityAssessor()
            assess_out = assessor.run({
                "images": ordered,
                "prompts": payload.get("prompts") or [],
                "character_reference_images": char_refs,
                "character_profiles": payload.get("character_profiles") or {},
            })
            new_payload = dict(store.get(session_id).get("payload") or {})
            new_payload["images"] = assess_out.get("images") or ordered
            new_payload["quality_report"] = assess_out.get("quality_report") or {}
            new_payload["_retry_progress"] = {"state": "done", "total": len(shot_ids), "done": len(shot_ids),
                                              "shot_ids": shot_ids, "message": "重排完成"}
            store.save(session_id, user_id, row.get("user_input", ""), row["status"], "done", new_payload)
        except Exception as exc:  # noqa: BLE001
            from app.services.logging import redact
            cur = dict(store.get(session_id).get("payload") or {})
            cur["_retry_progress"] = {"state": "error", "message": redact(str(exc)),
                                      "total": len(shot_ids), "done": 0, "shot_ids": shot_ids}
            store.save(session_id, user_id, row.get("user_input", ""), row["status"], "retry_quality", cur)

    threading.Thread(target=_do_retry, daemon=True).start()
    return {"status": "accepted", "session_id": session_id, "retried": shot_ids}


@app.post("/api/crew/review/{session_id}")
async def re_review(session_id: str, request: Request):
    """对已有会话的剧本重新跑一遍 ScriptCritic，更新 critic_report（不重跑工作流）。

    供「文学策划」独立页『重新审查』按钮调用；仅做展示/手动触发，不改动自动流程。
    """
    from app.agents.critics import ScriptCritic

    store = get_session_store()
    row = _require_owner(session_id, request)
    payload = row.get("payload") or {}
    if not payload.get("script"):
        return {"status": "error", "message": "该会话没有剧本，无法审查", "session_id": session_id}

    user_id = row.get("user_id") or "local"
    critic = ScriptCritic()
    # 从持久化的 critic_history 恢复历史，供追加；无则用 state.critic_report.history
    from app.services.critic_history import get_critic_history_store

    hist_store = get_critic_history_store()
    persisted_history = hist_store.get(session_id)
    if not persisted_history:
        persisted_history = (payload.get("critic_report") or {}).get("history") or []
    out = critic.run({
        "script": payload.get("script"),
        "story_outline": payload.get("outline") or {},
        "critic_report": {"history": persisted_history},  # 供追加修改历史
    })
    review = out.get("script_review") or {}
    critic_report = out.get("critic_report") or {}
    payload["script_review"] = review
    payload["critic_report"] = critic_report
    store.save(session_id, user_id, row.get("user_input", ""), row["status"], row["phase"], payload)
    # 跨会话持久化：同步到 critic_history 表
    hist_store.set(session_id, user_id, critic_report.get("history") or [])
    return {"status": "ok", "session_id": session_id,
            "script_review": review, "critic_report": critic_report,
            "history": critic_report.get("history") or []}


@app.get("/api/crew/review/history/{session_id}")
async def review_history(session_id: str, request: Request):
    """读取某会话持久化的剧评/修改历史（跨会话）。"""
    from app.services.critic_history import get_critic_history_store

    _require_owner(session_id, request)
    history = get_critic_history_store().get(session_id)
    return {"status": "ok", "session_id": session_id, "history": history}


@app.post("/api/crew/scene/{session_id}")
async def regen_scenes(session_id: str, request: Request, body: dict | None = None):
    """对已有会话重新生成场景氛围参考图。

    body 可选：{scene_ids: [...]} 指定只重做部分场景；缺省则重做全部场景。
    供「场景设计师」独立页『重新生成所有场景』/『修改某场景』调用。
    """
    import json

    from app.agents.stage2.scene_designer import SceneDesigner

    if isinstance(body, str):
        body = json.loads(body or "{}")
    body = body or {}
    scene_ids = [str(s) for s in (body.get("scene_ids") or [])]

    store = get_session_store()
    row = _require_owner(session_id, request)
    payload = row.get("payload") or {}
    if not payload.get("script"):
        return {"status": "error", "message": "该会话没有剧本，无法生成场景", "session_id": session_id}

    user_id = row.get("user_id") or "local"

    # 若要重做部分场景，先给 SceneDesigner 注入现有产物（保留其它）
    designer = SceneDesigner()
    if scene_ids:
        # 强制这些场景重新生成：临时把其它场景的 scene_reference_images 挖掉？改用 feedback 精确处理
        existing_refs = dict(payload.get("scene_reference_images") or {})
        existing_profiles = dict(payload.get("scene_design_profiles") or {})
        # 只对指定场景重做：先保留其它，再重跑并合并
        out = designer.run({
            "script": payload.get("script"),
            "story_outline": payload.get("outline") or {},
            "feedback": {"scene_ids": scene_ids},
            "scene_reference_images": existing_refs,
            "scene_design_profiles": existing_profiles,
        })
        # 合并：仅覆盖指定场景
        refs = dict(existing_refs); profs = dict(existing_profiles)
        refs.update(out.get("scene_reference_images") or {})
        profs.update(out.get("scene_design_profiles") or {})
    else:
        out = designer.run({
            "script": payload.get("script"),
            "story_outline": payload.get("outline") or {},
        })
        refs = out.get("scene_reference_images") or {}
        profs = out.get("scene_design_profiles") or {}

    payload["scene_reference_images"] = refs
    payload["scene_design_profiles"] = profs
    store.save(session_id, user_id, row.get("user_input", ""), row["status"], row["phase"], payload)
    return {"status": "ok", "session_id": session_id,
            "scene_reference_images": refs, "scene_design_profiles": profs}


@app.post("/api/crew/rhythm/{session_id}")
async def replan_rhythm(session_id: str, request: Request, body: dict | None = None):
    """对已有会话重新规划镜头节奏（RhythmDirector），写回 storyboard 时长。

    body 可选：{overrides: {shot_id: duration}} 手动调整指定镜头时长；
    {total_cap: 秒} 可覆盖总时长上限。
    供「剪辑指导」独立页『重新规划』/『手动调整』调用。
    """
    import json

    from app.agents.stage3.rhythm_director import RhythmDirector

    if isinstance(body, str):
        body = json.loads(body or "{}")
    body = body or {}
    overrides = dict(body.get("overrides") or {})
    # 注意 overrides 为 {shot_id: duration}，若用户传单值时需可覆盖

    store = get_session_store()
    row = _require_owner(session_id, request)
    payload = row.get("payload") or {}
    if not payload.get("storyboard"):
        return {"status": "error", "message": "该会话没有分镜，无法规划节奏", "session_id": session_id}

    user_id = row.get("user_id") or "local"

    # 用现有 storyboard + script 重新规划
    director = RhythmDirector()
    out = director.run({
        "storyboard": payload.get("storyboard") or [],
        "script": payload.get("script") or {},
    })
    plan = out.get("rhythm_plan") or []
    # 应用手动覆盖（时长）
    if overrides:
        for p in plan:
            sid = p["shot_id"]
            if sid in overrides:
                try:
                    p["duration"] = round(float(overrides[sid]), 2)
                except (TypeError, ValueError):
                    pass

    # 写回 payload.storyboard 的 duration_s
    storyboard = payload.get("storyboard") or []
    by_id = {p["shot_id"]: p for p in plan}
    new_board = []
    for s in storyboard:
        ns = dict(s)
        p = by_id.get(str(s.get("shot_id") or "").strip())
        if p:
            ns["duration_s"] = p["duration"]
            ns["tempo"] = p["tempo"]
        new_board.append(ns)
    payload["storyboard"] = new_board
    payload["rhythm_plan"] = plan
    store.save(session_id, user_id, row.get("user_input", ""), row["status"], row["phase"], payload)
    return {"status": "ok", "session_id": session_id, "rhythm_plan": plan, "storyboard": new_board}


@app.post("/api/crew/continuity/{session_id}")
async def check_continuity(session_id: str, request: Request, body: dict | None = None):
    """对已有会话的完整分镜做连续性(穿帮)检查，更新 continuity_report。

    供「场记」独立页『重新检查』调用；仅检查、不改动分镜。
    """
    from app.agents.stage3.continuity_checker import ContinuityChecker

    store = get_session_store()
    row = _require_owner(session_id, request)
    payload = row.get("payload") or {}
    if not payload.get("storyboard"):
        return {"status": "error", "message": "该会话没有分镜，无法检查", "session_id": session_id}

    user_id = row.get("user_id") or "local"
    checker = ContinuityChecker()
    out = checker.run({
        "storyboard": payload.get("storyboard") or [],
        "script": payload.get("script") or {},
        "scene_design_profiles": payload.get("scene_design_profiles") or {},
    })
    report = out.get("continuity_report") or []
    payload["continuity_report"] = report
    # 顺带把 rhythm 相关也保留（不改）
    store.save(session_id, user_id, row.get("user_input", ""), row["status"], row["phase"], payload)
    return {"status": "ok", "session_id": session_id, "continuity_report": report}


@app.post("/api/crew/publish/{session_id}")
async def publish_crew(session_id: str, request: Request, body: dict | None = None):
    """把成片发布到选定平台（Distributor）。

    body 可选：{platforms: [...], meta: {title, description, tags}}
    供「发行总监」独立页『一键发布』调用；mock 返回模拟链接。
    """
    import json

    from app.agents.distributor import Distributor

    if isinstance(body, str):
        body = json.loads(body or "{}")
    body = body or {}
    platforms = [str(p) for p in (body.get("platforms") or [])]
    meta = body.get("meta") or {}

    store = get_session_store()
    row = _require_owner(session_id, request)
    payload = row.get("payload") or {}
    user_id = row.get("user_id") or "local"

    # 视频产物
    video = payload.get("video") or {}
    outline = payload.get("outline") or {}
    if meta:
        outline = dict(outline)
        outline.update(meta)  # 允许覆盖标题/描述等

    distributor = Distributor()
    out = distributor.run({
        "video": video,
        "story_outline": outline,
    }, platforms=platforms or None)
    results = out.get("publish_results") or {}
    # 合并进 payload
    existing = payload.get("publish_results") or {}
    existing.update(results)
    payload["publish_results"] = existing
    store.save(session_id, user_id, row.get("user_input", ""), row["status"], row["phase"], payload)
    return {"status": "ok", "session_id": session_id, "publish_results": existing}


@app.post("/api/crew/publish/link/{platform}")
async def link_platform(platform: str, request: Request, body: dict | None = None):
    """平台的 OAuth 链接入口（骨架）。把凭证存到 runtime_settings（会话级）。

    body 可选：{token}；mock 时生成一次占位 token 并保存。
    """
    from app.services.runtime_settings import set_many as rs_set, snapshot as rs_snap

    import json
    import secrets

    if isinstance(body, str):
        body = json.loads(body or "{}")
    body = body or {}
    token = body.get("token") or secrets.token_urlsafe(16)
    user_id = _auth_user(request, "local")
    rs_set(user_id, {f"publish_{platform}_token": token})
    return {"status": "ok", "platform": platform, "linked": True, "token": token[:8] + "…",
            "settings_keys": [k for k in rs_snap(user_id) if "publish_" in k]}
