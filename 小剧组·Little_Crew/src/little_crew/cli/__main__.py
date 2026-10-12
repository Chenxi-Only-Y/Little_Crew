"""命令行入口 —— 平台的**产品级**对外接口。

与 demo 模块的区别
------------------
项目里已有若干 `*_demo.py`（`episode_demo` / `p3_episode` / `crew_demo` …），
它们的作用是**验证机制**：参数少、路径固定、输出给人看。

本模块是**产品入口**，三个差别：

| | demo 模块 | 本模块 |
|---|---|---|
| 参数 | 硬编码 + 少量开关 | 完整、有默认值、可脚本化 |
| 退出码 | 多为 0/1 | **分类的退出码**（见 `ExitCode`），CI 可据此分流 |
| 输出 | 给人看的富文本 | `--json` 给机器，默认给人 |

**为什么这很重要**：没有产品入口时，一切实测都只能靠 demo ——
而 demo 的参数不可控，"跑一集"就成了一次性动作，无法进 CI、无法被别人复现。

零第三方依赖
------------
只用标准库 `argparse`。`pyproject.toml` 声明 `dependencies = []`，
而 CLI 是**用户第一个碰到的面**——它若能因为缺依赖跑不起来，
"零依赖"这个卖点就白说了。

子命令
------
    probe     探测环境（ComfyUI / local-gen / 能力矩阵）
    compile   只编译 IR，产出执行图或请求体（不提交）
    shot      跑单个镜头
    episode   跑一集（分镜 → 渲染 → 门禁 → 合成）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from little_crew.cli import ExitCode

__all__ = ["main", "ExitCode", "build_parser"]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="little-crew",
        description="小剧组 · Little Crew —— AIGC 漫剧生成平台",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "退出码：0 成功 / 1 业务失败 / 2 环境不可用 / 3 用法错误\n"
            "示例：\n"
            "  little-crew probe\n"
            "  little-crew probe --json\n"
            "  little-crew episode --script config/script.txt --limit 3\n"
        ),
    )
    p.add_argument("--version", action="store_true", help="打印版本并退出")
    sub = p.add_subparsers(dest="command", metavar="<命令>")

    # ---- probe ----
    sp = sub.add_parser("probe", help="探测运行环境")
    sp.add_argument("--comfyui", default="", help="ComfyUI 端点（默认取 .env）")
    sp.add_argument("--local-gen", default="", dest="local_gen",
                    help="local-gen 端点（默认取 .env）")
    sp.add_argument("--json", action="store_true", help="输出 JSON")
    sp.add_argument("--save-snapshot", default="",
                    help="把能力矩阵快照存到指定路径")

    # ---- compile ----
    sc = sub.add_parser("compile", help="只编译 IR（不提交）")
    sc.add_argument("ir", help="IR JSON 文件路径")
    sc.add_argument("--out", default="", help="输出路径（默认打印到 stdout）")
    sc.add_argument("--json", action="store_true", help="输出 JSON")

    # ---- shot ----
    ss = sub.add_parser("shot", help="跑单个镜头")
    ss.add_argument("shot", help="镜头 JSON 文件路径，或一段直接描述")
    ss.add_argument("--channel", choices=("comfyui", "local-gen"),
                    default="local-gen", help="走哪条通道")
    ss.add_argument("--out", default="outputs/cli", help="产物目录")
    ss.add_argument("--seed", type=int, default=7)
    ss.add_argument("--steps", type=int, default=8)
    ss.add_argument("--json", action="store_true")

    # ---- episode ----
    se = sub.add_parser("episode", help="跑一集")
    se.add_argument("--script", default="", help="剧本文件路径")
    se.add_argument("--limit", type=int, default=0, help="只跑前 N 镜（0=全部）")
    se.add_argument("--channel", choices=("comfyui", "local-gen"),
                    default="local-gen")
    se.add_argument("--out", default="outputs/episode", help="工作目录")
    se.add_argument("--seed", type=int, default=7)
    se.add_argument("--steps", type=int, default=8)
    se.add_argument("--no-gate", action="store_true",
                    help="关闭验收门禁（默认开启）")
    se.add_argument("--no-video", action="store_true", help="不做视频合成")
    se.add_argument("--white-model", default="",
                    help="白模序列根目录（启用空间控制）")
    se.add_argument("--json", action="store_true")

    return p


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:                                   # noqa: BLE001
        pass

    args = build_parser().parse_args(argv)

    if args.version:
        from little_crew.version import VERSION
        print(f"小剧组 · Little Crew  {VERSION}")
        return ExitCode.OK

    if not args.command:
        build_parser().print_help()
        return ExitCode.USAGE

    from little_crew.cli import commands

    handler = {
        "probe": commands.cmd_probe,
        "compile": commands.cmd_compile,
        "shot": commands.cmd_shot,
        "episode": commands.cmd_episode,
    }.get(args.command)
    if handler is None:                                 # pragma: no cover
        print(f"未知命令：{args.command}", file=sys.stderr)
        return ExitCode.USAGE

    try:
        return int(handler(args))
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return ExitCode.INTERRUPTED
    except FileNotFoundError as e:
        print(f"文件不存在：{e}", file=sys.stderr)
        return ExitCode.USAGE
    except ValueError as e:
        print(f"参数错误：{e}", file=sys.stderr)
        return ExitCode.USAGE


if __name__ == "__main__":
    raise SystemExit(main())
