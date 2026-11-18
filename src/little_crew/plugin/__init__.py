"""插件注册表（2026-11-17）：**所有新职业/功能一律插件化**。

原则（项目纪律）：
1. 核心（`runtime/` / `agent/` / `compiler/` / `api/`）**不 import 具体插件**
2. 插件通过 `PluginManifest.provides` 注册能力（如 `series.plan`），
   通过 `PluginManifest.positions` 声明**剧组职位**（如 19 个核心职位）
3. 插件可卸载；缺插件时功能给出明确提示，不崩、不影响默认 19 职位单集流水线
4. 插件只能依赖核心公开模块（`runtime.*` / `agent.*`），反向依赖禁止

## 职位声明（2026-11-18，B1 全职业插件化）

`PluginManifest.positions` 是**职位声明**：名册/编制/依赖图/画布全部从注册表
构建，核心代码里不再硬编码职位。

```python
from little_crew.plugin import load_plugins, get_registry
load_plugins(["little_crew.plugins.series_showrunner"])
get_registry().positions()        # 全部职位（默认 19，加载 showrunner 后 20）
get_registry().default_roster()   # 默认名册职位（仅 default_roster=True 插件）
get_registry().position_owner("director")   # -> "core_crew"
```

**20/20 口径**：19 个核心职位（`plugins/core_crew.py`，`default_roster=True`）
+ 1 个总导演/showrunner（`plugins/series_showrunner.py`），全部通过 positions 声明；
默认单集名册仍是 19（showrunner 是插件专属，不进默认名册）。

## 默认插件

`DEFAULT_PLUGIN_MODULES = ("little_crew.plugins.core_crew",)`。
`ensure_default_plugins()` 幂等地保证默认插件已注册 —— `agent/roster.py`
在导入期调用它，所以**默认行为与迁移前一致**（19 职位名册立即可用）。
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Callable, Mapping

__all__ = ["PluginManifest", "PluginRegistry", "get_registry",
           "load_plugin", "load_plugins", "PluginError",
           "CORE_CAPABILITIES", "DEFAULT_PLUGIN_MODULES",
           "ensure_default_plugins"]


class PluginError(ValueError):
    """插件声明/加载/调用错误。"""


def _position_ids(manifest: "PluginManifest") -> list[str]:
    """职位声明的 agent_id 列表（duck-typing：只要求条目有非空 `agent_id`）。

    刻意**不 import** `little_crew.agent.crew_spec.AgentSpec`：插件机制属于
    机制层（包根、架构豁免），不应反向依赖 `agent` 层。职位条目由插件用
    `AgentSpec` 构造，这里只按协议取 `agent_id`。
    """
    return [str(getattr(p, "agent_id", "") or "").strip() for p in manifest.positions]


@dataclass(frozen=True)
class PluginManifest:
    """插件清单：名字 + 版本 + 提供的能力（key -> callable）+ 职位声明。"""
    name: str
    version: str = "0.1.0"
    description: str = ""
    provides: Mapping[str, Callable[..., Any]] = field(default_factory=dict)
    #: 该插件**依赖的能力**（其他插件或核心能力）；加载时校验、卸载时保护
    requires: tuple[str, ...] = ()
    optional: bool = True
    #: **职位声明**（2026-11-18，B1 全职业插件化）：条目须有非空 `agent_id`
    #: （duck-typing，通常是 `agent.crew_spec.AgentSpec`）。同一插件内不得重复。
    positions: tuple[Any, ...] = ()
    #: 该插件的职位是否构成**默认单集名册**（core_crew=True，showrunner=False）
    default_roster: bool = False

    def __post_init__(self) -> None:
        if not str(self.name).strip():
            raise PluginError("插件缺 name")
        if not isinstance(self.provides, Mapping):
            raise PluginError(f"插件 {self.name} 的 provides 必须是 Mapping")
        object.__setattr__(self, "positions", tuple(self.positions or ()))
        if not isinstance(self.default_roster, bool):
            raise PluginError(f"插件 {self.name} 的 default_roster 必须是 bool")
        #: 清单不能是空壳：能力与职位**至少一个非空**（2026-11-18）
        if not self.provides and not self.positions:
            raise PluginError(
                f"插件 {self.name} 没有提供任何能力或职位"
                f"（provides 与 positions 至少一个非空）")
        if self.default_roster and not self.positions:
            raise PluginError(
                f"插件 {self.name} 声明 default_roster=True 但未声明任何职位")
        for r in self.requires:
            if not str(r).strip():
                raise PluginError(f"插件 {self.name} requires 有空值")
        for k, v in self.provides.items():
            if not str(k).strip():
                raise PluginError(f"插件 {self.name} 能力 key 为空")
            if not callable(v):
                raise PluginError(f"插件 {self.name} 的能力 {k} 不可调用")
        seen: set[str] = set()
        for i, pos in enumerate(self.positions, 1):
            aid = str(getattr(pos, "agent_id", "") or "").strip()
            if not aid:
                raise PluginError(
                    f"插件 {self.name} 的第 {i} 个职位缺少非空 agent_id"
                    f"（positions 条目须有 agent_id）")
            if aid in seen:
                raise PluginError(
                    f"插件 {self.name} 内职位 agent_id 重复：{aid!r}")
            seen.add(aid)


class PluginRegistry:
    """进程内插件表；可注册/卸载/查能力/查职位。"""

    def __init__(self) -> None:
        self._plugins: dict[str, PluginManifest] = {}
        #: agent_id -> 插件名（职位索引，跨插件唯一）
        self._position_owners: dict[str, str] = {}

    def register(self, manifest: PluginManifest) -> None:
        if not isinstance(manifest, PluginManifest):
            raise PluginError("只能注册 PluginManifest")
        if manifest.name in self._plugins:
            raise PluginError(f"插件已注册：{manifest.name}")
        ids = _position_ids(manifest)
        clash = {pid: self._position_owners[pid] for pid in ids
                 if pid in self._position_owners}
        if clash:
            raise PluginError(
                f"插件 {manifest.name} 的职位 agent_id 已被其他插件声明：{clash}"
                f"（跨插件重复声明同一职位；本次注册被拒绝）")
        self._plugins[manifest.name] = manifest
        for pid in ids:
            self._position_owners[pid] = manifest.name

    def unload(self, name: str) -> bool:
        m = self._plugins.get(str(name))
        if m is None:
            return False
        provided = set(m.provides)
        for other in self._plugins.values():
            if other.name == m.name:
                continue
            hit = provided.intersection(other.requires)
            if hit:
                raise PluginError(
                    f"插件 {other.name} 依赖 {m.name} 的能力 {sorted(hit)}，不能卸载")
        self._plugins.pop(m.name, None)
        # 同步移除该插件的职位索引（只删归属本插件的，防御未来共享声明）
        for pid, owner in list(self._position_owners.items()):
            if owner == m.name:
                self._position_owners.pop(pid, None)
        return True

    def get(self, name: str) -> PluginManifest | None:
        return self._plugins.get(str(name))

    def names(self) -> list[str]:
        return sorted(self._plugins)

    def capabilities(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for m in self._plugins.values():
            for k in m.provides:
                out[k] = m.name
        return out

    def provider(self, capability: str) -> Callable[..., Any]:
        cap = str(capability)
        for m in self._plugins.values():
            fn = m.provides.get(cap)
            if fn is not None:
                return fn
        raise PluginError(f"没有插件提供能力 {cap}（请先 load_plugins）")

    # ---- 职位（2026-11-18，B1 全职业插件化）---------------------------

    def positions(self) -> list[Any]:
        """全部已注册插件的职位（**按插件注册顺序**，插件内按声明顺序）。"""
        out: list[Any] = []
        for m in self._plugins.values():
            out.extend(m.positions)
        return out

    def default_roster(self) -> tuple[Any, ...]:
        """**默认名册**职位：仅 `default_roster=True` 的插件声明（默认 19 条）。"""
        out: list[Any] = []
        for m in self._plugins.values():
            if m.default_roster:
                out.extend(m.positions)
        return tuple(out)

    def position_owner(self, agent_id: str) -> str:
        """职位归哪个插件；未注册 -> 空串。"""
        return self._position_owners.get(str(agent_id), "")


_GLOBAL = PluginRegistry()


def get_registry() -> PluginRegistry:
    return _GLOBAL


#: 核心能力（非插件提供）：插件可声明依赖这些，不需要注册
CORE_CAPABILITIES = frozenset({"llm.client", "vlm.judge", "runtime.chapter_plan",
                               "runtime.boundary_audit"})


def _check_requires(manifest: PluginManifest) -> None:
    have = set(_GLOBAL.capabilities()) | CORE_CAPABILITIES
    missing = [r for r in manifest.requires if r not in have]
    if missing:
        raise PluginError(
            f"插件 {manifest.name} 缺少依赖能力 {missing}（先加载依赖插件）")


def load_plugin(module: str | ModuleType) -> PluginManifest:
    """加载单个插件；依赖未满足 -> 拒绝且不注册。

    2026-11-18：删掉历史上重复定义的两个 `load_plugin`（第一个漏了
    `_check_requires`，被后一个覆盖；重复定义是隐患，故合并为唯一实现）。
    """
    mod = importlib.import_module(module) if isinstance(module, str) else module
    manifest = getattr(mod, "PLUGIN", None)
    if not isinstance(manifest, PluginManifest):
        raise PluginError(f"模块 {getattr(mod, '__name__', mod)} 没有 PLUGIN 清单")
    _check_requires(manifest)
    _GLOBAL.register(manifest)
    return manifest


def load_plugins(modules: list[str] | tuple[str, ...]) -> list[PluginManifest]:
    """按依赖拓扑加载一批插件（依赖在前）；缺依赖/成环 -> PluginError。"""
    mods = list(modules or ())
    manifests: dict[str, PluginManifest] = {}
    for m in mods:
        mod = importlib.import_module(m) if isinstance(m, str) else m
        man = getattr(mod, "PLUGIN", None)
        if not isinstance(man, PluginManifest):
            raise PluginError(f"模块 {getattr(mod, '__name__', mod)} 没有 PLUGIN 清单")
        manifests[man.name] = man
    # 插件能力 -> 提供者
    provider: dict[str, str] = {}
    for man in manifests.values():
        for cap in man.provides:
            provider[cap] = man.name
    order: list[str] = []
    done: set[str] = set()
    while len(order) < len(manifests):
        ready = []
        for name, man in manifests.items():
            if name in done:
                continue
            deps = {provider.get(r) for r in man.requires if provider.get(r)}
            if deps <= done:
                ready.append(name)
        if not ready:
            raise PluginError("插件依赖有环或缺少依赖")
        for name in sorted(ready):
            if name not in done:
                done.add(name)
                order.append(name)
    out = []
    for name in order:
        mod = next(m for m in mods
                   if getattr(importlib.import_module(m) if isinstance(m, str) else m,
                              "PLUGIN", None) is manifests[name])
        out.append(load_plugin(mod))
    return out


#: **默认插件模块**：导入 `agent.roster` 时自动确保注册（默认 19 职位名册）。
#: 想验证"缺默认插件"的失败语义：临时改这个元组（见 `runtime/core_crew_check.py`）。
DEFAULT_PLUGIN_MODULES: tuple[str, ...] = ("little_crew.plugins.core_crew",)


def ensure_default_plugins() -> list[PluginManifest]:
    """幂等地保证默认插件已注册（已注册则跳过；缺模块/坏清单 -> 清晰 PluginError）。

    幂等性对测试与卸载语义很重要：`unload("core_crew")` 后再次调用本函数
    会重新注册，而 `DEFAULT_ROSTER` 是模块级快照，不会因此改变。
    """
    out: list[PluginManifest] = []
    for module in DEFAULT_PLUGIN_MODULES:
        try:
            mod = importlib.import_module(module)
        except Exception as e:                              # noqa: BLE001
            raise PluginError(
                f"默认插件模块 {module!r} 导入失败：{type(e).__name__}: {e}"
                f"（检查 DEFAULT_PLUGIN_MODULES 与安装是否完整）") from e
        manifest = getattr(mod, "PLUGIN", None)
        if not isinstance(manifest, PluginManifest):
            raise PluginError(
                f"默认插件模块 {module!r} 没有 PLUGIN: PluginManifest")
        existing = _GLOBAL.get(manifest.name)
        if existing is not None:
            out.append(existing)
            continue
        _GLOBAL.register(manifest)
        out.append(manifest)
    return out
