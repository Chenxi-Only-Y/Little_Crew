"""小剧组 (Little Crew) — FastAPI 服务入口（异步任务 + 状态轮询 + 限流/配额）。

端点：
  GET  /health                   健康检查
  POST /api/crew/run             异步启动一次创作 -> {status, session_id}
  GET  /api/crew/status/{id}     轮询任务状态/进度/结果
  POST /api/crew/resume          对暂停的确认点续跑
  GET  /api/crew/history/{uid}   用户创作历史

生产化：限流/配额、请求日志（脱敏）、统一异常兜底。
"""

import pathlib
import shutil
import time
import uuid
import threading

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from app.services.auth import get_user_store
from app.services.events import get_hub
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


# 公开路径：健康检查 / 认证 / 静态结构 schema。
# `/api/registry` 与 `/api/providers` 是**纯静态定义**（职业/节点/端口/提供者清单），
# 不含任何用户数据或密钥；前端需要在登录前就能渲染导演台骨架，故一并公开。
_HEALTH_PUBLIC = ("/health", "/api/auth", "/api/asset", "/api/providers", "/api/registry",
                  "/docs", "/openapi.json", "/redoc")


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """硬性门槛：除公开路径外，必须带有效登录 token。

    鉴权来源（按优先级）：
      1. `Authorization: Bearer <token>`（常规请求）
      2. `?token=<token>` 查询参数

    为什么需要第 2 条：浏览器的 `EventSource` **无法自定义请求头**，
    SSE 订阅（`/api/sessions/{id}/events`）只能把 token 放查询参数里。
    非 loopback 部署务必用 HTTPS，否则 token 会在 URL 中明文传输。
    """
    path = request.url.path
    if path.startswith(_HEALTH_PUBLIC):
        return await call_next(request)

    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        token = request.query_params.get("token", "").strip()
    if token and get_user_store().resolve(token):
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
# 白名单**不再手写**：由 `registry.api.allowed_settings()` 从注册表派生。
# 原先这里有一份 `_SETTABLE` 常量，注册表新增配置键（tts_provider /
# lora_train_provider / image_provider_image_edit 等）时没同步，
# 导致界面上能改、保存却被静默丢弃。派生之后"能改的一定存得下"。


@app.get("/api/settings")
async def get_settings(request: Request):
    """返回指定用户（默认当前认证用户/local）的运行时可读设置（密钥已掩码）。"""
    uid = _auth_user(request, "local")
    return {"status": "ok", "user_id": uid, "settings": _mask_secrets(rs_snap(uid))}


@app.put("/api/settings")
async def put_settings(payload: dict, request: Request):
    """更新指定用户的运行时 API 配置（传 None=保留，传空串=清除覆盖回退默认）。

    白名单**由注册表派生**（`registry.api.allowed_settings()`），不再手写：
    否则注册表新增配置键时，界面上能改、却会被这里静默丢弃（已实际发生过）。
    """
    from app.registry import api as registry

    uid = _auth_user(request, payload.get("user_id") or "local")
    settable = registry.allowed_settings()
    allowed = {k: payload[k] for k in settable if k in payload}
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


# ------------------------------------------------------------------
# 工程文件（导演台画布）：保存/读取用户搭好的那张图
# ------------------------------------------------------------------
# 只存"画布形状 + 参数"，不存运行结果（结果是 sessions 的事）。
# 密钥**绝不入库**：写入前把 settings_snapshot 里的敏感键替换为掩码哨兵。

_PROJECT_SECRET_HINTS = ("api_key", "token", "secret", "password")


def _mask_for_project(snap: dict) -> dict:
    """工程快照里不保存任何密钥（只留"是否配过"的信息）。"""
    out = {}
    for k, v in (snap or {}).items():
        if any(h in k.lower() for h in _PROJECT_SECRET_HINTS):
            out[k] = _MASK_SENTINEL if v else ""
        else:
            out[k] = v
    return out


@app.get("/api/projects")
async def list_projects(request: Request):
    """列出当前用户的工程（按最近修改排序）。"""
    uid = _auth_user(request)
    if not uid:
        raise HTTPException(status_code=401, detail="未登录")
    from app.services.project_store import get_project_store

    items = get_project_store().list_by_user(uid)
    return {"status": "ok", "count": len(items), "projects": items}


@app.post("/api/projects")
async def create_project(payload: dict, request: Request):
    """保存工程。

    body: {name, canvas:{nodes,edges,viewport}, settings_snapshot?, project_id?}
    `project_id` 存在则覆盖该工程（用于"另存/更新"）。
    """
    uid = _auth_user(request)
    if not uid:
        raise HTTPException(status_code=401, detail="未登录")
    from app.services.project_store import get_project_store

    body = payload or {}
    try:
        summary = get_project_store().save(
            user_id=uid,
            name=str(body.get("name") or "未命名工程"),
            canvas=body.get("canvas") or {},
            settings_snapshot=_mask_for_project(body.get("settings_snapshot") or {}),
            project_id=body.get("project_id") or None,
        )
    except ValueError as exc:            # 体积超限等
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except PermissionError as exc:       # 覆盖他人工程
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"status": "ok", **summary}


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str, request: Request):
    """读取工程。不存在或不属于当前用户都返回 404（不暴露他人工程 id）。"""
    uid = _auth_user(request)
    if not uid:
        raise HTTPException(status_code=401, detail="未登录")
    from app.services.project_store import get_project_store

    doc = get_project_store().get(project_id, uid)
    if not doc:
        raise HTTPException(status_code=404, detail="工程不存在")
    return {"status": "ok", "project": doc}


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str, request: Request):
    uid = _auth_user(request)
    if not uid:
        raise HTTPException(status_code=401, detail="未登录")
    from app.services.project_store import get_project_store

    if not get_project_store().delete(project_id, uid):
        raise HTTPException(status_code=404, detail="工程不存在")
    return {"status": "ok", "project_id": project_id}


@app.post("/api/projects/{project_id}/rename")
async def rename_project(project_id: str, payload: dict, request: Request):
    uid = _auth_user(request)
    if not uid:
        raise HTTPException(status_code=401, detail="未登录")
    from app.services.project_store import get_project_store

    name = str((payload or {}).get("name") or "")
    if not get_project_store().rename(project_id, uid, name):
        raise HTTPException(status_code=404, detail="工程不存在")
    return {"status": "ok", "project_id": project_id, "name": name}


# ------------------------------------------------------------------
# 注册表（唯一数据源）：前端据此渲染节点库/面板/端口/确认点，不再自带清单
# ------------------------------------------------------------------
@app.get("/api/registry")
async def get_registry(response: Response):
    """下发完整注册表（职业/部门/节点/端口/确认点/配置字段/执行顺序/校验规格）。

    设计目的见 `docs/recon/04-frontend-rewrite.md §1`：
    前端**不得**自己定义职业、部门、阶段、产物、provider 清单——一律来自这里，
    否则会重演 v1「同一概念散落 5 个文件、且已真实不一致」的问题。

    这是**纯静态定义**（不含用户数据），故为公开端点；并要求每次重新校验，
    避免注册表更新后前端仍拿旧结构渲染。
    """
    from app.registry import api as registry

    response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return {"status": "ok", "registry": registry.snapshot()}


@app.post("/api/registry/validate")
async def validate_registry_topology(payload: dict | None = None):
    """校验一组节点/连线能否执行（画布"能不能连"的后端判定）。

    body: {
        "nodes": [node_key...],
        "edges": [[src, dst], ...] 或 [{"source": src, "target": dst}, ...],
        "available_inputs": [artifact_key...]
    }
    两种 edges 格式都接受，避免前端为适配接口而多做一层转换。
    与前端预校验共用同一份注册表规则，避免两边漂移。
    """
    from app.registry import api as registry

    body = payload or {}
    nodes = [str(k) for k in (body.get("nodes") or [])]
    edges: list[tuple[str, str]] = []
    for e in body.get("edges") or []:
        if isinstance(e, dict):
            src, dst = e.get("source"), e.get("target")
            if src and dst:
                edges.append((str(src), str(dst)))
        elif isinstance(e, (list, tuple)) and len(e) >= 2:
            edges.append((str(e[0]), str(e[1])))
    inputs = [str(k) for k in (body.get("available_inputs") or [])]
    result = registry.validate_topology(nodes, edges=edges or None, available_inputs=inputs)
    return {"status": "ok" if result["ok"] else "error", **result}


# ------------------------------------------------------------------
# 云端使用提醒（只提醒，不拦截）—— 见 app/services/cloud_cost.py
# ------------------------------------------------------------------
@app.post("/api/cost/reminder")
async def cost_reminder(payload: dict | None = None):
    """对一次"将要执行"的操作给出云端费用提醒（不阻断）。

    body: {provider, model, capability, units}
    返回 {level, is_cloud, title, detail, estimate, badges}，供前端在按钮旁直接展示。
    """
    from app.services.cloud_cost import reminder

    body = payload or {}
    return {
        "status": "ok",
        **reminder(
            str(body.get("provider") or ""),
            str(body.get("model") or ""),
            str(body.get("capability") or "image_gen"),   # type: ignore[arg-type]
            float(body.get("units") or 1),
        ),
    }


@app.get("/api/cost/audit")
async def cost_audit(request: Request):
    """扫描当前用户设置（含 .env 默认回退），汇总"哪些项会走云端、哪些本地免费"。"""
    from app.core.config import settings as _cfg
    from app.services.cloud_cost import audit_settings

    uid = _auth_user(request, "local")
    defaults = {
        "llm_provider": _cfg.llm_provider,
        "deepseek_model": _cfg.deepseek_model,
        "llm_model": _cfg.llm_model,
        "image_provider": _cfg.image_provider,
        "image_model": _cfg.image_model,
        "video_provider": _cfg.video_provider,
        "video_model": _cfg.video_model,
    }
    return {"status": "ok", "user_id": uid, **audit_settings(rs_snap(uid), defaults)}


@app.get("/api/cost/models")
async def cost_models(capability: str | None = None):
    """列出已知模型的成本/可用性元数据（供设置页展示"怎么选"）。"""
    from app.services.cloud_cost import MODEL_COST

    items = [v.__dict__ for v in MODEL_COST.values()]
    if capability:
        items = [i for i in items if i["capability"] == capability]
    return {"status": "ok", "count": len(items), "models": items}


@app.get("/api/providers/modes")
async def provider_modes():
    """下发"云端 / 本地"模式设置项（**必须先选**的 5 项）。

    设计决定（用户 2026-10-02）：不默认本地、也不默认云端，让用户显式选择。
    未选时后端会明确报错而不是猜——避免"没部署本地却走本地"或"不知情产生云端费用"。
    """
    from app.services.providers import generation_settings

    modes = generation_settings()
    return {"status": "ok", "count": len(modes), "modes": modes}


@app.get("/api/providers")
async def list_providers(capability: str | None = None, include_unavailable: bool = False):
    """下发 provider 清单（唯一数据源，前端不再自带 PROVIDER_OPTIONS）。

    设计目的见 `docs/recon/02-refactor-plan.md §2.6`：
    provider 列表原先散在 config.py、设置页、image/video 分派三处。

    Query:
        capability: 只看某类能力（llm / image_gen / image_edit / video_gen /
                    lora_train / tts / music / render_2d）
        include_unavailable: 是否包含"已登记未接入"的提供者
    """
    from app.services.providers import CONDITIONING_LABELS, PROVIDERS

    items = [
        {
            "key": p.key, "label": p.label, "capability": p.capability,
            "category": p.category, "available": p.available,
            "default_model": p.default_model, "known_models": list(p.known_models),
            "needs_api_key": p.needs_api_key, "needs_proxy": p.needs_proxy,
            "max_concurrency": p.max_concurrency, "note": p.note,
            # 该提供者能接受的**结构条件**（仅 video_gen 有意义）。
            # 白模据此决定产出什么——见 providers.previs_plan()。
            "conditioning": list(p.conditioning),
            "conditioning_labels": [CONDITIONING_LABELS.get(c, c) for c in p.conditioning],
        }
        for p in PROVIDERS
        if (not capability or p.capability == capability)
        and (include_unavailable or p.available)
    ]
    return {"status": "ok", "count": len(items), "providers": items}


# ------------------------------------------------------------------
# 白模（预演）：产出什么由**所选的视频模型能力**决定
# ------------------------------------------------------------------
@app.get("/api/previs/plan")
async def previs_plan(request: Request):
    """当前视频模型下，白模应产出什么。

    设计方向（用户 2026-10-02）：白模跟着视频模型走——模型支持什么条件，
    白模就准备什么。模型只收首帧，白模就只算起点布局；不支持任何结构条件，
    白模就"做了也用不上"，此处会如实说明，而不是让用户白跑一遍。
    """
    from app.services.providers import previs_plan as build_plan

    uid = _auth_user(request, "local")
    from app.services import runtime_settings as rs

    rs.set_user(uid)
    try:
        plan = build_plan()
    finally:
        rs.set_user(None)
    return {"status": "ok", "plan": plan}


# ------------------------------------------------------------------
# 会话：启动 + 实时事件流（导演台用）
# ------------------------------------------------------------------
@app.post("/api/sessions")
async def create_session(payload: dict, request: Request):
    """启动一次创作并返回 session_id（导演台入口）。

    body: {user_input, seed?, session_id?}
    与 `/api/crew/run` 等价，只是路径符合"资源"语义；
    立刻返回，进度通过 `GET /api/sessions/{id}/events` 实时推送。
    """
    user_input = (payload.get("user_input") or "").strip()
    if not user_input:
        return JSONResponse(status_code=400, content={"status": "error", "message": "缺少 user_input"})

    user_id = _auth_user(request, payload.get("user_id") or "local")
    client_ip = request.client.host if request.client else "unknown"
    ok, msg = quota.check(client_ip, user_id)
    if not ok:
        return JSONResponse(status_code=429, content={"status": "error", "message": msg})

    session_id = payload.get("session_id") or uuid.uuid4().hex[:8]
    get_hub().reset(session_id)          # 新一轮：清掉上一轮的事件历史
    start_run(session_id, user_input, user_id, seed=payload.get("seed") or None)
    return {"status": "accepted", "session_id": session_id, "user_id": user_id}


@app.get("/api/sessions/{session_id}/events")
async def session_events(session_id: str, request: Request, token: str | None = None):
    """实时事件流（SSE）：阶段状态、AI 思考过程、确认点、整体状态。

    为什么用 SSE 而不是 WebSocket：只需服务端单向推送，浏览器原生支持断线重连，
    实现与部署都更简单（见 docs/recon/04-frontend-rewrite.md §2）。

    鉴权说明：浏览器 `EventSource` **无法自定义请求头**，因此这里额外接受
    `?token=` 查询参数；同时保留 `Authorization` 头（便于 fetch-based 客户端）。
    非 loopback 部署务必用 HTTPS，否则 token 会在 URL 中明文传输。
    """
    from app.services.events import get_hub as _hub

    # 鉴权：优先 Authorization 头，其次 query token
    auth = request.headers.get("Authorization", "")
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    supplied = bearer or (token or "")
    if not supplied or not get_user_store().resolve(supplied):
        return JSONResponse(status_code=401, content={"status": "error", "message": "未登录"})

    hub = _hub()
    return StreamingResponse(
        hub.stream(session_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 关掉 nginx 等反代的缓冲，否则事件会被攒着一起发
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/sessions/{session_id}/events/snapshot")
async def session_events_snapshot(session_id: str, request: Request):
    """已发生事件的快照（用于排查"实时没收到"的问题）。"""
    from app.services.events import get_hub as _hub

    row = _require_owner(session_id, request)
    hub = _hub()
    return {
        "status": "ok",
        "session_id": session_id,
        "session_status": row.get("status"),
        "subscribers": hub.subscriber_count(session_id),
        "events": [
            {"type": e.type, "ts": e.ts, "seq": e.seq, **e.data}
            for e in hub.history(session_id)
        ],
    }


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
    """对暂停的确认点/人工接管段续跑（approve / redo+feedback / 提供 content）。

    若工作流断点已丢失（如服务重启过），返回 409 并说明原因，**不会静默重放整张图**
    （见 `docs/recon/01-usage.md §6.1`）。
    """
    session_id = payload.get("session_id", "")
    decision = payload.get("decision", "approve")
    feedback = payload.get("feedback", {})
    content = payload.get("content")
    row = _require_owner(session_id, request)
    if row["status"] != "review":
        return {"status": "error", "message": "会话不在确认点，无法续跑", "session_id": session_id}

    started, message = resume_run(session_id, row["user_id"] or "local", decision, feedback, content)
    if not started:
        return JSONResponse(
            status_code=409,
            content={"status": "error", "message": message, "session_id": session_id,
                     "reason": "checkpoint_lost"},
        )
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

    body 可带：
      - `video_count`: 只生成前 N 段视频（0/缺省=跟图片数走）
      - `reference_video`: 参考视频（`mm_file://{file_id}` 或公网 URL）。
        给了它就走**参考驱动**（白模录像复刻运镜与走位），此时不再发首尾帧
        ——官方限制两者互斥。
    """
    from app.core.config import settings

    store = get_session_store()
    row = _require_owner(session_id, request)
    payload = row.get("payload") or {}
    images = payload.get("images") or []
    script = payload.get("script") or {}
    user_id = row.get("user_id") or "local"
    video_count = None
    reference_video = ""
    if isinstance(body, dict):
        if body.get("video_count") is not None:
            video_count = int(body["video_count"])
        reference_video = str(body.get("reference_video") or "").strip()
    # 已在生成则直接返回，不重复起线程、也不阻塞事件循环
    if session_id in _VIDEO_JOBS and _VIDEO_JOBS[session_id].is_alive():
        return {"status": "ok", "message": "视频正在生成中", "session_id": session_id}
    t = threading.Thread(
        target=_run_video,
        args=(session_id, user_id, row.get("user_input", ""), images, script,
              video_count, reference_video),
        daemon=True,
    )
    _VIDEO_JOBS[session_id] = t
    t.start()
    return {"status": "started", "message": "已开始生成视频（后台），可轮询状态", "session_id": session_id}


_VIDEO_JOBS: dict[str, threading.Thread] = {}


# ------------------------------------------------------------------
# 白模参考视频：帧序列 → mp4 →（可选）上传换 mm_file://{file_id}
# ------------------------------------------------------------------

@app.post("/api/video/whitebox")
async def upload_whitebox_video(request: Request):
    """接收预演导出的帧序列，编码成 mp4 并（可选）上传，返回可用的参考地址。

    `multipart/form-data`：
      - `frames`: 多份 PNG，按表单顺序即播放顺序（顺序很重要，不能靠文件名猜）
      - `upload`: `"1"` 则上传到 MiniMax 换 `mm_file://{file_id}`；
                  否则只编码，返回本地文件路径（用于先看效果、或不走云端）

    为什么由后端编码而不是浏览器：
      浏览器端要么用 MediaRecorder（基本只能出 webm，而云端只收 mp4），
      要么用 WebCodecs + 自己写 mp4 封装——复杂且兼容性差。
      后端只需调一次 ffmpeg，可靠得多。

    为什么让前端显式传 `upload`：
      上传会消耗额度、且**7 天后过期**。让用户能先只看编码结果，
      而不是一按导出就必然产生一次上传。
    """
    from app.core.config import settings
    from app.services import runtime_settings as rs
    from app.services.whitebox_video import (
        MAX_REF_VIDEO_SECONDS,
        encode_frames_to_mp4,
        probe_duration,
        upload_for_video_generation,
    )

    # 鉴权：`auth_guard` 是全局中间件，`/api/video` 不在 `_HEALTH_PUBLIC` 里，
    # 所以本端点自动要求有效 token——不需要（也不能）在这里手动调它。
    form = await request.form()
    raw_frames = [v for k, v in form.multi_items() if k == "frames"]
    if not raw_frames:
        raise HTTPException(status_code=400, detail="没有收到任何帧（表单字段应为 frames）。")
    do_upload = str(form.get("upload") or "") in ("1", "true", "yes")
    fps = int(str(form.get("fps") or 24))

    work = pathlib.Path(settings.output_dir_path) / "whitebox" / uuid.uuid4().hex[:12]
    frames_dir = work / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    saved: list[pathlib.Path] = []
    try:
        for i, item in enumerate(raw_frames):
            data = await item.read() if hasattr(item, "read") else bytes(item)
            if not data:
                continue
            # 按**接收顺序**编号：顺序即时间顺序，不依赖原文件名
            fp = frames_dir / f"{i:05d}.png"
            fp.write_bytes(data)
            saved.append(fp)

        if not saved:
            raise HTTPException(status_code=400, detail="收到的帧都是空的。")

        out_mp4 = work / "whitebox.mp4"
        try:
            encode_frames_to_mp4(saved, out_mp4, fps=fps,
                                 ffmpeg=str(rs.get("ffmpeg_path") or settings.ffmpeg_path or ""))
        except Exception as exc:  # 找不到 ffmpeg / 编码失败 → 明确回给用户
            raise HTTPException(status_code=400, detail=redact(str(exc))) from exc

        seconds = probe_duration(out_mp4) or (len(saved) / max(1, fps))
        result: dict = {
            "status": "ok",
            "frames": len(saved),
            "fps": fps,
            "seconds": round(seconds, 2),
            "size_mb": round(out_mp4.stat().st_size / (1024 * 1024), 2),
            "local_path": str(out_mp4),
        }
        if seconds > MAX_REF_VIDEO_SECONDS:
            # 不阻断——用户可以自己剪短；但必须明确告知，否则等到云端才失败
            result["warning"] = (
                f"视频 {seconds:.1f} 秒，超出参考视频上限 {MAX_REF_VIDEO_SECONDS} 秒。"
                "云端可能拒绝；建议缩短片段或降低帧率。"
            )

        if do_upload:
            from app.services.providers import resolve_provider

            provider, why = resolve_provider("video_gen")
            if provider != "minimax":
                raise HTTPException(
                    status_code=400,
                    detail=f"当前视频模型是「{provider or '未选择'}」，"
                           f"上传换 file_id 只实现了 MiniMax。{why}",
                )
            key = str(rs.get("minimax_api_key") or settings.minimax_api_key or "").strip()
            if not key:
                raise HTTPException(status_code=400,
                                    detail="未配置 MiniMax API Key，无法上传。")
            base = str(rs.get("minimax_base_url") or settings.minimax_base_url or "")
            try:
                ref = upload_for_video_generation(out_mp4, key, base_url=base)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=redact(str(exc))) from exc
            result["reference_video"] = ref
            result["expires_in_days"] = 7
        return result
    finally:
        # 帧占位置且没有保留价值（mp4 已生成）；mp4 本身保留，便于用户查看与重试
        shutil.rmtree(frames_dir, ignore_errors=True)


def _run_video(session_id: str, user_id: str, user_input: str, images: list, script: dict,
               video_count: int | None = None, reference_video: str = "") -> None:
    """后台线程：合成视频并写回会话 payload（不阻塞事件循环）。

    `reference_video` 非空时走**参考驱动**（白模录像复刻运镜与走位）——
    这就是 `/api/video/whitebox` 上传后拿到的 `mm_file://{file_id}` 的去处。
    """
    from app.core.config import settings

    store = get_session_store()
    try:
        out, produced = get_video_service().synthesize(
            images, script, str(settings.output_dir_path), session_id,
            video_count=video_count, text_prompt=user_input,
            reference_video=reference_video)
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
