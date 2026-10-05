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
