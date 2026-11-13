"""`little-crew provider` —— 第三方供应商 / 自定义模型 API 的配置入口（2026-11-13）。

对齐界面上的那几栏：

```
provider vendors                     # 内置供应商目录（下拉框的选项）
provider list                        # 已配置的供应商与模型（含 key 是否在）
provider test <model_id>             # 只检查配置 + key + 端点可达，**不生成、不花钱**
provider add --json '{...}'          # 往 config/providers.custom.json 追加一条（原子写）
```

设计取舍：
- **test 不生成**：生成要花钱、要授权核验；"配置对不对"与"能不能出图"是两件事，
  混在一起会让人以为测过了就等于能用了。
- **add 用 JSON 输入**：字段多（供应商/协议/模型目录/用途限定），交互式问答容易漏字段；
  JSON 输入可以直接进 CI/脚本，也便于把界面上填的东西原样贴过来。
- **写之前校验**：`add` 先让加载器解析一遍，非法就**不落盘**（避免写出一个坏配置）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _print(text: str = "") -> None:
    print(text)


def cmd_provider(args: Any) -> int:
    action = str(getattr(args, "provider_action", "") or "")
    if action == "vendors":
        return _cmd_vendors()
    if action == "list":
        return _cmd_list()
    if action == "test":
        return _cmd_test(getattr(args, "model_id", "") or "")
    if action == "add":
        return _cmd_add(getattr(args, "json", "") or "",
                        dry=bool(getattr(args, "dry_run", False)))
    _print("用法：little-crew provider {vendors|list|test <id>|add --json '<json>'}")
    return 2


def _cmd_vendors() -> int:
    from little_crew.models.providers_builtin import describe_vendors

    rows = describe_vendors()
    _print(f"内置供应商目录（{len(rows)} 家）—— 选 --vendor 后 baseUrl/协议/凭据变量自动继承")
    _print("")
    _print(f"  {'id':12s} {'协议':22s} {'凭据变量':22s} 说明")
    for r in rows:
        _print(f"  {r['id']:12s} {r['protocol']:22s} {r['keyEnv']:22s} {r['displayName']}")
    return 0


def _cmd_list() -> int:
    from little_crew.models.custom_providers import describe, resolve_path

    info = describe()
    p = resolve_path()
    if not info["exists"]:
        _print(f"未配置自定义供应商（{p} 不存在）")
        _print("  用 `little-crew provider add --json '...'` 添加，或参考 config/providers.custom.json")
        return 0
    _print(f"自定义供应商：{info['path']}（{info['count']} 个模型）")
    if info.get("allowUnverified"):
        _print("  ⚠️ 已开启 LITTLE_CREW_ALLOW_UNVERIFIED=1（未核实授权也将放行）")
    _print("")
    for r in info["providers"]:
        key = "key √" if r["keyPresent"] else f"缺 {r['keyEnv'] or '(未声明变量)'}"
        scope = ("限定 " + ",".join(r["allowedAgents"])) if r["allowedAgents"] else "不限职位"
        _print(f"  {r['id']:24s} {r['task']:12s} {key:14s} {scope}")
    if info["problems"]:
        _print("")
        _print(f"  ⚠️ {len(info['problems'])} 条配置问题：")
        for msg in info["problems"]:
            _print(f"    · {msg}")
    return 0


def _cmd_test(model_id: str) -> int:
    """配置完整性 + key 是否在 + 端点可达。**不生成、不花钱。**"""
    from little_crew.config import env_value
    from little_crew.models.registry import build_default_registry
    from little_crew.runtime.backend_guard import allowed_agents

    if not model_id:
        _print("用法：little-crew provider test <model_id>")
        return 2
    reg = build_default_registry()
    spec = reg.get(model_id)
    if spec is None:
        _print(f"❌ 注册表里没有 {model_id!r}")
        return 1
    be = spec.backends[0] if spec.backends else None
    if be is None:
        _print(f"❌ {model_id} 没有可用后端")
        return 1

    ok = True
    _print(f"模型 {model_id} · {spec.display_name}")
    _print(f"  task      : {getattr(spec.task, 'name', spec.task)}")
    _print(f"  endpoint  : {be.endpoint or '(空)'}")
    _print(f"  path      : {be.path}  ← {be.method}")
    _print(f"  response  : {be.response_path}")
    scope = allowed_agents(be) or ("（不限）",)
    _print(f"  用途限定  : {list(scope)}")
    if not be.endpoint or not be.path or not be.body_template:
        _print("  ❌ 配置不完整（endpoint/path/bodyTemplate）")
        ok = False
    else:
        _print("  ✅ 配置完整")

    key_env = str(be.api_key_env or "")
    if not key_env:
        _print("  ⚠️ 未声明凭据变量（若该端点无需鉴权可忽略）")
    else:
        val = str(env_value(key_env, "") or "")
        if val:
            _print(f"  ✅ 凭据 {key_env} 已设置（{len(val)} 字符）")
        else:
            _print(f"  ❌ 凭据 {key_env} 未设置（在 config/.env 里填）")
            ok = False

    # 端点可达性（只发 HEAD/GET，不调用生成接口）
    try:
        import urllib.request

        req = urllib.request.Request(be.endpoint, method="GET",
                                     headers={"User-Agent": "little-crew-doctor"})
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:   # noqa: S310
                _print(f"  ✅ 端点可达（HTTP {resp.status}）")
        except Exception as exc:                              # noqa: BLE001
            # 很多 API 根路径会 404/401，这仍说明"网络通了"；只把连接失败算失败
            if "timed out" in str(exc).lower() or "refused" in str(exc).lower() \
                    or "Name or service" in str(exc) or "getaddrinfo" in str(exc):
                _print(f"  ❌ 端点不可达：{type(exc).__name__}: {exc}")
                ok = False
            else:
                _print(f"  ✅ 端点可达（根路径返回 {type(exc).__name__} 属正常）")
    except Exception as exc:                                  # noqa: BLE001
        _print(f"  ⚠️ 可达性检查未执行：{type(exc).__name__}: {exc}")

    _print("")
    _print("✅ 可以尝试调用" if ok else "❌ 还不能用（先修上面的 ❌）")
    return 0 if ok else 1


def _cmd_add(payload: str, *, dry: bool) -> int:
    """往配置文件追加一条供应商。**先校验，非法不落盘。**"""
    from little_crew.models.custom_providers import load_custom_providers, resolve_path

    if not payload.strip():
        _print("用法：little-crew provider add --json '<供应商 JSON>'")
        _print("      示例：--json '{\"id\":\"acme\",\"vendor\":\"zai\",\"apiKeyEnv\":\"ZAI_API_KEY\","
               "\"allowedAgents\":[\"character_designer\"],\"models\":[{\"id\":\"cogview-3-flash\","
               "\"task\":\"image.t2i\"}]}'")
        return 2
    try:
        entry = json.loads(payload)
    except Exception as exc:                                  # noqa: BLE001
        _print(f"❌ --json 不是合法 JSON：{type(exc).__name__}: {exc}")
        return 2
    if not isinstance(entry, dict):
        _print("❌ --json 必须是对象")
        return 2

    path = resolve_path()
    data: dict[str, Any] = {"version": 2, "providers": []}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:                              # noqa: BLE001
            _print(f"❌ 现有配置不是合法 JSON，拒绝覆盖：{type(exc).__name__}: {exc}")
            return 2
        if not isinstance(data, dict) or not isinstance(data.get("providers"), list):
            _print("❌ 现有配置缺少 providers 数组，拒绝覆盖")
            return 2
    existing = {str(p.get("id")) for p in data["providers"] if isinstance(p, dict)}
    if str(entry.get("id")) in existing:
        _print(f"❌ 供应商 id {entry.get('id')!r} 已存在（先删旧条目，或改 id）")
        return 2

    data["providers"].append(entry)

    # **先校验**：写进临时文件，用加载器解析一遍
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "providers.json"
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        specs, problems = load_custom_providers(tmp)
        if problems:
            _print(f"❌ 校验未通过（{len(problems)} 条），未写入：")
            for msg in problems:
                _print(f"   · {msg}")
            return 2
        _print(f"✅ 校验通过：将新增 {len(specs)} 个模型（供应商 {entry.get('id')}）")

    if dry:
        _print("（--dry-run：未写入）")
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)
    _print(f"✅ 已写入 {path}")
    _print("  下一步：little-crew provider test <模型 id>")
    return 0
