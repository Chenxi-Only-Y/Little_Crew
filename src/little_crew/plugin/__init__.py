"""插件注册表（2026-11-17）：**所有新职业/功能一律插件化**。

原则（项目纪律）：
1. 核心（`runtime/` / `agent/` / `compiler/` / `api/`）**不 import 具体插件**
2. 插件通过 `PluginManifest.provides` 注册能力（如 `series.plan` / `position.showrunner`）
3. 插件可卸载；缺插件时功能给出明确提示，不崩、不影响默认 19 职位单集流水线
4. 插件只能依赖核心公开模块（`runtime.*` / `agent.*`），反向依赖禁止

用法：
```python
from little_crew.plugin import load_plugins, get_registry
load_plugins(["little_crew.plugins.series_showrunner"])
fn = get_registry().provider("series.plan")
fn(outline, client=...)
```
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Callable, Mapping

__all__ = ["PluginManifest", "PluginRegistry", "get_registry",
           "load_plugin", "load_plugins", "PluginError"]


class PluginError(ValueError):
    """插件声明/加载/调用错误。"""


@dataclass(frozen=True)
class PluginManifest:
    """插件清单：名字 + 版本 + 提供的能力（key -> callable）。"""
    name: str
    version: str = "0.1.0"
    description: str = ""
    provides: Mapping[str, Callable[..., Any]] = field(default_factory=dict)
    optional: bool = True

    def __post_init__(self) -> None:
        if not str(self.name).strip():
            raise PluginError("插件缺 name")
        if not isinstance(self.provides, Mapping) or not self.provides:
            raise PluginError(f"插件 {self.name} 没有提供任何能力")
        for k, v in self.provides.items():
            if not str(k).strip():
                raise PluginError(f"插件 {self.name} 能力 key 为空")
            if not callable(v):
                raise PluginError(f"插件 {self.name} 的能力 {k} 不可调用")


class PluginRegistry:
    """进程内插件表；可注册/卸载/查能力。"""

    def __init__(self) -> None:
        self._plugins: dict[str, PluginManifest] = {}

    def register(self, manifest: PluginManifest) -> None:
        if not isinstance(manifest, PluginManifest):
            raise PluginError("只能注册 PluginManifest")
        if manifest.name in self._plugins:
            raise PluginError(f"插件已注册：{manifest.name}")
        self._plugins[manifest.name] = manifest

    def unload(self, name: str) -> bool:
        return self._plugins.pop(str(name), None) is not None

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


_GLOBAL = PluginRegistry()


def get_registry() -> PluginRegistry:
    return _GLOBAL


def load_plugin(module: str | ModuleType) -> PluginManifest:
    """加载单个插件模块：模块必须暴露 `PLUGIN: PluginManifest`。"""
    mod = importlib.import_module(module) if isinstance(module, str) else module
    manifest = getattr(mod, "PLUGIN", None)
    if not isinstance(manifest, PluginManifest):
        raise PluginError(f"模块 {getattr(mod, '__name__', mod)} 没有 PLUGIN 清单")
    _GLOBAL.register(manifest)
    return manifest


def load_plugins(modules: list[str] | tuple[str, ...]) -> list[PluginManifest]:
    return [load_plugin(m) for m in (modules or ())]
