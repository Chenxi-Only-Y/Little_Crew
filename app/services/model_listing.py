"""把各家的**模型清单响应**解析成统一的 `[{id, kind, raw}]`。

## 为什么要单独一个模块

同一个解析在**两处**需要，而且它们以前各写一份：

  · `llm.fetch_models()`    —— 文字模型（LLM）
  · `model_catalog._ask_provider()` —— 图像 / 视频 / 配音

两份手写必然漂移（本项目已经吃过这个亏好几次）。这里统一成一处。

## 支持哪些响应形状（照 DSH 的做法）

DSH 的 `dsh-llm-pi-ai` 里写着：

    "The parser accepts the standard `data` array and the enriched `models`
     map some compatible gateways expose."

也就是**两种形状都要认**：

    {"data":   [{"id": "gpt-x"}, ...]}          OpenAI / vLLM / 多数网关
    {"models": [{"name": "qwen3:8b"}, ...]}      Ollama 原生 /api/tags
    {"models": {"gpt-x": {...}, "gpt-y": {...}}} 部分兼容网关（**map，不是数组**）

只认数组时遇到第三种会报"返回里没有模型列表"—— 而它其实有，只是形状不同。

## 另外兼容

  · 顶层直接是**数组**（少数端点如此）
  · 条目是**纯字符串**
  · 名称字段叫 `id` / `name` / `model` / `model_name` 都认
    （不同厂商叫法不同，只认 `id` 会漏）
"""

from __future__ import annotations

import json
from typing import Any

#: 条目里可能是"模型名"的键（按优先级）。
_NAME_KEYS = ("id", "name", "model", "model_name", "modelName")


def _name_of(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for k in _NAME_KEYS:
            v = item.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def extract_items(obj: Any) -> list[Any]:
    """从响应里取出"模型条目"列表（统一的这一步）。

    Returns:
        条目列表（元素可能是 dict 或 str）。**取不到时返回空列表** ——
        调用方据此给"没有模型列表"的提示。
    """
    if isinstance(obj, list):
        return obj
    if not isinstance(obj, dict):
        return []

    # ① 标准 data 数组
    data = obj.get("data")
    if isinstance(data, list):
        return data
    # 有些网关把 data 也写成 map（少见，但形如 {"data": {"id": {...}}} 时按值取）
    if isinstance(data, dict):
        return list(data.values())

    # ② models：可能是数组（Ollama 原生），也可能是 map（部分兼容网关）
    models = obj.get("models")
    if isinstance(models, list):
        return models
    if isinstance(models, dict):
        # map 形状：`{"gpt-x": {...}}` —— key 就是模型名。
        # 把 key 作为名字注入，避免条目里没有 id/name 字段时丢名字。
        out: list[Any] = []
        for k, v in models.items():
            if isinstance(v, dict):
                merged = dict(v)
                merged.setdefault("id", k)
                out.append(merged)
            else:
                out.append({"id": k})
        return out
    return []


def parse_models(raw: bytes | str) -> list[dict]:
    """解析响应体 → `[{"id", "kind", "raw"}]`（已去重、保序）。

    Raises:
        ValueError: 响应不是合法 JSON（调用方据此区分"没有端点"与"端点存在
            但返回怪东西"）。**不在这里吞掉** —— 两种情况给用户的提示不同。
    """
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    obj = json.loads(text)

    entries: list[dict] = []
    seen: set[str] = set()
    for it in extract_items(obj):
        mid = _name_of(it)
        if not mid or mid in seen:
            continue
        seen.add(mid)
        entries.append({
            "id": mid,
            "kind": _classify(it),
            "raw": it if isinstance(it, dict) else {},
        })
    return entries


def _classify(item: Any) -> str:
    """判断是**对话**还是**嵌入**（界面用来标注；选错会直接报错）。

    判据优先级（Ollama 的字段最权威）：
      1. `capabilities` 里有 completion/chat/tools/thinking → chat
      2. `capabilities` 里有 embedding/embed → embed
      3. `details.family` / `details.families` 命中 bert/embed/rerank/clip/bge → embed
      4. 认不出 → `""`（**不瞎标**，界面就不显示类型标签）
    """
    if not isinstance(item, dict):
        return ""

    caps = item.get("capabilities")
    if isinstance(caps, list):
        c = {str(x).lower() for x in caps}
        if "completion" in c or "chat" in c or "tools" in c or "thinking" in c:
            return "chat"
        if "embedding" in c or "embed" in c:
            return "embed"

    details = item.get("details")
    fam = ""
    if isinstance(details, dict):
        fam = str(details.get("family") or "").lower()
        fams = details.get("families")
        if isinstance(fams, list):
            fam = " ".join([fam] + [str(x).lower() for x in fams])
    # 也看 type / object 字段（部分网关用 `"type": "embedding"`）
    for k in ("type", "object"):
        v = item.get(k)
        if isinstance(v, str):
            fam += " " + v.lower()
    if any(t in fam for t in ("bert", "embed", "rerank", "clip", "bge")):
        return "embed"
    return ""
