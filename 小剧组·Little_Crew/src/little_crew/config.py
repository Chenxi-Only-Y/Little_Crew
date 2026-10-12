"""运行期配置读取 —— 从 `config/.env` 取**随部署变化**的值。

归属依据 `STRUCTURE.md` §2：**秘密凭据与端点**属于配置第 ①/② 类，
放 `config/.env`（不入库），模板 `.env.example` 入库。

零第三方依赖
------------
不引 `python-dotenv`：`dependencies = []` 是硬约束，
而 `.env` 的格式简单到不值得为它引一个包（见 `parse_env`）。

优先级（高 -> 低）
------------------
    1. 进程环境变量       —— 部署方/CI 的显式覆盖
    2. `config/.env`      —— 本机配置
    3. 调用方给的默认值

**环境变量优先于文件**是刻意的：容器/CI 里通常只能设环境变量，
若文件反而优先，那些环境就无法覆盖本机配置。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Mapping

__all__ = ["env_value", "load_env", "parse_env", "env_path",
           "describe", "repo_root"]


def parse_env(text: str) -> dict[str, str]:
    """解析 `.env` 文本。

    支持的语法（**刻意保持最小**）：
      · `KEY=VALUE`
      · `# 注释` 与空行忽略
      · 值两端可有引号（`"..."` / `'...'`），引号会被剥掉
      · `export KEY=VALUE` 前缀被容忍（很多人习惯那么写）

    **不支持**多行值、变量插值、命令替换 —— 那些是 shell 的功能，
    在配置文件里做会让"这个值到底是什么"变得难以静态判断。
    需要它们时应当由部署方在环境变量里算好。
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        out[key] = val
    return out


#: 用**标记文件**定位项目根，而不是数 `parents[N]`。
#:
#: ⚠️ 数层数踩过坑：`config.py` 在 `src/little_crew/` 下，根是 `parents[2]`；
#: 而 `cli/` 里的模块是 `parents[3]`。同一个仓库里两个深度，
#: 用同一套 `parents[N]` 必然有一处错 —— 而错了的表现是
#: **配置静默读不到**（`.env` 里明明有值，`env_value` 返回空串）。
#: 标记文件不依赖深度，挪动模块也不会失效。
_ROOT_MARKERS: tuple[str, ...] = ("pyproject.toml", "STRUCTURE.md")


def repo_root(start: Path | None = None) -> Path:
    """向上找项目根：第一个含标记文件的目录。

    找不到时退回 `parents[2]`（包根的上两级）——
    **不抛异常**：本项目可能被以 zip/单文件方式使用，
    那时没有"项目根"这个概念，配置走默认值即可。
    """
    here = (start or Path(__file__)).resolve()
    for d in (here, *here.parents):
        if any((d / m).is_file() for m in _ROOT_MARKERS):
            return d
    return here.parents[2]


def env_path(repo_root_: Path | None = None) -> Path:
    """`config/.env` 的路径。

    参数名带下划线是为了不与上面的 `repo_root()` 函数撞名 ——
    同名参数会**遮蔽**模块级函数，而那种 bug 很难看出来。
    """
    root = repo_root_ or repo_root()
    return root / "config" / ".env"


def load_env(root: Path | None = None) -> dict[str, str]:
    """读 `.env`。**文件不存在返回空字典，不抛异常。**

    不抛的理由：`.env` 是"本机配置"，缺它应当走默认值，
    而不是让所有命令都失败。真正需要某个键的地方会在取不到时
    给出具体报错（并说明该去哪配）。
    """
    p = env_path(root)
    if not p.is_file():
        return {}
    try:
        return parse_env(p.read_text(encoding="utf-8"))
    except OSError:
        return {}


def env_value(key: str, default: str = "",
              root: Path | None = None) -> str:
    """取一个配置值：**进程环境变量 > `.env` > default**。"""
    v = os.environ.get(key)
    if v:
        return v
    return load_env(root).get(key, default) or default


def describe(keys: Iterable[str],
             root: Path | None = None) -> list[dict[str, str]]:
    """给每个键说明「值从哪来、有没有值」—— 排查配置问题用。

    `source` 字段是关键：**"没配" 与 "配成空串" 是两回事**，
    而只看值看不出来。前者该走默认，后者往往是个错误。
    """
    file_vals = load_env(root)
    out: list[dict[str, str]] = []
    for k in keys:
        if os.environ.get(k):
            src, val = "env", os.environ[k]
        elif k in file_vals:
            src, val = "file", file_vals[k]
        else:
            src, val = "unset", ""
        out.append({"key": k, "source": src, "value": val,
                    "empty": "yes" if (src != "unset" and not val) else "no"})
    return out
