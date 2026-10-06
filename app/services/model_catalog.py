"""非 LLM 能力的**模型清单**（图像 / 视频 / 配音 / LoRA）。

## 为什么需要单独一个模块

`/api/providers/models` 原先只支持 `llm`，遇到图像/视频直接返回 400，
理由是代码注释里写的：

    "图像/视频提供者没有统一的 /models 端点"

这话**是对的** —— 但结论错了。用户的问题是"**不知道能填哪些模型**"，
而答案有两个来源，应该**都用上并合并**：

  1. **问厂商**（`GET {base_url}/models`）：最准，厂商上新模型就自动出现。
     但它**不一定列出图像/视频模型** —— 很多平台这个端点只返回对话模型。
  2. **注册表里人工核对的 `known_models`**：一定包含图像/视频模型，
     但它是人工维护的，厂商上新模型**不会自动出现**。

只用一个都不够：只用 (1) 会得到空的或不相干的清单；只用 (2) 永远滞后。
所以这里**合并**，并给每个模型标出来源，让界面能诚实地说
"这是厂商返回的"还是"这是项目里已核对的"。

## 安全：地址与密钥**只从已保存的设置里解析**

与 `llm.fetch_models` 同一条原则，不接受调用方传地址 —— 这样本机地址
（本地推理服务）可以放行，而 SSRF 面为零（用户无法让服务端去请求
他指定的任意地址）。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger("little_crew")

#: 支持"问厂商"的能力（这些提供者的端点都是 OpenAI 兼容的 `/models`）。
#: 不在这里的能力只返回注册表清单。
_QUERYABLE: frozenset[str] = frozenset({"image_gen", "image_edit", "video_gen", "tts"})

#: 探测超时（秒）。短一点：它在"点按钮"的交互路径上。
_TIMEOUT_S = 10.0


def _rs():
    """延迟取运行时设置（避免模块导入期的循环依赖）。"""
    from app.services.runtime_settings import get as rs_get

    return rs_get


def _resolve(capability: str, role: str = "") -> tuple[str, str, str]:
    """解析该能力**当前会用的提供者**及其 `(provider, base_url, api_key)`。

    与各服务自己的解析保持一致，**不另立一套** —— 否则会出现
    "设置页问的是 A 家的模型，实际跑的是 B 家"。
    """
    from app.services import providers as prov

    capability = (capability or "").strip().lower()
    if capability in ("image_gen", "image_edit"):
        cap_for_resolve = "image_gen"
    elif capability in ("video_gen", "tts", "lora_train"):
        cap_for_resolve = capability
    else:
        return "", "", ""

    key, _why = prov.resolve_provider(cap_for_resolve, role)
    if not key:
        return "", "", ""
    key = key.lower()
    spec = prov.get(cap_for_resolve, key)
    if spec is None:
        return key, "", ""

    rs = _rs()
    # 端点：视频用 `endpoint_setting`（Seedance 的键是 `ark_base_url`，
    # 与 provider key 不同名 —— 硬拼会拿到不存在的键）。
    base = ""
    if cap_for_resolve == "video_gen":
        try:
            from app.services.video_adapters import get_video_provider

            vp = get_video_provider(key)
        except Exception:  # noqa: BLE001
            vp = None
        if vp is not None:
            base = str(rs(vp.endpoint_setting or "") or "").strip() or vp.base_url or ""
            api_key = str(rs(vp.api_key_setting or "") or "").strip()
            return key, base, api_key
    if not base:
        # 图像 / 配音：设置键约定为 `{capability 前缀}_base_url`
        for cand in (f"{capability}_base_url", "image_base_url", f"{key}_base_url"):
            base = str(rs(cand) or "").strip()
            if base:
                break
    api_key = ""
    for cand in (f"{key}_api_key", "image_api_key", "dashscope_api_key", "minimax_api_key",
                 "ark_api_key", "replicate_api_token"):
        api_key = str(rs(cand) or "").strip()
        if api_key:
            break
    return key, base, api_key


def _registry_models(capability: str, provider_key: str) -> list[str]:
    """该提供者在注册表里**已人工核对**的模型清单。"""
    from app.services import providers as prov

    cap = "image_gen" if capability in ("image_gen", "image_edit") else capability
    spec = prov.get(cap, provider_key)
    return list(spec.known_models) if spec else []


def _ask_provider(base_url: str, api_key: str, *, is_local: bool) -> tuple[bool, list[str], str]:
    """问厂商要模型清单。

    Returns:
        (ok, models, detail)。`ok=False` 时 `detail` 说明原因（供界面展示）。
    """
    from app.services.url_guard import assert_public_url

    b = base_url.rstrip("/")
    host_is_private = ("localhost" in b) or ("127.0.0.1" in b) or ("[::1]" in b)
    # 本地提供者 / 回环地址允许内网；真正的云端才走 SSRF 防护
    if not (is_local or host_is_private):
        try:
            assert_public_url(b, context="模型清单地址")
        except ValueError as exc:
            return False, [], str(exc)

    url = f"{b}/models"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as r:
            body = r.read(400_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, [], f"厂商拒绝了这次查询（HTTP {exc.code}）—— 请先填好 API Key"
        return False, [], f"厂商返回 HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, [], f"连不上厂商（{type(exc).__name__}）"

    try:
        data = json.loads(body)
    except ValueError:
        return False, [], "厂商返回的不是 JSON"
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return False, [], "厂商返回的结构里没有模型数组"
    ids: list[str] = []
    for it in rows:
        if isinstance(it, str):
            ids.append(it)
        elif isinstance(it, dict):
            mid = it.get("id") or it.get("model") or it.get("name")
            if mid:
                ids.append(str(mid))
    return True, ids, f"厂商返回 {len(ids)} 个模型"


def list_models(capability: str, role: str = "") -> dict:
    """该能力当前提供者的**可用模型清单**（厂商 + 注册表，合并）。

    Returns:
        {"ok": bool, "models": [{"id","source"}], "detail": str,
         "provider": str, "base_url": str, "source": "provider"|"registry"|"merged"}
    """
    capability = (capability or "").strip().lower()
    provider_key, base_url, api_key = _resolve(capability, role)
    if not provider_key:
        return {"ok": False, "models": [], "provider": "", "base_url": "",
                "source": "none", "error_type": "config",
                "detail": "还没选提供者，无法查询模型清单。请先在上面选好「来源」。"}

    known = _registry_models(capability, provider_key)
    from app.services import providers as prov

    spec = prov.get("image_gen" if capability in ("image_gen", "image_edit") else capability,
                    provider_key)
    is_local = bool(spec and spec.category in ("local", "free"))

    live: list[str] = []
    live_detail = ""
    if capability in _QUERYABLE and base_url:
        ok, live, live_detail = _ask_provider(base_url, api_key, is_local=is_local)
    elif capability in _QUERYABLE and not base_url:
        live_detail = "该提供者没有可用的 API 地址，只能给出项目里已核对的清单"
    else:
        live_detail = "该能力不从厂商查询（没有统一端点），给出项目里已核对的清单"

    # 合并：厂商的在前（更新鲜），注册表的补在后面（去重）
    models: list[dict] = []
    seen: set[str] = set()
    for m in live:
        if m and m not in seen:
            seen.add(m)
            models.append({"id": m, "source": "provider"})
    for m in known:
        if m and m not in seen:
            seen.add(m)
            models.append({"id": m, "source": "registry"})

    if live and known:
        source = "merged"
        detail = f"{live_detail}；另附项目里已核对的 {len(known)} 个（厂商那个端点不一定列出图像/视频模型）"
    elif live:
        source = "provider"
        detail = live_detail
    else:
        source = "registry"
        detail = f"{live_detail}（共 {len(known)} 个，来自项目里人工核对的清单）"

    return {"ok": bool(models), "models": models, "provider": provider_key,
            "base_url": base_url, "source": source, "detail": detail}
