"""第三方供应商 · **自定义模型 API** 模式（2026-11-13）。

## 为什么需要

内置模型目录写在 `models/registry.py` 里，**加一家供应商就要改代码**。
而现实是：用户手上可能已经买了某家的图像/语音/视频 API，只想把它接进来用。
所以提供一份**配置驱动的**入口：

```
config/providers.custom.json   （入库；只写"变量名"，不写 key 值）
        ↓  load_custom_providers()
   ModelSpec 列表  →  追加进 ModelRegistry
        ↓
   路由 / 通道 / 授权门禁 照常工作（与内置模型无差别）
```

## 四条硬规则（都是可测的）

1 **key 只存变量名**（`apiKeyEnv`）—— 值留在 `config/.env`，永不入库。
2 **冲突不覆盖**：自定义 `id` 撞上内置模型 → **跳过并告警**，绝不静默覆盖内置定义。
3 **未核实授权默认不放行**：缺 `license` 或 `verified: false` → `NEEDS_REVIEW`；
   要用必须显式 `LITTLE_CREW_ALLOW_UNVERIFIED=1`（接受风险，理由会写进裁决）。
4 **`allowedAgents` 是用途限定**：为空 = 所有职位可用；非空 = 只有列出的职位可用。
   主链路渲染（`runtime`）请求一个只给 `character_designer` 用的后端 → 拒绝。

## 不做什么

不在本模块发网络请求（`test` 只做"配置完整性 + key 是否存在 + endpoint 可达"）。
不猜字段：缺必填项 → 该条**跳过并报错原因**，不静默补默认值（猜出来的默认值最危险）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

#: 配置文件默认位置（相对仓库根）
DEFAULT_PATH = Path("config") / "providers.custom.json"

#: 可用环境变量改路径
ENV_PATH = "LITTLE_CREW_PROVIDERS_FILE"

#: 显式接受"未核实授权"的环境变量
ENV_ALLOW_UNVERIFIED = "LITTLE_CREW_ALLOW_UNVERIFIED"

#: 任务字符串 -> TaskKind 名称（只接受这些，写错就报错，不猜）
TASK_ALIASES: dict[str, str] = {
    "text": "LLM_TEXT", "text.complete": "LLM_TEXT",
    "text.structured": "LLM_STRUCTURED", "text.long_context": "LLM_LONG",
    "vlm.judge": "VLM_JUDGE",
    "image.t2i": "IMAGE_T2I", "image.edit": "IMAGE_EDIT",
    "image.upscale": "IMAGE_UPSCALE",
    "video.i2v": "VIDEO_I2V", "video.t2v": "VIDEO_T2V",
    "video.r2v": "VIDEO_R2V",
    "audio.tts": "AUDIO_TTS", "audio.music": "AUDIO_MUSIC",
}

#: 必填字段（缺一即跳过该条）
REQUIRED = ("id", "task", "endpoint", "path", "bodyTemplate", "responsePath")


def _warn(msg: str) -> None:
    print(f"  ⚠️ 自定义供应商：{msg}")


def resolve_path(path: str | Path | None = None) -> Path:
    """配置文件路径：显式参数 > 环境变量 > 默认 `config/providers.custom.json`。"""
    if path:
        return Path(path)
    env = str(os.environ.get(ENV_PATH) or "").strip()
    return Path(env) if env else DEFAULT_PATH


def allow_unverified() -> bool:
    """是否显式接受"未核实授权"（默认否）。"""
    try:
        from little_crew.config import allow_unverified_risk

        return allow_unverified_risk()
    except Exception:                                         # noqa: BLE001
        return str(os.environ.get(ENV_ALLOW_UNVERIFIED) or "").strip() in (
            "1", "true", "yes")


def _task_kind(task: str) -> Any:
    """把配置里的任务名解析成 `TaskKind`；未知 -> 抛错（不猜）。"""
    from little_crew.models.spec import TaskKind

    raw = str(task or "").strip()
    name = TASK_ALIASES.get(raw)
    if name is None:
        # 也接受正枚举名（如 "IMAGE_T2I"）
        name = raw.upper().replace(".", "_")
    try:
        return TaskKind[name]
    except KeyError as exc:
        raise ValueError(
            f"未知任务 {raw!r}；可用：{sorted(TASK_ALIASES)} or 枚举名 "
            f"{[t.name for t in TaskKind]}"
        ) from exc


def _backend_from(item: Mapping[str, Any]) -> Any:
    """一条配置 -> `Backend`（含用途限定）。"""
    from little_crew.models.spec import Backend, BackendKind

    kind_raw = str(item.get("kind") or "http").strip().lower()
    kind = {"http": BackendKind.HTTP, "comfyui": BackendKind.COMFYUI,
            "process": BackendKind.PROCESS}.get(kind_raw)
    if kind is None:
        raise ValueError(f"未知 kind {kind_raw!r}（http / comfyui / process）")

    allowed = item.get("allowedAgents") or []
    if not isinstance(allowed, (list, tuple)):
        raise ValueError("allowedAgents 必须是字符串数组")
    return Backend(
        kind=kind,
        endpoint=str(item.get("endpoint") or ""),
        api_key_env=str(item.get("apiKeyEnv") or ""),
        path=str(item.get("path") or ""),
        method=str(item.get("method") or "POST"),
        body_template=str(item.get("bodyTemplate") or ""),
        response_path=str(item.get("responsePath") or ""),
        async_task=bool(item.get("asyncTask", False)),
        status_path=str(item.get("statusPath") or ""),
        artifact_kind=str(item.get("artifactKind") or ""),
        is_local_by_default=bool(item.get("isLocal", False)),
        notes=str(item.get("notes") or ""),
        allowed_agents=tuple(str(a) for a in allowed),
    )


def _provider_base(item: Mapping[str, Any]) -> dict[str, Any]:
    """解析一个供应商条目（v2：vendor/protocol/models；v1：单模型+模板）。

    返回统一结构：`{id, displayName, baseUrl, protocol, keyEnv, allowedAgents,
    license, notes, models:[{id, task, overrides}]}`；不合法 -> 抛 ValueError。
    """
    from little_crew.models.providers_builtin import protocol as _proto, vendor as _vendor

    pid = str(item.get("id") or "").strip()
    if not pid:
        raise ValueError("缺字段 id")
    v = _vendor(str(item.get("vendor") or ""))
    base = {
        "id": pid,
        "displayName": str(item.get("displayName") or (v.display_name if v else pid)),
        # `endpoint` 是 v1 的写法，`baseUrl` 是 v2（对齐界面）；两个都认
        "baseUrl": str(item.get("baseUrl") or item.get("endpoint")
                       or (v.base_url if v else "")).rstrip("/"),
        "protocol": str(item.get("protocol") or (v.protocol if v else "")),
        "keyEnv": str(item.get("apiKeyEnv") or (v.key_env if v else "")),
        "allowedAgents": tuple(str(a) for a in (item.get("allowedAgents") or ())),
        "insecureTls": bool(item.get("insecureTls")),
        "userAgent": str(item.get("userAgent") or ""),
        "responseB64Path": str(item.get("responseB64Path") or ""),
        "license": dict(item.get("license") or {}),
        "notes": str(item.get("notes") or (v.notes if v else "")),
        "models": [],
    }
    if not base["baseUrl"]:
        raise ValueError("缺字段 baseUrl/endpoint（且 vendor 未知、无默认基址）")

    models = item.get("models")
    if isinstance(models, list) and models:
        for m in models:
            if not isinstance(m, Mapping) or not str(m.get("id") or "").strip():
                raise ValueError("models[] 里每项都要有 id")
            base["models"].append({
                "id": str(m["id"]).strip(),
                "task": str(m.get("task") or ""),
                "overrides": dict(m),
            })
        return base

    # v1 / 简写：单模型，task 直接写在供应商上
    if not str(item.get("task") or "").strip():
        raise ValueError("缺字段：没有 models[]，也没有 task")
    base["models"].append({
        "id": pid,
        "task": str(item["task"]),
        "overrides": dict(item),
    })
    return base


def _model_spec(provider: Mapping[str, Any], model: Mapping[str, Any]) -> Any:
    """把 (供应商, 模型) 组合成一个 `ModelSpec`。"""
    from little_crew.models.providers_builtin import protocol as _proto
    from little_crew.models.spec import Backend, BackendKind, Capability, CostProfile, Maturity, ModelSpec

    ov = dict(model.get("overrides") or {})
    proto = _proto(str(provider["protocol"]))
    is_local = bool(ov.get("isLocal", "127.0.0.1" in provider["baseUrl"]
                          or "localhost" in provider["baseUrl"]))
    kind_raw = str(ov.get("kind") or "http").lower()
    kind = {"http": BackendKind.HTTP, "comfyui": BackendKind.COMFYUI,
            "process": BackendKind.PROCESS}.get(kind_raw)
    if kind is None:
        raise ValueError(f"未知 kind {kind_raw!r}")

    # 请求形状：显式模板优先，其次协议预设，最后报错
    path = str(ov.get("path") or (proto or {}).get("path") or "")
    body = str(ov.get("bodyTemplate") or (proto or {}).get("body") or "")
    resp = str(ov.get("responsePath") or (proto or {}).get("response") or "")
    artifact = str(ov.get("artifactKind") or (proto or {}).get("artifact")
                   or ("image" if "IMAGE_" in str(model["task"]) else "text"))
    task_raw = str(model["task"] or (proto or {}).get("task") or "")
    if not path or not body or not resp:
        raise ValueError(
            f"无法确定请求形状：protocol={provider['protocol']!r} 未预设，"
            f"且未给 path/bodyTemplate/responsePath")
    # 模板里的 `{model}` 在**配置加载时**就烘成模型 id：
    # 模型名是配置的一部分，不该等到运行期再猜（IR 里没有 modelId 时会填空）。
    mid = str(model["id"])
    for pat in ('"{model}"', "{model}"):
        body = body.replace(pat, json.dumps(mid) if pat == '"{model}"' else mid)
    task = _task_kind(task_raw)

    be = Backend(
        kind=kind,
        endpoint=provider["baseUrl"],
        api_key_env=str(provider["keyEnv"]),
        path=path,
        method=str(ov.get("method") or "POST"),
        body_template=body,
        response_path=resp,
        response_b64_path=str(ov.get("responseB64Path")
                              or provider.get("responseB64Path") or ""),
        async_task=bool(ov.get("asyncTask", False)),
        status_path=str(ov.get("statusPath") or ""),
        artifact_kind=artifact,
        is_local_by_default=is_local,
        notes=str(provider["notes"]),
        allowed_agents=tuple(provider["allowedAgents"]),
        insecure_tls=bool(ov.get("insecureTls") or provider.get("insecureTls")),
        user_agent=str(ov.get("userAgent") or provider.get("userAgent") or ""),
    )
    lic = dict(provider["license"])
    return ModelSpec(
        model_id=str(model["id"]),
        display_name=f"{model['id']}（{provider['displayName']}）",
        task=task,
        backends=(be,),
        capability=Capability(**_capability_kwargs(ov)),
        cost=CostProfile(kind="local" if is_local else "cloud"),
        license_id=str(lic.get("id") or provider["id"]),
        maturity=Maturity.EXPERIMENTAL,
        notes=("**自定义供应商**（配置驱动，用户自备 key）· " + str(provider["notes"])).strip(),
    )


def load_custom_providers(path: str | Path | None = None) -> tuple[list[Any], list[str]]:
    """读取配置文件，返回 `(规格列表, 问题列表)`。**坏条目跳过并说明原因**。"""
    from little_crew.models.spec import Capability, CostProfile, Maturity, ModelSpec

    p = resolve_path(path)
    problems: list[str] = []
    if not p.is_file():
        return [], []                      # 没有配置文件不是错误
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:               # noqa: BLE001
        return [], [f"{p} 不是合法 JSON：{type(exc).__name__}: {exc}"]

    items = data.get("providers") if isinstance(data, Mapping) else None
    if not isinstance(items, list):
        return [], [f"{p} 缺少 providers 数组"]

    specs: list[Any] = []
    seen: set[str] = set()
    for idx, item in enumerate(items):
        if not isinstance(item, Mapping):
            problems.append(f"第 {idx + 1} 条不是对象，跳过")
            continue
        try:
            provider = _provider_base(item)
        except Exception as exc:                          # noqa: BLE001
            problems.append(f"第 {idx + 1} 条：{exc}，跳过")
            continue
        for model in provider["models"]:
            mid = str(model["id"])
            if mid in seen:
                problems.append(f"模型 id 重复 {mid!r}，跳过（供应商 {provider['id']}）")
                continue
            try:
                specs.append(_model_spec(provider, model))
            except Exception as exc:                      # noqa: BLE001
                problems.append(f"{mid}（{provider['id']}）：{exc}，跳过")
                continue
            seen.add(mid)
    return specs, problems


def _capability_kwargs(item: Mapping[str, Any]) -> dict[str, Any]:
    """只取配置里**显式给出**的能力项（不猜）。"""
    from little_crew.models.spec import Capability

    fields = set(Capability.__dataclass_fields__)
    cap = item.get("capability")
    if not isinstance(cap, Mapping):
        return {}
    return {k: v for k, v in cap.items() if k in fields}


def describe(path: str | Path | None = None) -> dict[str, Any]:
    """给 `doctor` 用的一句话状态。"""
    p = resolve_path(path)
    specs, problems = load_custom_providers(path)
    rows = []
    for s in specs:
        be = s.backends[0] if s.backends else None
        key_ok = bool(str(os.environ.get(getattr(be, "api_key_env", "") or "")
                          or "").strip()) if be is not None else False
        if be is not None and not key_ok:
            try:
                from little_crew.config import env_value

                key_ok = bool(str(env_value(getattr(be, "api_key_env", "") or "", "")
                                  or "").strip())
            except Exception:                             # noqa: BLE001
                key_ok = False
        rows.append({
            "id": s.model_id,
            "task": s.task.name if hasattr(s.task, "name") else str(s.task),
            "endpoint": getattr(be, "endpoint", ""),
            "keyEnv": getattr(be, "api_key_env", ""),
            "keyPresent": key_ok,
            "allowedAgents": list(getattr(be, "allowed_agents", ()) or ()),
        })
    return {"path": str(p), "exists": p.is_file(), "count": len(specs),
            "providers": rows, "problems": problems,
            "allowUnverified": allow_unverified()}
