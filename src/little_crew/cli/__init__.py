"""命令行入口 —— 平台的**产品级**对外接口。

与 demo 模块的区别
------------------
项目里已有若干 `*_demo.py`（`episode_demo` / `p3_episode` / `crew_demo` …），
它们的作用是**验证机制**：参数少、路径固定、输出给人看。

本包是**产品入口**，三个差别：

| | demo 模块 | 本包 |
|---|---|---|
| 参数 | 硬编码 + 少量开关 | 完整、有默认值、可脚本化 |
| 退出码 | 多为 0/1 | **分类的退出码**（见 `ExitCode`），CI 可据以分流 |
| 输出 | 给人看的富文本 | `--json` 给机器，默认给人 |

**为什么这很重要**：没有产品入口时，一切实测都只能靠 demo ——
而 demo 的参数不可控，"跑一集"就成了一次性动作，
无法进 CI、无法被别人复现。

零第三方依赖
------------
只用标准库 `argparse`。`pyproject.toml` 声明 `dependencies = []`，
而 CLI 是**用户第一个碰到的面** —— 它若能因为缺依赖跑不起来，
"零依赖"这个卖点就白说了。

用法
----
    python -m little_crew.cli probe
    python -m little_crew.cli episode --limit 3
    little-crew probe          # 安装后（见 pyproject 的 entry-points）
"""

from __future__ import annotations

__all__ = ["ExitCode", "main", "build_parser"]


class ExitCode:
    """退出码 —— **分类而非二值**，让 CI 能据以分流。

    `1` 与 `2` 的区别很实际：
      · `1` 是我们自己的问题（代码/配置），该修
      · `2` 是**环境**的问题（服务没起、权重没下），该去装

    两者混用会让"跑失败"变成一个需要人工判断的模糊状态 ——
    而 CI 恰恰最不该需要人工判断。
    """

    OK = 0
    #: 业务失败：门禁未过、渲染失败、剧本未通过
    FAILED = 1
    #: 环境不可用：服务连不上、缺节点、缺权重
    ENVIRONMENT = 2
    #: 用法错误：参数不对、文件不存在
    USAGE = 3
    #: 被中断（Ctrl-C）
    INTERRUPTED = 130


def __getattr__(name: str):
    """惰性转发 `main` / `build_parser` 到 `__main__`。

    这样 `from little_crew.cli import main` 可用，
    又不必在 import 包时就拉起 argparse 的全部子命令定义。
    """
    if name in ("main", "build_parser"):
        from little_crew.cli import __main__ as m
        return getattr(m, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
