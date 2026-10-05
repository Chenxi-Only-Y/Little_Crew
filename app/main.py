"""小剧组 (Little Crew) — FastAPI 服务入口（异步任务 + 状态轮询）。

端点：
  GET  /health                   健康检查
  POST /api/crew/run             异步启动一次创作 -> {status, session_id}
  GET  /api/crew/status/{id}     轮询任务状态/进度/结果
  POST /api/crew/resume          对暂停的确认点续跑
  GET  /api/crew/history/{uid}   用户创作历史

生产化：请求日志（脱敏）、统一异常兜底。
注：**不做限流/配额**（开源非商业项目，用户 2026-10-02 决定）。
"""

import os
import pathlib
import re
import shutil
import time
import uuid
import threading

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.config import settings
from app.services.auth import get_user_store
from app.services.events import get_hub
from app.services.logging import redact, setup_logging
from app.services.profile import get_profile_store
from app.services.runtime_settings import (
    DecryptError,
    set_many as rs_set,
    snapshot as rs_snap,
)
from app.services.session_store import get_session_store
from app.services.task_runner import cancel_run, resume_run, start_run
from app.services.video import get_video_service

logger = setup_logging()

#: 是否暴露交互式 API 文档（`/docs`、`/redoc`、`/openapi.json`）。
#:
#: 默认**关闭**：这些页面未鉴权即可访问，会把完整 API 结构（含
#: 每个端点的路径与请求体字段）交给任何人——正好是构造越权请求的说明书。
#: 本机开发想用就设 `LITTLE_CREW_DOCS=1`（或 `.env` 里 `ENABLE_DOCS=1`）。
_DOCS_ENABLED = str(
    os.environ.get("LITTLE_CREW_DOCS")
    or getattr(settings, "enable_docs", False)
).lower() in ("1", "true", "yes", "on")

app = FastAPI(
    title="小剧组 Little Crew",
    description="基于 LangGraph 多智能体协作的 AI 漫剧自动生成平台",
    version="0.3.0",
    docs_url="/docs" if _DOCS_ENABLED else None,
    redoc_url="/redoc" if _DOCS_ENABLED else None,
    openapi_url="/openapi.json" if _DOCS_ENABLED else None,
)


# 免鉴权路径：**逐个精确列出**。
#
# ⚠️ 这里必须用**精确匹配**，不能用 `startswith` 前缀匹配。
# 原先用 `p.startswith(x for x in _HEALTH_PUBLIC)`，后果是：
#   白名单里的 `/api/asset` 顺手把 `/api/asset/upload`（匿名可写盘、无大小
#   限制）也一并公开了；它原本只服务于已删除的 Streamlit 前端，
#   前端删掉后就成了没人用的公开匿名写盘入口。
#   同类地 `/api/registry` 也会放行 `/api/registry/validate`。
# 精确匹配后，新增端点不会因为命名碰巧以某个前缀开头而意外公开——
# 要公开必须显式加到这里，这才是"公开"应有的成本。
_PUBLIC_PATHS = frozenset({
    "/",
    "/health",
    # 认证：登录/注册本身就必须免鉴权
    "/api/auth/login",
    "/api/auth/register",
    # 静态结构 schema：纯定义，不含任何用户数据或密钥。
    # 前端需要在登录前就能渲染导演台骨架与提供者清单，故公开。
    "/api/registry",
    "/api/providers",
    "/api/providers/modes",
    # 画布拓扑校验：纯内存的规则计算（不读库、不调外部服务、无副作用），
    # 前端在**登录前**就要用它判断"这条连线能不能接"，所以必须免鉴权。
    # 注意它是**显式列出**的：原先靠 `/api/registry` 的前缀顺带放行，
    # 这样"公开"就没有成本、容易失控。有测试守住这份清单。
    "/api/registry/validate",
})

# 接口文档：**仅当显式开启时才公开**（见 `_DOCS_ENABLED`）。
# 未开启时 FastAPI 根本不注册这些路由，所以这里也不必列出——
# 列出反而会让"公开清单"与真实路由不一致（有测试校验每个公开路径都是真实路由）。
if _DOCS_ENABLED:
    _PUBLIC_PATHS |= {"/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"}


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
    # 精确匹配（见 `_PUBLIC_PATHS` 的说明）。末尾多余的 `/` 容错一下，
    # 否则 `/health/` 这种写法会得到 401，而它显然是同一个路径。
    if path in _PUBLIC_PATHS or (path.endswith("/") and path[:-1] in _PUBLIC_PATHS):
        return await call_next(request)

    # 两种凭据来源都要接受（SSE 的 EventSource 无法自定义请求头）。
    # 关键：**在这里一次性解析出身份并写进 request.state**，
    # 之后所有端点都只能从它读身份——绝不允许端点自己拿请求体里的
    # `user_id` 当回退值（见 `_auth_user` 的说明，那是一个真实出现过的越权）。
    #
    # ⚠️ 重复的 `Authorization` 头一律拒绝。
    # `headers.get()` 取**第一个**，而某些反向代理取**最后一个**——
    # 在"代理做鉴权、应用做授权"的部署里，两边可能按不同身份判断，
    # 攻击者只要发两个头就能制造分歧。与其赌代理的行为，不如直接拒绝。
    auth_values = request.headers.getlist("Authorization")
    if len(auth_values) > 1:
        logger.warning("[auth] 请求带有 %d 个 Authorization 头，已拒绝：%s",
                       len(auth_values), path)
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "请求包含重复的 Authorization 头"},
        )
    auth = auth_values[0] if auth_values else ""
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        token = request.query_params.get("token", "").strip()
    username = get_user_store().resolve(token) if token else None
    if username:
        request.state.user = username
        try:
            # 把当前用户放进**请求作用域**，这样任何端点内部调
            # `runtime_settings.get(...)` 都能读到该用户的设置。
            #
            # 为什么不能只靠 `set_user()`（threading.local）：
            # 它不跨线程传播——中间件在事件循环线程设置，而**同步端点**
            # 跑在 AnyIO worker 线程里，读到的会是 `_global`。
            # 实测危害：`ffmpeg_path` 之类的服务内部设置「填了不生效」。
            from app.services import runtime_settings as _rs

            _rs.set_request_user(username)
        except Exception:  # noqa: BLE001  身份已定，这里失败不该拦下请求
            logger.exception("[auth] 设置请求作用域用户失败")
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


@app.exception_handler(DecryptError)
async def decrypt_error_handler(request: Request, exc: DecryptError):
    """密钥/密文问题 → **把可操作的提示透给用户**，不要吞成通用 500。

    为什么单独注册：`DecryptError` 的文案是精心写的、唯一有用的排障指引
    （例如"请先修复密钥文件 `.little_crew.key`，数据仍在库里"）。
    但原先全仓没有处理器，于是它落到下面的通用兜底，
    用户只看到"服务内部错误，请稍后重试"——**完全不知道该做什么**。
    实测：换掉密钥后 `PUT /api/settings` 返回 500 且不含任何指引。

    用异常处理器而不是在每个调用点 try/except：这是**环境级**故障，
    会从多个路径抛出（`put_settings`、`link_platform`、以及将来任何写设置的
    地方），集中处理才不会漏。

    ⚠️ 对外文案**不能原样回显异常字符串**：里面含服务端文件系统的**绝对路径**
    与内部类名（例如 `密钥文件不可用：C:\\Users\\...\\.little_crew.key（ValueError）`）。
    那既泄漏部署结构、对用户也毫无帮助（他关心的是"该做什么"，不是路径）。
    所以：详细原因只进日志，对外只给**固定文案 + 可操作 hint**。
    """
    logger.error("settings storage error on %s: %s",
                 request.url.path, redact(str(exc)), exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={
            "status": "error",
            "message": "本地加密数据无法读取或写入，本次操作已中止（未改动任何数据）。",
            "hint": "这是本地数据加密/密钥问题，不是操作错误。"
                    "请检查 data/.little_crew.key 是否被替换或删除；"
                    "详细原因见服务端日志。",
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    """全局异常兜底：只回显脱敏后的通用信息，细节进日志。"""
    logger.error("unhandled error on %s: %s", request.url.path, redact(str(exc)), exc_info=exc)
    return JSONResponse(status_code=500, content={"status": "error", "message": "服务内部错误，请稍后重试。"})


@app.get("/")
def root():
    """根路径：返回服务信息。"""
    info = {"message": "Little Crew API", "health": "/health"}
    if _DOCS_ENABLED:
        info["docs"] = "/docs"
    return info


@app.get("/health")
def health():
    """健康检查。"""
    return {"status": "ok", "service": "little-crew"}


# ------------------------------------------------------------------
# 认证：注册 / 登录 / 解析当前用户
# ------------------------------------------------------------------
def _auth_user(request: Request) -> str:
    """返回**已认证的**用户名；未认证时返回空串。

    ⚠️ 身份只有一个来源：中间件在 `request.state.user` 里放的那个值。
    它由 `auth_guard` 从 `Authorization: Bearer` **或** `?token=` 解析后写入，
    两者都已验证有效。

    这里**不再回退到调用方给的 fallback**——那是一个真实出现过的高危越权：
        `auth_guard` 接受 `?token=` 并放行，而本函数原来只看 Authorization 头，
        于是 `?token=<攻击者的>` + `body.user_id=<受害者>` 就能冒充受害者。
        实测：攻击者用自己 token 成功改写了受害者的 `llm_base_url`。
        更严重的是 `ffmpeg_path` 在白名单里，会被 `subprocess.run` 直接执行。
    所以任何"由请求体/路径决定身份"的写法都必须去掉；调用方若要区分
    "未登录"，请判断返回值是否为空串。
    """
    user = getattr(request.state, "user", "")
    if user:
        return str(user)
    # 兼容：万一某个请求没走中间件（例如直接单元测试里调用端点函数），
    # 仍然只认 Authorization 头——绝不使用 fallback。
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        resolved = get_user_store().resolve(auth[7:].strip())
        if resolved:
            return resolved
    return ""


def _require_owner(session_id: str, request: Request) -> dict:
    """校验当前登录用户对该 session 有属主权限；返回该会话行，否则抛 404/403。

    auth_guard 已保证这些端点带有效 token（Bearer 或 `?token=`），
    且身份只在中间件解析一次并写入 `request.state.user`（见 `_auth_user`），
    所以这里拿到的一定是认证用户本身，不会受请求体影响。
    任何按 session 操作的端点都必须以它为准，否则任意登录用户可读写他人会话(IDOR)
    —— 注意实现比"宽松放行"更严：属主不匹配一律 403，**没有**任何
    "匿名/遗留会话放行"的分支（早期注释这么写过，但代码从未如此）。
    """
    row = get_session_store().get(session_id)
    if not row:
        raise HTTPException(status_code=404, detail="会话不存在")
    owner = _auth_user(request)
    if owner != row.get("user_id"):
        # 匿名/遗留会话仅允许 "local" 或未登录态访问，登录用户不可越权。
        raise HTTPException(status_code=403, detail="无权访问该会话")
    return row


#: 合法的会话 ID：只允许 URL 安全字符，且限长。
#: 它同时被用作目录名，所以这条正则也是**路径穿越防线**（见 `_claim_session`）。
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _claim_session(session_id: object, user_id: str):
    """校验客户端带来的 `session_id` 是否可归当前用户使用。
    Returns:
        None  = 可以继续（id 为空＝新建；或已存在且属主是自己；或尚不存在）
        JSONResponse = 应当直接返回给客户端的 400/403

    ⚠️ 为什么必须有"属主"这一层：
        `SessionStore.save` 是 `ON CONFLICT(session_id) DO UPDATE`，**会把
        user_id 一起改写**。若不校验就接受客户端给的 id，
        另一个登录用户只要把别人的 session_id 放进请求体，就能把属主改成自己——
        之后 `_require_owner` 反而认为他是主人，他便可持续读状态、
        触发 resume，并因 thread_id 相同而复用对方的 checkpoint。
        （实测：B 用 A 的 id 建会话后，读到的是
          `{"status":"running","phase":"writer","user_id":"iso_b"}`。）

    ⚠️ 为什么还要校验**格式**：
        `session_id` 会被拼进输出目录（`output/<user>/<部门>/<职业>/<session_id>/`）。
        未校验时 `../../..` 能在输出根之外建目录并写文件（实测目录被真实创建）。
        限制为 `[A-Za-z0-9_-]{1,64}` 从源头掐掉路径穿越。
    """
    sid = str(session_id or "").strip()
    if not sid:
        return None
    if not _SESSION_ID_RE.fullmatch(sid):
        logger.warning("[auth] 用户 %s 提交了非法 session_id：%r", user_id, sid[:80])
        return JSONResponse(
            status_code=400,
            content={"status": "error",
                     "message": "非法 session_id（只允许字母、数字、下划线、连字符，1~64 位）"},
        )
    existing = get_session_store().get(sid)
    if existing and existing.get("user_id") not in (user_id,):
        logger.warning("[auth] 用户 %s 试图占用他人会话 %s（属主 %s）",
                       user_id, sid, existing.get("user_id"))
        return JSONResponse(
            status_code=403,
            content={"status": "error", "message": "该会话不属于当前用户"},
        )
    return None


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


@app.post("/api/auth/logout")
async def logout(request: Request):
    """登出：吊销当前 token。

    原先**没有登出**：token 签发后只能等 90 天过期，用户（或管理员）
    没有任何主动收回凭据的手段——而 token 又是长期有效的。
    这里同时接受 `Authorization` 头与 `?token=`（与 SSE 一致的口径）。
    """
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        token = (request.query_params.get("token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="缺少 token")
    ok = get_user_store().logout(token)
    # 即使没命中（已过期/已登出）也返回 ok：登出是幂等的，
    # 且不区分"有效/无效"以免变成 token 有效性探测接口。
    return {"status": "ok", "revoked": ok}


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
    uid = _auth_user(request)
    return {"status": "ok", "user_id": uid, "settings": _mask_secrets(rs_snap(uid))}


@app.put("/api/settings")
async def put_settings(payload: dict, request: Request):
    """更新指定用户的运行时 API 配置（传 None=保留，传空串=清除覆盖回退默认）。

    白名单**由注册表派生**（`registry.api.allowed_settings()`），不再手写：
    否则注册表新增配置键时，界面上能改、却会被这里静默丢弃（已实际发生过）。
    """
    from app.registry import api as registry

    uid = _auth_user(request)
    settable = registry.allowed_settings()
    allowed = {k: payload[k] for k in settable if k in payload}
    # 丢弃掩码哨兵，避免把掩码回写覆盖真实密钥；空串=清除覆盖
    cleaned = {k: v for k, v in allowed.items()
               if v is not None and not (_is_secret_key(k) and v == _MASK_SENTINEL)}
    rs_set(uid, cleaned)
    return {"status": "ok", "user_id": uid, "settings": _mask_secrets(rs_snap(uid))}


@app.get("/api/providers/models")
async def list_provider_models(request: Request, capability: str = "llm", role: str = ""):
    """拉取当前已配置提供者的**可用模型清单**（`GET {base_url}/models`）。

    用户问题："本地 vLLM / Ollama"下面"模型"一栏是空的，怎么知道有哪些模型。

    注册表里的 `known_models` 是**云端模型名**，对本地服务无用；
    本地装了什么只有本地服务自己知道。所以直接问它。

    ## 为什么参数里没有 base_url / api_key

    地址与密钥**只从服务端已保存的设置里解析**（`llm.fetch_models` 内部走
    `_resolve(role)`）。这是刻意的：

      · `/api/settings/test` 的 `base_url` 来自请求体，所以它**必须禁止
        localhost**，否则任意登录用户能让服务端访问本机管理接口；
      · 而列模型**必须**能访问本机（Ollama 11434 / vLLM 8000）。

    不接受调用方传地址，本机地址就能安全放行 —— SSRF 面为零，
    因为用户无法让服务端请求他指定的任意地址。

    Query:
        capability: 目前只支持 llm（图像/视频提供者没有统一的 /models 端点）
        role:       按某职业的设置解析（留空 = 全局设置）
    """
    cap = (capability or "llm").strip().lower()
    if cap != "llm":
        raise HTTPException(status_code=400,
                            detail=f"暂时只支持 llm 的模型清单（收到 {cap}）")
    _auth_user(request)

    from app.services.llm import fetch_models

    res = fetch_models(role=(role or "").strip())
    return {"status": "ok" if res["ok"] else "error", **res}


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

    # SSRF 防护：这个端点的 base_url **完全由调用方决定**，而服务端会带着
    # 调用方给的 key 去 POST 它，并把响应前 200 字符回显给调用方。
    # 不加校验就是"认证用户可让服务端访问任意内网/本机地址并读回响应"。
    # 另外 `{base}/v1/...` 这种拼接允许用 `?`/`#` 改写实际请求路径，一并拒绝。
    if base_url:
        from urllib.parse import urlparse as _urlparse

        parsed = _urlparse(base_url)
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise HTTPException(
                status_code=400,
                detail="base_url 不允许包含查询串、片段或用户信息（可能被用来改写实际请求路径）",
            )
        # localhost 也不放行：测试端点没有任何"必须访问本机"的场景，
        # 而放行它等于把本机管理接口（Docker/ES/ComfyUI 等）暴露给任意登录用户。
        from app.services.url_guard import assert_public_url

        try:
            assert_public_url(base_url, context="base_url")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=redact(str(exc))) from exc
        if _urlparse(base_url).hostname in ("localhost", "127.0.0.1", "::1"):
            raise HTTPException(
                status_code=400,
                detail="base_url 不允许指向本机（该端点无访问本机的必要）",
            )


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
    uid = _auth_user(request)
    return {"status": "ok", "profile": get_profile_store().get(uid)}


@app.put("/api/user/profile")
async def put_profile(payload: dict, request: Request):
    """更新当前用户的个人资料（display_name / avatar / avatar_color）。"""
    uid = _auth_user(request)
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

    body: {name, canvas:{nodes,edges,viewport}, settings_snapshot?, project_id?, force?}
    `project_id` 存在则覆盖该工程（用于"另存/更新"）。
    `force=true` 才允许覆盖一份**解不开**的既有工程（需用户明确确认）。
    """
    uid = _auth_user(request)
    if not uid:
        raise HTTPException(status_code=401, detail="未登录")
    from app.services.project_store import ProjectUnreadable, get_project_store

    body = payload or {}
    try:
        summary = get_project_store().save(
            user_id=uid,
            name=str(body.get("name") or "未命名工程"),
            canvas=body.get("canvas") or {},
            settings_snapshot=_mask_for_project(body.get("settings_snapshot") or {}),
            project_id=body.get("project_id") or None,
            allow_overwrite_unreadable=bool(body.get("force")),
        )
    except ValueError as exc:            # 体积超限等
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except PermissionError as exc:       # 覆盖他人工程
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ProjectUnreadable as exc:     # 覆盖读不懂的旧工程 → 需显式确认
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "ok", **summary}


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str, request: Request):
    """读取工程。不存在或不属于当前用户都返回 404（不暴露他人工程 id）。

    ⚠️ 若工程存在但**解不开**，返回 409 而不是"空工程"。
    返回空工程会让用户以为工程本来就是空的、随手保存一次就把**能救回**的数据
    永久覆盖（原先就是 `except: doc = {}`）。409 的 detail 会说明要修
    `.little_crew.key`。见 `ProjectUnreadable`。
    """
    uid = _auth_user(request)
    if not uid:
        raise HTTPException(status_code=401, detail="未登录")
    from app.services.project_store import ProjectUnreadable, get_project_store

    try:
        doc = get_project_store().get(project_id, uid)
    except ProjectUnreadable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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

    uid = _auth_user(request)
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
    from app.services.providers import CONDITIONING_LABELS, PROVIDERS, provider_payload

    # 提供者 → 需要的凭据字段（设置项 key）。
    # 为什么要下发这个：视频的密钥是**提供者级**的（`minimax_api_key` 等），
    # 与 LLM/图像的 `*_api_key_{角色}` 命名不同。前端若按前缀猜，
    # 就会出现"白名单里有、界面却没输入框"，用户根本没法配——
    # 这是本项目实际发生过的问题。
    from app.services.video_adapters import VIDEO_PROVIDERS, provider_credential_spec

    cred_by_provider: dict[str, list[str]] = {
        p.key: ([p.api_key_setting] if p.api_key_setting else [])
        for p in VIDEO_PROVIDERS
    }

    items = [
        {
            # ⚠️ 字段集由 `providers.provider_payload()` **唯一**决定。
            # 端点这里只补它不知道的东西：`conditioning_labels`（要把
            # ConditioningKind 翻成中文）与 `credential_keys`（视频密钥名
            # 只有 video_adapters 知道）。
            #
            # 曾经这里把 ProviderSpec 的字段**另抄一遍**，于是
            # `is_local` / `api_style` 只加在了 `to_payload()` 里、
            # 没加到端点里 —— 前端读到的 /api/providers 缺这两个字段，
            # 按模式分组的逻辑完全不生效。两份手写清单必然漂移。
            **provider_payload(p, credential_keys=cred_by_provider.get(p.key, [])),
            # 该提供者能接受的**结构条件**（仅 video_gen 有意义）。
            # 白模据此决定产出什么——见 providers.previs_plan()。
            "conditioning": list(p.conditioning),
            "conditioning_labels": [CONDITIONING_LABELS.get(c, c) for c in p.conditioning],
        }
        for p in PROVIDERS
        if (not capability or p.capability == capability)
        and (include_unavailable or p.available)
    ]
    return {
        "status": "ok",
        "count": len(items),
        "providers": items,
        # 设置页渲染"提供者凭据"区域用；键名与 PUT /api/settings 完全一致
        "credential_spec": provider_credential_spec(),
    }


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

    uid = _auth_user(request)
    from app.services import runtime_settings as rs

    rs.set_user(uid)
    try:
        plan = build_plan()
    finally:
        rs.set_user(None)
    return {"status": "ok", "plan": plan}


# ------------------------------------------------------------------
# 白模：由 LLM 生成规格（JSON），再由前端渲染成体块
# ------------------------------------------------------------------
@app.post("/api/previs/white-model")
def generate_white_model(payload: dict, request: Request):
    """按角色描述生成一份**白模规格**（JSON）。

    为什么让 LLM 生成 JSON 而不是直接生成 Three.js 代码：见
    `app/services/white_model.py` 开头的对照表（安全/可校验/可存/可复现/
    可人工修改）。渲染那一步是前端写死的代码，所以不会因模型抽风而崩。

    载荷：
        description  必填，一句话角色描述
        name         可选，角色名
        pose         可选，idle/walk/run/sit/wave/point
        heightHint   可选，体型提示（"矮胖""纤细"之类）

    返回 `{status:"ok", spec, attempts}` 或 400 + 可读原因。
    """
    uid = _auth_user(request)
    from app.services import runtime_settings as rs

    desc = str((payload or {}).get("description") or "").strip()
    if not desc:
        raise HTTPException(status_code=400, detail="请先描述一下这个角色")
    if len(desc) > 400:
        desc = desc[:400]

    name = str((payload or {}).get("name") or "").strip()[:40]
    pose = str((payload or {}).get("pose") or "").strip()
    height_hint = str((payload or {}).get("heightHint") or "").strip()[:80]

    from app.services.white_model_prompt import generate_spec

    rs.set_user(uid)
    try:
        result = generate_spec(desc, name=name, pose=pose, height_hint=height_hint)
    finally:
        rs.set_user(None)

    if result.get("status") != "ok":
        # 400 而不是 500：这是"模型没给出合规格的东西"，用户重试或改描述即可，
        # 不是服务端故障。raw 一并返回，方便 C 模式里让用户自己改。
        raise HTTPException(
            status_code=400,
            detail=redact(str(result.get("message") or "生成失败")),
        )
    return result


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

    user_id = _auth_user(request)

    # 客户端可以带 session_id（便于重试/续跑），但**必须先确认它属于自己**：
    # `SessionStore.save` 是 upsert，会把 user_id 一起改写——若无校验，
    # 登录用户 B 只要把 A 的 session_id 放进 body，就能把属主改成自己，
    # 之后 `_require_owner` 反而认为 B 是主人，B 可读状态、续跑并复用 A 的
    # checkpoint（实测确认过）。见 `_claim_session`。
    claimed = _claim_session(payload.get("session_id"), user_id)
    if claimed is not None:
        return claimed
    session_id = str(payload.get("session_id") or "").strip() or uuid.uuid4().hex[:8]
    get_hub().reset(session_id)          # 新一轮：清掉上一轮的事件历史
    # 工程名 → 产出路径的第二层（账号/项目名/部门/职业/会话/类型/文件）
    start_run(
        session_id, user_input, user_id,
        seed=payload.get("seed") or None,
        project=str(payload.get("project") or "").strip()[:80],
    )
    return {"status": "accepted", "session_id": session_id, "user_id": user_id}


@app.get("/api/sessions/{session_id}/events")
async def session_events(session_id: str, request: Request, token: str | None = None):
    """实时事件流（SSE）：阶段状态、AI 思考过程、确认点、整体状态。

    为什么用 SSE 而不是 WebSocket：只需服务端单向推送，浏览器原生支持断线重连，
    实现与部署都更简单（见 docs/recon/04-frontend-rewrite.md §2）。

    鉴权说明：浏览器 `EventSource` **无法自定义请求头**，因此这里额外接受
    `?token=` 查询参数；同时保留 `Authorization` 头（便于 fetch-based 客户端）。
    非 loopback 部署务必用 HTTPS，否则 token 会在 URL 中明文传输。

    ⚠️ 光"token 有效"不够：还必须确认**这个 token 的主人就是会话属主**。
    否则任何登录用户都能订阅他人会话的事件流，**实时**读走 AI 思考日志与
    确认点内容（剧本/提示词/用户输入）。原先这里只判了 token 有效性，
    而相邻的 `/events/snapshot` 反而判了属主——不一致本身就是信号。
    用 `_require_owner`：不存在→404、非属主→403（都是 JSON，不是 SSE 帧）。
    """
    from app.services.events import get_hub as _hub

    # 鉴权：优先 Authorization 头，其次 query token
    auth = request.headers.get("Authorization", "")
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    supplied = bearer or (token or "")
    if not supplied or not get_user_store().resolve(supplied):
        return JSONResponse(status_code=401, content={"status": "error", "message": "未登录"})

    # 属主校验（越权订阅）
    _require_owner(session_id, request)

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
    """异步启动一次创作（后台运行，轮询 /status 取进度）。

    注：**不做限流/配额**（用户 2026-10-02 决定：本项目是开源项目、非商业用途，
    不替使用者的成本做拦截）。自部署者若担心被刷，请在反向代理层做限制。
    """
    user_input = (payload.get("user_input") or "").strip()
    if not user_input:
        return {"status": "error", "message": "缺少 user_input"}
    user_id = _auth_user(request)

    # 同 `/api/sessions`：客户端给的 session_id 必须先确认属于自己（见 `_claim_session`）
    claimed = _claim_session(payload.get("session_id"), user_id)
    if claimed is not None:
        return claimed
    session_id = str(payload.get("session_id") or "").strip() or uuid.uuid4().hex[:8]
    # 工程名 → 产出路径的第二层（账号/项目名/部门/职业/会话/类型/文件）
    start_run(
        session_id, user_input, user_id,
        seed=payload.get("seed") or None,
        project=str(payload.get("project") or "").strip()[:80],
    )
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
    resolved = _auth_user(request)
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

    # 鉴权：`auth_guard` 是全局中间件，`/api/video` 不在 `_PUBLIC_PATHS` 里，
    # 所以本端点自动要求有效 token——不需要（也不能）在这里手动调它。
    #

    form = await request.form()
    raw_frames = [v for k, v in form.multi_items() if k == "frames"]
    if not raw_frames:
        raise HTTPException(status_code=400, detail="没有收到任何帧（表单字段应为 frames）。")
    do_upload = str(form.get("upload") or "") in ("1", "true", "yes")
    fps = int(str(form.get("fps") or 24))

    # 白模视频（预演导出的帧序列）此前落在 `outputs/whitebox/{随机 uuid}/`，
    # **完全没有归类**——既看不出属于哪个账号，也看不出是哪个职业产出的，
    # 更没和图片放在同一棵树里。
    #
    # 现在走统一规则（`app/services/asset_paths.py`）：
    #
    #     {outputs}/{账号}/{项目名}/{部门}/{职业}/{会话}/whitebox-{id}.mp4
    #     {outputs}/{账号}/{项目名}/{部门}/{职业}/{会话}/whitebox-{id}/00000.png...
    #
    # 归属职业取 `previs_artist`（白模预演师）——这就是它的产出者。
    # 帧与成片放同一层：`.png` 与 `.mp4` 靠扩展名区分，
    # 而帧用一个以成片同名的子目录隔开，避免多批次互相覆盖。
    from app.services import asset_paths

    role = "previs_artist"
    token = uuid.uuid4().hex[:12]
    work = asset_paths.asset_dir(settings.output_dir_path, role, "frames")
    frames_dir = work / f"whitebox-{token}"
    frames_dir.mkdir(parents=True, exist_ok=True)
    box_mp4 = work / f"whitebox-{token}.mp4"

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

        out_mp4 = box_mp4   # 统一路径：.../videos/whitebox-{id}.mp4
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

    ⚠️ **必须在这里 `set_context`**：后台线程不继承请求上下文，
    而 `runtime_settings` 读取运行时设置靠的是 threading.local / ContextVar。
    不设的话这一线程会用 `_global` 的设置，导致：
      · 用全局（而非该用户）的 API Key 与本地服务端点；
      · 产物按 `_global` 归类，落进错误的输出目录；
      · IP-Adapter 等"按用户"的缓存键退化成同一个值。
    实测过这类症状（产物落 `_global/`）。用 try/finally 保证线程结束前复位。
    """
    from app.core.config import settings
    from app.services.runtime_settings import set_context as _rs_set_ctx

    _rs_set_ctx(user_id, session_id)
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
    finally:
        # 线程结束前复位，避免"设置跟着线程泄漏"
        _rs_reset_for_thread()


def _rs_reset_for_thread() -> None:
    """把当前线程的运行时设置上下文复位。

    独立成函数是为了**可测**：测试可以直接调它验证复位行为，
    而不必真的起一个视频合成线程（那需要 ffmpeg 和几分钟）。
    """
    from app.services.runtime_settings import set_user as _set_user

    _set_user(None)


@app.post("/api/crew/reassess/{session_id}")
def reassess_quality(session_id: str, request: Request):
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
def retry_shots(session_id: str, request: Request, body: dict | None = None):
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
        # 后台线程不继承请求上下文 → 必须显式设，否则会用 `_global` 的设置
        # （他人的 API Key、错误的输出目录、退化的按用户缓存键）。见 `_run_video`。
        from app.services.runtime_settings import set_context as _rs_set_ctx

        _rs_set_ctx(user_id, session_id)
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
        finally:
            _rs_reset_for_thread()

    threading.Thread(target=_do_retry, daemon=True).start()
    return {"status": "accepted", "session_id": session_id, "retried": shot_ids}


@app.post("/api/crew/review/{session_id}")
def re_review(session_id: str, request: Request):
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
def regen_scenes(session_id: str, request: Request, body: dict | None = None):
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
def replan_rhythm(session_id: str, request: Request, body: dict | None = None):
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
def check_continuity(session_id: str, request: Request, body: dict | None = None):
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
def publish_crew(session_id: str, request: Request, body: dict | None = None):
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
    user_id = _auth_user(request)

    # ⚠️ 必须挡掉**掩码哨兵**再落库。
    # 该端点直接 `rs_set`，绕过了 `put_settings` 的哨兵过滤，而后端把
    # `publish_*_token` 回给前端时是掩码形态（`••••已配置`）。用户若把
    # 界面上看到的那串哨兵再提交回来（或前端回填了掩码值），
    # 就会用哨兵**覆盖掉真实令牌**——凭据被静默破坏，且不是泄漏、
    # 更难发现（下次发布才报"未授权"）。
    raw = body.get("token")
    if raw is not None and str(raw).strip() == _MASK_SENTINEL:
        raise HTTPException(
            status_code=400,
            detail="检测到掩码占位符（••••已配置），已拒绝写入以免覆盖真实令牌。"
                   "如需更换令牌请提交新的真实值；如需清除请传空串。",
        )
    # 空串=显式清除；未提供=沿用已有值
    if raw is None:
        existing = rs_snap(user_id).get(f"publish_{platform}_token")
        if existing:
            return {"status": "ok", "platform": platform, "linked": True,
                    "token": str(existing)[:8] + "…",
                    "settings_keys": [k for k in rs_snap(user_id) if "publish_" in k]}
        token: object = secrets.token_urlsafe(16)
    else:
        token = str(raw).strip()
    rs_set(user_id, {f"publish_{platform}_token": token})
    shown = str(token)[:8] + "…" if token else "(已清除)"
    return {"status": "ok", "platform": platform, "linked": bool(token),
            "token": shown,
            "settings_keys": [k for k in rs_snap(user_id) if "publish_" in k]}
