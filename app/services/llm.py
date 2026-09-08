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
    "deepseek": ("https://api.deepseek.com", "deepseek-v4-flash"),
    "zhipu": ("https://open.bigmodel.cn/api/paas/v4", "glm-4"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "qwen": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),        # 阿里通义
    "moonshot": ("https://api.moonshot.cn/v1", "moonshot-v1-8k"),                       # 月之暗面 Kimi
    "claude": ("https://api.anthropic.com/v1", "claude-3-5-sonnet-latest"),             # Anthropic
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-1.5-pro"),  # Google
    "doubao": ("https://ark.cn-beijing.volces.com/api/v3", "doubao-pro-32k"),           # 字节豆包(火山方舟)
    "ernie": ("https://qianfan.baidubce.com/v2", "ernie-4.0-8k"),                       # 百度文心(千帆)
    "ollama": ("http://localhost:11434/v1", "qwen2.5"),                                 # 本地部署
    "vllm": ("http://localhost:8000/v1", "Qwen/Qwen2.5-7B-Instruct"),                   # 本地 vLLM
}
_LOCAL_PROVIDERS = {"ollama", "vllm"}


def _resolve(role: str = "") -> tuple:
    """解析出 (provider, base_url, model, api_key)。role 非空时优先用该角色覆盖。"""
    s = f"_{role}" if role else ""
    provider = (rs_get(f"llm_provider{s}") or rs_get("llm_provider") or settings.llm_provider or "deepseek").lower()
    preset = _PRESETS.get(provider, _PRESETS["deepseek"])
    base_url = rs_get(f"llm_base_url{s}") or rs_get("llm_base_url") or settings.llm_base_url or preset[0]
    model = rs_get(f"llm_model{s}") or rs_get("llm_model") or settings.llm_model or preset[1]
    api_key = rs_get(f"llm_api_key{s}") or rs_get("llm_api_key") or settings.llm_api_key or settings.deepseek_api_key
    return provider, base_url, model, api_key


class LLMService:
    def __init__(self) -> None:
        self.mock = settings.mock_llm
        self._clients: dict = {}  # (provider,base,model,key) -> ChatOpenAI

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
        if self._client(role) is None:
            provider, _, _, _ = _resolve(role)
            raise RuntimeError(
                f"LLM 客户端未初始化（provider={provider}, role={role or 'global'}）：请检查密钥或在「设置」页填写。"
            )


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
