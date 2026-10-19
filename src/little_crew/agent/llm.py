"""LLM 调用层 —— 把 ``models`` 注册表与 HTTP 通道接起来。

这一层此前**不存在**，而它的缺失正是"18 个职位真实 LLM 实现 = 0"的根因
--------------------------------------------------------------------
`models/registry.py` 里 6 个模型条目都声明了 ``api_key_env``（如
``DEEPSEEK_API_KEY``），而**全项目没有任何地方读它**：

    registry.py:116    api_key_env="DEEPSEEK_API_KEY",     ← 声明
    （无任何使用点）                                        ← 从没被读取

`runtime/channels/http.py` 能发请求、能渲染模板、能取响应路径 ——
但**没人告诉它用哪个模型、密钥在哪、提示词从哪来**。
于是"接真模型"这件事在架构上一直是断的：**协议齐备，只缺中间那一段**。

本模块就是那一段。

设计要点
--------
**零第三方依赖**：标准库 ``urllib``，与 ``HttpChannel`` 同源。

**只重试传输层失败，不重试语义失败**
重试"提示词有问题"没有意义 —— 重试一万次也是同一份账单。
但重试**网络抖动**是必须的，否则一次抖动就会让一整轮判定作废
（实测：C 组 5 次里 2 次整轮作废、分数 0.0，根因就是调用失败）。

区分靠 `LLMError.retryable`：它只对 **429 / 5xx / 网络异常**为真，
对 **4xx 请求错误**为假。`_post_with_retry` 只对前者退避重试。

**失败必须可分辨**：`LLMError` 带 ``retryable`` 与 ``kind``，
让上层能区分"网络抖了一下"（可重试）与"提示词/额度问题"（重试无意义）。
**重试次数进 `call_log`** —— 那是可靠性指标，藏起来就等于宣称"我们从不失败"。

**结构化输出要宽容但可核**：模型会在 JSON 外面套 ```json 围栏、
加解释性前言。`extract_json` 容忍这些，但**取不到就报错**，
不返回空 dict 装作成功 —— 那会让上层把"没产出"当成"产出了空的"。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = ["LLMError", "LLMReply", "LlmClient", "extract_json",
           "resolve_api_key", "model_spec_for", "DEFAULT_MODEL_ID"]


#: 本机默认使用的模型条目（`models/registry.py` 的 model_id）。
#:
#: 选它而不是另建一条：注册表已经声明了它的端点、路径、模板、响应路径、
#: 上下文长度与结构化输出评分 —— **重复声明会产生第二份真相**，
#: 而两份真相迟早会不一致。
DEFAULT_MODEL_ID = "deepseek-v4"



#: **同一模型的调用串行化**（2026-10-19 实测）。
#:
#: ## 为什么需要
#:
#: 编排器是**并行双审** —— `dramaturge` 与 `story_editor` 同时发起。
#: 接入 GLM 后实测到：
#:
#: | | 结果 |
#: |---|---|
#: | 并发 2 个 | 253 字 / **0 字**（各 77~79 秒，明显在排队） |
#: | 串行 2 个 | 231 字 / 209 字（39.6s / 15.0s）**都正常** |
#:
#: 也就是说该端点**并发受限**，第二个请求会返回**空内容** ——
#: 而上层只会看到 `[empty-output]`，很难归因到"并发"。
#:
#: ## 为什么按 model_id 分锁，而不是一把全局锁
#:
#: 全局锁会把**所有模型**串起来（DeepSeek 与 MiMo 本来能并行，没必要等）。
#: 按模型分锁只串行"同一个端点的调用"，不同模型之间仍并行。
#:
#: 代价：同一模型的并发变成串行 → 双审的总耗时约等于两者之和。
#: **这是可接受的**：正确性优先于墙钟；且审查本身就是瓶颈之外的环节。
_MODEL_LOCKS: dict[str, Any] = {}
_MODEL_LOCKS_GUARD = threading.Lock()


def _lock_for(model_id: str) -> Any:
    with _MODEL_LOCKS_GUARD:
        got = _MODEL_LOCKS.get(model_id)
        if got is None:
            got = threading.RLock()
            _MODEL_LOCKS[model_id] = got
        return got


class LLMError(RuntimeError):
    """LLM 调用失败。**带可分辨的原因**，不是一句"失败了"。"""

    def __init__(self, kind: str, message: str, *,
                 retryable: bool = False,
                 detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.detail = dict(detail or {})

    def __str__(self) -> str:                                  # pragma: no cover
        return f"[{self.kind}] {super().__str__()}"


@dataclass
class LLMReply:
    """一次成功调用的结果。"""

    text: str
    model: str = ""
    #: 服务端实际服务的模型。
    #:
    #: ⚠️ 与 `model` **可能不同** —— 厂商会用别名路由
    #: （实测：请求 `deepseek-chat`，实际是 `deepseek-flash`）。
    #: 记下来是因为**跨模型族盲读需要知道真实族属**：
    #: 若审核方与生成方请求了不同名字却被路由到同一个模型，
    #: "不同族"就是假的，判定应当作废。
    served_model: str = ""
    usage: Mapping[str, Any] = field(default_factory=dict)
    elapsed_s: float = 0.0
    raw: Mapping[str, Any] = field(default_factory=dict)


# ==========================================================================
# 模型与密钥
# ==========================================================================

def model_spec_for(model_id: str = DEFAULT_MODEL_ID) -> Any:
    """从注册表取模型条目。取不到时抛 `LLMError`（**不静默退回默认**）。"""
    from little_crew.models.registry import build_default_registry

    reg = build_default_registry()
    spec = reg.get(model_id)
    if spec is None:
        raise LLMError(
            "model-not-found",
            f"注册表里没有模型 {model_id!r}（可选："
            f"{', '.join(sorted(m.model_id for m in reg.all()))}）",
        )
    return spec


def resolve_api_key(spec: Any) -> str:
    """按模型条目声明的 ``api_key_env`` 取密钥。

    顺序：**进程环境变量 > 项目外秘密文件 > `config/.env`**
    （与 `config.env_value` 一致 —— 复用同一套优先级，
    避免"这里读得到、那里读不到"这种分裂）。
    """
    from little_crew.config import env_value

    if not spec.backends:
        return ""
    env_name = str(getattr(spec.backends[0], "api_key_env", "") or "")
    if not env_name:
        return ""
    return env_value(env_name)


# ==========================================================================
# 结构化输出提取
# ==========================================================================

#: ```json ... ``` 围栏
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str, *, what: str = "输出") -> dict[str, Any]:
    """从模型回复里取出 JSON 对象。

    容忍三种常见包装：```json 围栏、解释性前言、尾随说明。
    ⚠️ **取不到就抛 `LLMError`** —— 绝不返回空 dict 装作成功。
    把"模型没按格式产出"当成"产出了一个空对象"，
    是这类集成里最会骗人的失败模式。
    """
    s = (text or "").strip()
    if not s:
        raise LLMError("empty-output", f"模型{what}为空")

    # ① 直接就是 JSON
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # ② 代码围栏
    for m in _FENCE.finditer(s):
        try:
            obj = json.loads(m.group(1).strip())
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue

    # ③ 第一个 { 到最后一个 } —— 处理"前言 + JSON + 后记"
    i, j = s.find("{"), s.rfind("}")
    if 0 <= i < j:
        try:
            obj = json.loads(s[i:j + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    raise LLMError(
        "bad-json",
        f"模型{what}里找不到合法 JSON 对象（前 200 字：{s[:200]!r}）",
        detail={"head": s[:400]},
    )


# ==========================================================================
# 客户端
# ==========================================================================

class LlmClient:
    """按注册表条目调用 LLM。**同步、无重试、零依赖**。

    用法::

        c = LlmClient()
        reply = c.chat("用一句话介绍自己", system="你是编剧")
        data  = c.chat_json("产出分镜，JSON 格式…")

    `call_log` 记录每次调用（模型、耗时、token）—— 供证据链与成本核算用。
    """

    def __init__(self, model_id: str = DEFAULT_MODEL_ID, *,
                 api_key: str | None = None, timeout_s: int = 120,
                 temperature: float = 0.7,
                 max_retries: int = 2, backoff_s: float = 0.8) -> None:
        self.spec = model_spec_for(model_id)
        self.model_id = model_id
        self.timeout_s = timeout_s
        self.temperature = temperature
        #: **仅对 `retryable=True` 的失败**重试这么多次（0 = 不重试）。
        #:
        #: ## 为什么必须重试（2026-10-17 实测）
        #:
        #: 条件对比实验里，C 组（都市悬疑）**5 次中有 2 次整轮判定作废**、
        #: 分数 0.0。查下去不是"剧本写得差"，而是**评审调用失败** ——
        #: 而 `review_artifact` 遇到任一审核者失败就中止整轮：
        #:
        #:     if not out.ok:
        #:         return (), f"审核者 {rid} 执行失败：{out.error}"
        #:
        #: **一次网络抖动 = 一整轮判定作废 = 浪费掉这一轮的全部 token。**
        #:
        #: ## 这与"本层不重试"的设计说明不冲突
        #:
        #: 那句话的原意是：**重试"语义"失败没有意义**（提示词有问题，
        #: 重试一万次也一样）。而 `retryable` 字段恰好编码的就是这个区分 ——
        #: 它只对 **429 / 5xx / 网络异常**为真，对 **4xx 请求错误**为假。
        #:
        #: 所以现在：**传输层失败重试，语义失败不重试。**
        #: 那正是当初想做而没做的那一刀。
        self.max_retries = max(0, int(max_retries))
        self.backoff_s = max(0.0, float(backoff_s))
        #: 显式传入优先（测试注入用）；否则按 `api_key_env` 解析
        self.api_key = (api_key if api_key is not None
                        else resolve_api_key(self.spec))
        self.call_log: list[dict[str, Any]] = []
        self.calls = 0
        #: 累计重试次数（进证据链用：能看出"这次跑得不顺"）
        self.retries = 0

    # ---- 能力自述 ------------------------------------------------------

    @property
    def configured(self) -> bool:
        """密钥是否就位。**让调用方能区分"没配"与"配错了"。**"""
        return bool(self.api_key)

    def describe(self) -> dict[str, Any]:
        key = self.api_key
        return {
            "modelId": self.model_id,
            "displayName": self.spec.display_name,
            "endpoint": (self.spec.backends[0].endpoint
                         if self.spec.backends else ""),
            "apiKeyEnv": (self.spec.backends[0].api_key_env
                          if self.spec.backends else ""),
            "configured": self.configured,
            "apiKeyMasked": (f"{key[:6]}…{key[-4:]}（{len(key)} 字符）"
                             if key else "（未配置）"),
        }

    # ---- 调用 ----------------------------------------------------------

    def chat(self, prompt: str, *, system: str = "",
             temperature: float | None = None,
             timeout_s: int | None = None,
             json_mode: bool = False) -> LLMReply:
        """发一次 chat completion。

        `json_mode=True` 时启用厂商的严格 JSON 模式（若该模型声明了
        `body_template_json`）。**默认关闭** —— 因为某些厂商在开启它时
        强制要求提示词里出现 "json" 字样，纯文本调用会直接 400。

        ⚠️ **失败抛 `LLMError`，不返回空串。** 调用方必须显式处理 ——
        否则"模型没答"会静默变成"模型答了空"。
        """
        if not self.configured:
            envn = (self.spec.backends[0].api_key_env
                    if self.spec.backends else "?")
            raise LLMError(
                "no-api-key",
                f"模型 {self.model_id!r} 需要密钥 {envn}，但三处都取不到"
                f"（进程环境 / 项目外秘密文件 / config/.env）",
            )

        backend = self.spec.backends[0]
        # 复用模板渲染 —— 它已处理引号感知、转义、容器序列化，
        # 以及**未替换占位符必须报错**（那个检查救过一次静默失败）。
        #
        # 从 `models.template` 导入而非 `runtime.channels.http`：
        # 后者只是**再导出**，而 `agent`(层 6) 不能 import `runtime`(层 8)。
        from little_crew.models.template import render_body_template

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        values = {
            "messages": messages,
            "temperature": (self.temperature if temperature is None
                            else temperature),
            "model_name": self.model_id,
        }

        template = ""
        if json_mode:
            template = str(getattr(backend, "body_template_json", "") or "")
        if not template:
            template = backend.body_template
        if json_mode and not getattr(backend, "body_template_json", ""):
            # 如实说明：不静默降级 —— 调用方以为拿到严格 JSON，
            # 实际只是"提示词里求了 JSON"，两者的可靠性差一个量级。
            raise LLMError(
                "json-mode-unsupported",
                f"模型 {self.model_id!r} 未声明 body_template_json，"
                f"无法启用严格 JSON 模式（不要静默降级）",
            )

        try:
            body = render_body_template(template, values)
        except Exception as e:                                  # noqa: BLE001
            raise LLMError("template", f"请求体模板渲染失败: {e}") from e

        url = backend.endpoint.rstrip("/") + backend.path
        reply = self._post_with_retry(url, body, timeout_s or self.timeout_s)
        self.calls += 1
        return reply

    def _post_with_retry(self, url: str, body: Mapping[str, Any],
                         timeout_s: int) -> "LLMReply":
        """**只重试 `retryable=True` 的失败**（429 / 5xx / 网络异常）。

        语义失败（4xx、JSON 结构不对、没有 key）**一次都不重试** ——
        重试它们只是把同一份账单再交一遍。

        退避是 `backoff_s × 2**第几次` —— 第二次失败等得更久一点。
        不做无限重试：`max_retries` 默认 2，总共最多 3 次尝试。
        """
        attempt = 0
        while True:
            try:
                return self._post(url, body, timeout_s)
            except LLMError as e:
                if not e.retryable or attempt >= self.max_retries:
                    raise
                attempt += 1
                self.retries += 1
                delay = self.backoff_s * (2 ** (attempt - 1))
                #: 记进账本 —— 证据链能看出"这次跑得不顺"。
                #: **不记成成功**：重试次数是**可靠性指标**，藏起来就等于
                #: 宣称"我们的调用从不失败"。
                self.call_log.append({
                    "modelId": self.model_id, "servedModel": "",
                    "elapsedS": 0.0, "chars": 0,
                    "retry": attempt, "retryAfterS": round(delay, 2),
                    "retryKind": e.kind, "retryReason": str(e)[:120]})
                time.sleep(delay)

    def chat_json(self, prompt: str, *, system: str = "",
                  temperature: float | None = None,
                  what: str = "输出") -> dict[str, Any]:
        """`chat` + `extract_json`。**启用严格 JSON 模式**。

        ⚠️ 严格模式要求提示词里出现 "json" 字样（部分厂商的硬性要求）。
        本方法会**自动补一句**，免得每个调用方都要记得 ——
        但**模板里仍要明确说明要什么 JSON 结构**，
        否则模型会返回一个合法但无用的 `{}`。
        """
        hint = "" if "json" in prompt.lower() else "\n\n请以 JSON 格式输出。"
        # ⚠️ **同一模型的调用串行化**（见 `_MODEL_LOCKS` 的说明）。
        # 并发受限的端点会在并发时返回空内容，而上层只会看到 `[empty-output]`。
        with _lock_for(self.model_id):
            reply = self.chat(prompt + hint, system=system,
                              temperature=temperature, json_mode=True)
        return extract_json(reply.text, what=what)

    # ---- 内部 ----------------------------------------------------------

    def _post(self, url: str, body: Mapping[str, Any],
              timeout_s: int) -> LLMReply:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST")
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                payload = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raw = ""
            try:
                raw = e.read().decode("utf-8", errors="replace")
            except Exception:                                   # noqa: BLE001
                pass
            # 429/5xx 是服务端侧，可重试；4xx（除 429）是请求本身的问题
            raise LLMError(
                "http",
                f"HTTP {e.code} {e.reason} —— {_brief(raw)}",
                retryable=(e.code == 429 or 500 <= e.code < 600),
                detail={"status": e.code, "url": url, "body": raw[:600]},
            ) from e
        except Exception as e:                                  # noqa: BLE001
            low = str(e).lower()
            raise LLMError(
                "timeout" if "timed out" in low else "network",
                f"{type(e).__name__}: {e}",
                retryable=True, detail={"url": url},
            ) from e
        elapsed = time.perf_counter() - t0

        try:
            obj = json.loads(payload)
        except json.JSONDecodeError as e:
            raise LLMError("bad-response", f"响应不是合法 JSON: {e}",
                           detail={"body": payload[:400]}) from e

        # 取内容 —— 用 `extract_path`，路径来自注册表（同层，不跨层）
        from little_crew.models.template import extract_path

        text = extract_path(obj, self.spec.backends[0].response_path)
        if text is None:
            # 有些服务把错误放在 200 响应里（配额、内容过滤）
            err = extract_path(obj, "error.message") or extract_path(obj, "error")
            raise LLMError(
                "no-content",
                f"响应里取不到 {self.spec.backends[0].response_path!r}"
                + (f"（服务端消息：{err}）" if err else ""),
                detail={"raw": str(obj)[:400]},
            )

        entry = {
            "modelId": self.model_id,
            "servedModel": str(obj.get("model", "")),
            "elapsedS": round(elapsed, 2),
            "usage": obj.get("usage") or {},
            "chars": len(str(text)),
        }
        self.call_log.append(entry)
        return LLMReply(
            text=str(text),
            model=self.model_id,
            served_model=str(obj.get("model", "")),
            usage=obj.get("usage") or {},
            elapsed_s=elapsed,
            raw=obj if isinstance(obj, dict) else {},
        )


def _brief(raw: str, limit: int = 160) -> str:
    """从错误响应里抠出可读的一句话（厂商格式各不相同）。"""
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return (raw or "")[:limit]
    if isinstance(obj, dict):
        err = obj.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)[:limit]
        if err:
            return str(err)[:limit]
        if obj.get("message"):
            return str(obj["message"])[:limit]
    return str(obj)[:limit]
