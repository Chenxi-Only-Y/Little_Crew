"""小剧组 · Little Crew —— 基于多智能体协作的 AIGC 漫剧生成平台。

本包为**原项目整体回炉重造**的实现。当前版本 0.4.0-alpha.1。

分层与依赖方向见项目根 [`ARCHITECTURE.md`](../../../ARCHITECTURE.md)。
文件放置与配置约定见 [`STRUCTURE.md`](../../../STRUCTURE.md)。

依赖铁律
--------
``ir`` 与 ``governance`` 是叶子层，**不得 import 任何兄弟包**；
其余各层只能依赖序号更小的层。该约束由 ``tests/test_architecture.py`` 机器强制。

版本说明
--------
应用版本与协议版本是两件事，见 `little_crew.version` 模块的说明。
"""

from little_crew.version import (
    PACKAGE_NAME,
    PEP440_VERSION,
    PROJECT_NAME,
    PROJECT_SLUG,
    VERSION,
    VERSION_STAGE,
    protocol_versions,
    version_summary,
)

__all__ = [
    "PACKAGE_NAME",
    "PEP440_VERSION",
    "PROJECT_NAME",
    "PROJECT_SLUG",
    "VERSION",
    "VERSION_STAGE",
    "protocol_versions",
    "version_summary",
    "__version__",
]

#: 兼容惯例写法，与 VERSION 同源
__version__ = VERSION
