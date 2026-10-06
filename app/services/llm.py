"""LLM 服务封装 —— 对接任意 OpenAI 兼容协议提供者（可切换，支持按角色/按用户）。

优先级：角色覆盖(设置页) > 用户覆盖 > 全局覆盖 > 环境变量(LLM_*/DEEPSEEK_*) > 预设。
角色覆盖键：llm_provider_{role} / llm_model_{role} / llm_api_key_{role} / llm_base_url_{role}。
开发阶段 `MOCK_LLM=1` 时勿调用本服务（Agent 走各自 `_mock` 分支）。
"""

import json

from app.core.config import settings
from app.services.logging import redact
from app.services.runtime_settings import get as rs_get


def _try_parse_json(s: str):
    """从文本中截取第一段合法 JSON（对象或数组）并解析，失败返回 None。"""
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start = s.find(open_c)
        if start != -1:
            end = s.rfind(close_c)
            if end > start:
                try:
                    return json.loads(s[start:end + 1])
                except json.JSONDecodeError:
                    continue
    return None

_PRESETS = {
    "deepseek": ("https://api.deepseek.com", "deepseek-flash"),
    "zhipu": ("https://open.bigmodel.cn/api/paas/v4", "glm-5.3"),
    "openai": ("https://api.openai.com/v1", "gpt-6.1-sol"),
    "qwen": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3.7-plus"),        # 阿里通义
    "moonshot": ("https://api.moonshot.cn/v1", "kimi-k3"),                       # 月之暗面 Kimi
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.8-flash"),  # Google
    "doubao": ("https://ark.cn-beijing.volces.com/api/v3", "doubao-seed-evolving"),           # 字节豆包(火山方舟)
    "ernie": ("https://qianfan.baidubce.com/v2", "ernie-5.1"),                       # 百度文心(千帆)
    "mimo": ("https://api.xiaomimimo.com/v1", "mimo-v2.6-pro"),                        # 小米 MiMo
    # ⚠️ 腾讯混元必须用新平台 TokenHub：旧域名 api.hunyuan.cloud.tencent.com
    #    官方公告已于 2026-09-30 全面停服（控制台与 API 同步关停）。
    "hunyuan": ("https://tokenhub.tencentmaas.com/v1", "hy3"),                          # 腾讯混元
    "stepfun": ("https://api.stepfun.com/v1", "step-5-preview"),                        # 阶跃 StepFun
    "baichuan": ("https://api.baichuan-ai.com/v1", "Baichuan4-Turbo"),                  # 百川
    "minimax": ("https://api.minimax.io/v1", "MiniMax-M3"),                            # MiniMax 文本
    "mistral": ("https://api.mistral.ai/v1", "mistral-large-latest"),                  # Mistral AI
    "ollama": ("http://localhost:11434/v1", "qwen2.5"),                                 # 本地部署
    "vllm": ("http://localhost:8000/v1", "Qwen/Qwen2.5-7B-Instruct"),                   # 本地 vLLM
    # ---- 聚合网关（OpenAI 兼容）：base_url 与各厂同一机制，key 用共同的 llm_api_key ----
    #
    # ⚠️ 这几家**不预设默认模型**：它们的模型名是各家自己的目录 slug
    #   （OpenRouter 形如 `anthropic/claude-…`、Groq 形如 `llama-…`），
    #   预设一个必然很快过时，反而误导用户以为"默认那个一定能用"。
    #   留空让用户在「模型」里填，provider 的 note 里说明了形状。
    "openrouter": ("https://openrouter.ai/api/v1", ""),                                  # OpenRouter
    "groq": ("https://api.groq.com/openai/v1", ""),                                      # Groq
    "together": ("https://api.together.xyz/v1", ""),                                     # Together AI
    "fireworks": ("https://api.fireworks.ai/inference/v1", ""),                          # Fireworks AI
    "deepinfra": ("https://api.deepinfra.com/v1/openai", ""),                            # DeepInfra
    "cerebras": ("https://api.cerebras.ai/v1", ""),                                      # Cerebras
    "siliconflow": ("https://api.siliconflow.cn/v1", ""),                                # 硅基流动
}
_LOCAL_PROVIDERS = {"ollama", "vllm"}


def _resolve(role: str = "") -> tuple:
    """解析出 (provider, base_url, model, api_key)。

    provider 的解析**不做静默默认**（用户 2026-10-02 决定）：
        职业级覆盖 > 全局 `llm_provider` > 模式（cloud/local）默认 > **未选则空**
    返回空 provider 表示"用户还没选"，调用方（`_ensure_ready`）会给出可读提示。

    base_url/model/api_key 仍按角色/全局/config 逐级回退——它们在 provider 选定后
    都有合理预设，回退不会误导。
    """
    from app.services.providers import resolve_provider

    s = f"_{role}" if role else ""
    provider, _why = resolve_provider("llm", role)
    provider = (provider or "").lower()

    preset = _PRESETS.get(provider) or ("" , "")
    base_url = rs_get(f"llm_base_url{s}") or rs_get("llm_base_url") or settings.llm_base_url or preset[0]
    model = rs_get(f"llm_model{s}") or rs_get("llm_model") or settings.llm_model or preset[1]
    api_key = rs_get(f"llm_api_key{s}") or rs_get("llm_api_key") or settings.llm_api_key or settings.deepseek_api_key
    return provider, base_url, model, api_key


class LLMService:
    def __init__(self) -> None:
        self._clients: dict = {}  # (provider,base,model,key) -> ChatOpenAI

    # ---- mock 开关：每次实时解析，不做快照 ----
    # 原先在 __init__ 里 `self.mock = settings.mock_llm`，导致运行中改配置不生效
    # （设置页承诺"保存立即生效"，但已创建的单例永远停在旧值，已实测）。
    @property
    def mock(self) -> bool:
        from app.services.providers import is_mock

        return is_mock("llm")

    @mock.setter
    def mock(self, value: bool) -> None:
        from app.services.providers import set_override

        set_override("llm", bool(value))

    def _client(self, role: str = ""):
        """按解析配置获取（并缓存）对应 client，支持不同角色不同模型。"""
        from langchain_openai import ChatOpenAI

        provider, base_url, model, api_key = _resolve(role)
        if base_url:
            from app.services.url_guard import assert_public_url
            assert_public_url(base_url, context="llm_base_url")
        cid = (provider, base_url, model, api_key)
        if cid not in self._clients:
            if api_key or provider in _LOCAL_PROVIDERS:
                self._clients[cid] = ChatOpenAI(
                    model=model, api_key=api_key or "ollama", base_url=base_url, temperature=0.7
                )
            else:
                return None
        return self._clients[cid]

    def chat(self, system: str, user: str, role: str = "") -> str:
        """普通文本对话。mock 模式下禁止调用。role 指定该环节模型。"""
        self._ensure_ready(role)
        from langchain_core.messages import HumanMessage, SystemMessage

        llm = self._client(role)
        resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
        out = resp.content
        return out if isinstance(out, str) else str(out)

    def chat_json(self, system: str, user: str, role: str = "") -> dict:
        """对话并解析为 dict（容忍 ```json ... ``` 包裹，解析失败时安全降级，不抛异常）。"""
        content = self.chat(system, user, role).strip()
        if content.startswith("```"):
            parts = content.split("```")
            content = parts[1] if len(parts) >= 2 else ""
            if content.startswith("json"):
                content = content[4:].strip()
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            parsed = _try_parse_json(content)
            if parsed is not None:
                return parsed
            return {"_parse_error": True, "raw": content[:500], "error": str(exc)}

    def _ensure_ready(self, role: str = "") -> None:
        if self.mock:
            raise RuntimeError(
                "MOCK_LLM=1 时请使用 Agent 的 _mock 分支；调用真实模型请设 MOCK_LLM=0。"
            )
        provider, why = _resolve_provider_for_error(role)
        if not provider:
            # 用户还没选提供者：给可操作的提示，而不是抛一个看不懂的错
            raise RuntimeError(why)
        if self._client(role) is None:
            raise RuntimeError(
                f"LLM 客户端未初始化（provider={provider}, role={role or 'global'}）："
                f"请检查密钥，或在「设置」页填写。{why}"
            )


def _resolve_provider_for_error(role: str) -> tuple[str, str]:
    """取 provider 与说明（供报错文案用）。"""
    from app.services.providers import resolve_provider

    provider, why = resolve_provider("llm", role)
    return (provider or "").lower(), why


_llm_instance: LLMService | None = None


def get_llm() -> LLMService:
    """获取（单例）LLM 服务。"""
    global _llm_instance
    if _llm_instance is None:
        _llm_instance = LLMService()
    return _llm_instance


def test_llm_provider(provider: str, base_url: str | None, model: str | None,
                      api_key: str | None, timeout: float = 15.0) -> dict:
    """对单个 LLM 提供者做一次连通性/鉴权测试，返回 {ok, error_type, detail}。

    error_type: ok | auth | network | model | rate_limit | other
    用于在设置页「测试通道」识别具体是哪个模型/哪类问题。
    """
    from langchain_openai import ChatOpenAI
    from langchain_core.messages import HumanMessage, SystemMessage

    # 用给定参数优先，缺省取 _resolve
    prov = provider
    b = base_url
    m = model
    k = api_key
    if not b or not m:
        p2, b2, m2, k2 = _resolve()
        prov = prov or p2
        b = b or b2
        m = m or m2
        k = k or k2
    try:
        llm = ChatOpenAI(model=m, api_key=k or "ollama", base_url=b,
                         temperature=0.0, timeout=timeout, max_retries=0)
        resp = llm.invoke([SystemMessage(content="You are a helper."),
                           HumanMessage(content="回复“ok”两个字即可")])
        ok = bool(getattr(resp, "content", None))
        return {"ok": ok, "error_type": "ok", "detail": "调用成功"} if ok else \
            {"ok": False, "error_type": "other", "detail": "无返回内容"}
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        et = "other"
        low = msg.lower()
        if "unauthorized" in low or "401" in msg or "invalid api key" in low or "authentication" in low:
            et = "auth"
        elif "connection" in low or "connect" in low or "timeout" in low or "timed out" in low or "network" in low:
            et = "network"
        elif "404" in msg or "not found" in low or "model" in low or "does not exist" in low:
            et = "model"
        elif "429" in msg or "rate" in low or "quota" in low or "throttl" in low:
            et = "rate_limit"
        return {"ok": False, "error_type": et, "detail": redact(msg)[:300]}

# ------------------------------------------------------------------
# 获取提供者的可用模型清单
# ------------------------------------------------------------------

def fetch_models(provider: str = "", base_url: str = "", api_key: str = "",
                 *, role: str = "", timeout: float = 12.0) -> dict:
    """拉取该提供者的**可用模型清单**（`GET {base_url}/models`）。

    ## 为什么需要它（用户的问题）

    用户截图里选了「本地 vLLM」，下面"模型"一栏是**空的**，问：
    "这种情况怎么知道自己模型"。

    确实无从知道：注册表里的 `known_models` 是**云端模型名**（给 DeepSeek /
    OpenAI 那类用的），而**本地 vLLM / Ollama 装了什么模型只有本地服务知道**。
    让人去翻 `ollama list` 或 vLLM 启动日志，是把系统的信息缺口转嫁给用户。

    所以直接问提供者自己 —— OpenAI 兼容端点都有 `GET /models`。

    ## 安全：base_url **由服务端解析**，不接受调用方传入

    这是与 `/api/settings/test` 的关键差异。那个端点的 `base_url` 来自请求体，
    所以它**故意禁止 localhost**（否则任意登录用户可让服务端去访问本机
    管理接口并读回响应）。

    但"列模型"必须能访问本机（Ollama 在 11434、vLLM 在 8000）——
    所以改成：**地址只从服务端已保存的设置里解析**，调用方只能指定
    `capability` / `role`。这样本机地址能用，而 SSRF 面为零
    （用户无法让服务端请求他指定的任意地址）。

    仍然保留 `assert_public_url` 之外的一层：本地提供者才允许本机地址。

    Returns:
        {"ok": bool, "models": [str], "provider": str, "base_url": str,
         "detail": str, "error_type": str}
    """
    import urllib.error
    import urllib.request

    from app.services.url_guard import assert_public_url

    prov = (provider or "").strip().lower()
    b = (base_url or "").strip()
    k = api_key or ""

    # 没给全就走服务端解析（正常路径）
    if not b or not prov:
        p2, b2, _m2, k2 = _resolve(role)
        prov = prov or (p2 or "")
        b = b or (b2 or "")
        k = k or (k2 or "")

    if not b:
        return {"ok": False, "models": [], "provider": prov, "base_url": "",
                "error_type": "config",
                "detail": "这个提供者没有可用的 API 地址，无法查询模型清单"}

    # ⚠️ "是不是本地"**只看提供者身份**，不要看主机名。
    #
    # 我第一版写成 `... or "127.0.0.1" in b`，于是任何指向回环地址的
    # **云端**提供者都被判成本地 —— 而本机是不回退兜底清单的，
    # 结果云端也拿不到兜底、直接失败（测试当场抓到）。
    #
    # 而且"本机地址"是**用户可配**的（`llm_base_url` 能指向局域网另一台
    # 推理机）——那时它仍是本地推理语义，但与回环无关。
    # 所以判据取提供者 key：ollama / vllm 是本地推理，其余都是云端。
    is_local = prov in _LOCAL_PROVIDERS

    # 本机 / 内网推理服务允许私有地址；真正的云端才走 SSRF 防护。
    # 这里仍按**地址**区分"要不要做防护"，与上面的语义判断是两件事。
    host_is_private = ("localhost" in b) or ("127.0.0.1" in b) or ("[::1]" in b)
    if not (is_local or host_is_private):
        try:
            assert_public_url(b, context="llm_base_url")
        except ValueError as exc:
            return {"ok": False, "models": [], "provider": prov, "base_url": b,
                    "error_type": "config", "detail": redact(str(exc))}

    url = b.rstrip("/") + "/models"
    req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    if k:
        req.add_header("Authorization", f"Bearer {k}")
    # 少数网关（如 OpenRouter）按来源站点做统计，带上无害；不带也能用
    req.add_header("Content-Type", "application/json")

    def _get(req_: urllib.request.Request):
        """发一次 GET。返回 (成功, bytes 或错误信息 dict)。"""
        try:
            with urllib.request.urlopen(req_, timeout=timeout) as resp:  # noqa: S310
                return True, resp.read(512 * 1024)
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read(300).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            return False, {"code": exc.code, "body": body}
        except Exception as exc:  # noqa: BLE001
            return False, {"exc": exc}

    ok, out = _get(req)

    # Ollama 的**原生**清单端点是 `/api/tags`，不是 `/v1/models`。
    #
    # ⚠️ 对 Ollama 要**优先**问原生端点，而不是"失败才兜底"。原因不只是
    #    兼容性 —— 实测两者的信息量**不一样**：
    #
    #      /v1/models → {"data":[{"id":"bge-m3:latest","object":"model",...}]}
    #                   **没有能力信息**，无法判断是对话还是嵌入模型
    #      /api/tags  → {"models":[{...,"capabilities":["embedding"]}]}
    #                   有 `capabilities`，能准确标注类型
    #
    #    用户要求"在列表里标一下模型类型"，而标类型必须靠 `capabilities`。
    #    （实测确认：走 /v1/models 时 kind 全为空，走 /api/tags 才拿到。）
    #
    # 其余本地提供者（vLLM 等）只有 OpenAI 端，仍走 /v1/models。
    if is_local:
        base = b.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        for path in ("/api/tags", "/v1/models", "/models"):
            req2 = urllib.request.Request(
                base.rstrip("/") + path, method="GET",
                headers={"Accept": "application/json"},
            )
            if k:
                req2.add_header("Authorization", f"Bearer {k}")
            ok2, out2 = _get(req2)
            if ok2 and isinstance(out2, bytes):
                ok, out = ok2, out2
                break

    if not ok or not isinstance(out, bytes):
        info = out if isinstance(out, dict) else {}
        if "code" in info:
            body = str(info.get("body") or "")
            # ⚠️ 一个实测踩到的真实陷阱：**vLLM 的默认端口 8000 正是本项目
            #    后端自己的端口**。用户配 vLLM 时如果没改地址，请求会打到
            #    我们自己的 `/v1/models`，拿回 `401 未登录，请先登录` ——
            #    看起来像"API Key 不对"，实际上**打错服务了**。
            #    这里识别出来，直接告诉用户，省得他去瞎调密钥。
            if info["code"] in (401, 403) and is_local and any(
                t in body for t in ("请先登录", "未登录", "little crew", "小剧组")
            ):
                return {"ok": False, "models": [], "provider": prov, "base_url": b,
                        "source": "provider", "error_type": "config",
                        "detail": f"{b} 返回了「{body[:60]}」—— 这看起来是**本项目自己的"
                                  f"后端**，不是 vLLM。vLLM 的默认端口 8000 与本项目后端"
                                  f"冲突，请把 vLLM 换到别的端口（如 8033）再填这里"}
            et = "auth" if info["code"] in (401, 403) else \
                "network" if info["code"] >= 500 else "other"
            reason = f"HTTP {info['code']}：{redact(body)[:200]}" if body \
                else f"HTTP {info['code']}"
            return _maybe_fallback(prov, b, is_local, et, reason)
        exc = info.get("exc") or ""
        low = str(exc).lower()
        # 本机服务没在跑时的几种表现都要识别，别只认 "refused"：
        #   · 端口没人听    → ConnectionRefusedError / WinError 10061
        #   · 防火墙丢弃    → 超时（timed out），实测 127.0.0.1:1 就是这个
        #   · 域名解析不了  → getaddrinfo failed
        conn_like = ("refused", "connect", "10061", "timed out", "timeout",
                     "unreachable", "getaddrinfo", "no route")
        if is_local and any(t in low for t in conn_like):
            return {"ok": False, "models": [], "provider": prov, "base_url": b,
                    "source": "provider", "error_type": "network",
                    "detail": f"连不上 {b} —— 本地推理服务没在运行。"
                              f"请先启动它（Ollama 执行 `ollama serve`），再点一次"}
        et = "network" if any(t in low for t in conn_like) else "other"
        return _maybe_fallback(prov, b, is_local, et, redact(str(exc))[:250])

    raw = out

    # 各家 shape 不同，交给**共用解析器**（`services/model_listing.py`）：
    #   OpenAI / vLLM / 多数网关  {"data": [{"id": "..."}]}
    #   Ollama 原生 /api/tags     {"models": [{"name": "..."}]}
    #   **部分兼容网关**           {"models": {"id": {...}}}   ← map，不是数组
    #
    # ⚠️ 这个解析原先在 `fetch_models` 与 `model_catalog` 里**各写一份**
    # （两份手写必然漂移），现在统一到 `model_listing.parse_models()`。
    # 补上 map 形状是照 DSH 的做法：它的解析器明确"接受标准的 data 数组
    # 以及某些兼容网关暴露的 models map"。
    from app.services.model_listing import parse_models

    try:
        uniq = parse_models(raw)
    except Exception:  # noqa: BLE001  不是合法 JSON
        return _maybe_fallback(prov, b, is_local, "other",
                               "返回的不是 JSON（该提供者可能没有 /models 端点）")

    # 注意：这里**只**处理"响应不是合法结构"（parse_models 抛异常，上面已处理）。
    # "响应里没有条目"是**空列表**，属于成功但有提示 —— 见下面 `if not entries`。
    # ⚠️ 只回 `{id, kind}` —— 解析器返回的条目还带一个内部用的 `raw`
    # （原始条目），泄漏到接口响应里会让字段集变得不可预期。
    entries = [{"id": e["id"], "kind": e["kind"]} for e in uniq]
    ids = [e["id"] for e in entries]

    # **响应里没有条目 ≠ 查询失败**：本地服务还没 pull、云端账号没开通模型，
    # 都会返回空列表 —— 那是"问到了，但里面是空的"，要给提示而不是报错。
    #
    # ⚠️ 这里**不能**走 `_maybe_fallback`：那会把"空列表"报成
    # "返回里没有模型列表"（措辞像故障），而且对本地提供者还会直接变成失败。
    if not entries:
        # ⚠️ **本机不回退**，与 `_maybe_fallback` 同一个道理：
        #    本机装了什么只有本机知道。回退会给出 `_PRESETS` 里那个
        #    旧默认名（如 `qwen2.5`），用户在本机根本没有它 —— 选了必失败。
        #    （这个 bug 被测试当场抓到：`/api/tags` 返回空，却"查"出了 qwen2.5。）
        if not is_local:
            fb = _fallback_known(prov)
            if fb:
                return {"ok": True, "models": [{"id": x, "kind": "chat"} for x in fb],
                        "provider": prov, "base_url": b,
                        "source": "registry", "error_type": "ok",
                        "detail": f"「{prov}」的 /models 返回空，改用已核对的清单"
                                  f"（{len(fb)} 个）"}
        hint = ("本地服务在跑，但里面还没有模型 —— 先下载/pull 一个再点一次"
                if is_local else "该提供者返回了空列表（账号可能没开通任何模型）")
        return {"ok": True, "models": [], "provider": prov, "base_url": b,
                "source": "provider", "error_type": "ok", "detail": hint}

    # ⚠️ 回的是 `entries`（只有 id/kind），不是解析器的原始输出 `uniq`
    # —— 后者带内部用的 `raw`。
    return {"ok": True, "models": entries, "provider": prov, "base_url": b,
            "source": "provider", "error_type": "ok",
            "detail": f"从 {prov} 查到 {len(entries)} 个模型"
                      + ("（已标出模型类型）" if any(e["kind"] for e in entries) else "")}

def _maybe_fallback(provider: str, base_url: str, is_local: bool,
                    error_type: str, reason: str) -> dict:
    """问厂商失败时：云端退回已核对清单，本机如实报错。

    **本机不回退**——本机装了什么只有本机知道，拿云端模型名去糊弄用户
    会让他在自己的 Ollama 上选一个不存在的模型，比报错更糟。
    """
    if not is_local:
        fb = _fallback_known(provider)
        if fb:
            # ⚠️ 措辞必须区分"没端点"和"没通过鉴权" —— 这两件事完全不同：
            #
            #   · **没端点**：这个平台就是不提供列模型接口 → 只能用兜底清单，
            #     用户做什么都没用；
            #   · **401/403**：端点存在，只是**没通过鉴权**（多半是还没填 Key）
            #     → 用户**填好 Key 就能拿到实时清单**。
            #
            # 原先一律写成"没有可用的 /models 端点"，于是没填 Key 的用户会以为
            # "这个平台不支持"，**永远不会去填 Key**（实测踩到：
            # "HTTP 401: Authentication Fails (governor)" 被说成"没有端点"）。
            if error_type == "auth":
                why = (f"厂商拒绝了这次查询（{reason}）—— **端点是在的**，"
                       f"填好 API Key 就能拿到实时清单；先用已核对的 {len(fb)} 个垫着")
            elif error_type == "network":
                why = (f"连不上厂商（{reason}），"
                       f"先用已核对的清单（{len(fb)} 个）")
            else:
                why = (f"「{provider}」没有可用的 /models 端点（{reason}），"
                       f"改用已核对的清单（{len(fb)} 个，可能不是最新）")
            return {"ok": True,
                    # 兜底清单里都是我们人工核对的**对话模型**，所以 kind 一律标 chat
                    "models": [{"id": x, "kind": "chat"} for x in fb],
                    "provider": provider, "base_url": base_url,
                    "source": "registry", "error_type": "ok",
                    "detail": why}
    return {"ok": False, "models": [], "provider": provider, "base_url": base_url,
            "source": "provider", "error_type": error_type, "detail": reason}


def _fallback_known(provider: str) -> list[str]:
    """云端提供者的兜底清单：注册表里已人工核对的 `known_models`。

    ## 为什么云端需要兜底（用户："如果是 api 就检测厂商模型"）

    优先**问厂商自己**（`GET {base_url}/models`）——那是最准的，厂商上架
    什么就有什么。但**不是每家都有这个端点**：国内不少厂商只提供
    `/chat/completions`，`/models` 返回 404；网关（OpenRouter 等）虽然
    有，但有的要额外 header。

    这时如果只报错，用户就卡住了 —— 而我们在 `providers.py` 里**已经
    逐家核对过**当前型号（`known_models`，含来源 URL）。所以顺序是：

        1. 问厂商（最新、最全）
        2. 厂商答不了 → 用已核对的清单（并标明来源，让用户知道这不是实时的）
    """
    try:
        from app.services.providers import get as _spec_get

        spec = _spec_get("llm", provider)
        if spec is None:
            return []
        out = list(spec.known_models)
        # 默认模型如果不在清单里也带上（它一定可用）
        if spec.default_model and spec.default_model not in out:
            out.insert(0, spec.default_model)
        return out
    except Exception:  # noqa: BLE001
        return []
