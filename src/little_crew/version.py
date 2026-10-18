"""版本与标识的唯一来源。

设计依据（STRUCTURE.md 原则 1）
------------------------------
本模块集中定义**应用版本**，避免在多处重复维护导致漂移。

三类"版本"必须严格区分，**不可互相代替**：

  1. **应用版本**（本模块 ``__version__``）
     整个产品《小剧组 · Little Crew》的发行版本。改动它**不影响**任何缓存。

  2. **协议版本**（各自模块内定义，本模块只做汇总引用）
     - ``ir.canonical.IR_VERSION``          —— Unified IR 规范版本
     - ``compiler.COMPILE_RULE_VERSION``    —— 编译规则版本
     - ``capability.CATALOG_PROTOCOL_VERSION`` —— 能力矩阵协议版本
     - ``capability.comfy_interfaces.COMPAT_PROTOCOL_VERSION`` —— 接口契约版本
     这些**参与缓存键**：一旦变更，旧缓存必须失效（见设计文档 §7.5「版本化变换」原则）。

  3. **业务版本**（运行时数据携带）
     - 模型版本、权重版本、引擎版本 —— 属 `ModelSpec` / 产物时效清单的字段，
       不在代码常量里。

> ⚠️ **常见错误**：把应用版本写进缓存键。后果是"改个版本号（哪怕只改文档）
> 就让全量缓存失效"。反过来，把协议版本漏出缓存键，后果是"规则升级后
> 静默复用旧产物"。两者都发生过，因此在本模块顶部显式声明这条边界。
"""

from __future__ import annotations

__all__ = [
    "PROJECT_NAME",
    "PROJECT_SLUG",
    "PACKAGE_NAME",
    "VERSION",
    "VERSION_STAGE",
    "PEP440_VERSION",
    "__version__",
    "protocol_versions",
    "version_summary",
]

#: 产品名（用于界面、日志、成片尾帧）
PROJECT_NAME = "小剧组 · Little Crew"

#: 面向仓库/文件系统的短名
PROJECT_SLUG = "little-crew"

#: Python 导入包名
PACKAGE_NAME = "little_crew"

#: 人类可读的版本号。**发行流程只改这一处。**
VERSION = "0.4.0-alpha.58"

#: 版本阶段。用于运行时行为分支（如 alpha 阶段打印更激进的告警）
VERSION_STAGE = "alpha"

#: PEP 440 规范化后的版本。规范用连字符（alpha.1），
#: 而 Python 打包体系要求用点（a1）；两者指向同一版本。
PEP440_VERSION = "0.4.0a58"

#: 兼容惯例：与本模块 VERSION 保持同步
__version__ = VERSION


def protocol_versions() -> dict[str, str]:
    """汇总所有**参与缓存键**的协议版本。

    刻意用函数而非模块级常量：这些值分别定义在各自模块中，
    在模块级导入会形成循环依赖（如 `ir` 不得依赖 `compiler`）。
    """
    out: dict[str, str] = {}
    try:
        from little_crew.ir import canonical

        out["ir"] = canonical.IR_VERSION
    except ImportError:  # pragma: no cover - 部分包未安装时仍可读应用版本
        pass
    try:
        from little_crew.capability import capability, comfy_interfaces

        out["catalogProtocol"] = capability.CATALOG_PROTOCOL_VERSION
        out["comfyInterfaces"] = comfy_interfaces.COMPAT_PROTOCOL_VERSION
    except ImportError:  # pragma: no cover
        pass
    try:
        from little_crew.compiler import compiler

        out["compileRule"] = compiler.COMPILE_RULE_VERSION
    except ImportError:  # pragma: no cover
        pass
    return out


def version_summary() -> dict[str, object]:
    """一份可直接打印/落盘的版本清单，用于故障排查与交付证据。"""
    return {
        "project": PROJECT_NAME,
        "slug": PROJECT_SLUG,
        "package": PACKAGE_NAME,
        "version": VERSION,
        "stage": VERSION_STAGE,
        "pep440": PEP440_VERSION,
        "protocols": protocol_versions(),
    }
