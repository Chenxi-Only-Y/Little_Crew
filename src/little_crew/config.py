"""运行期配置读取 —— 从 `config/.env` 取**随部署变化**的值。

归属依据 `STRUCTURE.md` §2：**秘密凭据与端点**属于配置第 ①/② 类，
放 `config/.env`（不入库），模板 `.env.example` 入库。

零第三方依赖
------------
不引 `python-dotenv`：`dependencies = []` 是硬约束，
而 `.env` 的格式简单到不值得为它引一个包（见 `parse_env`）。

优先级（高 -> 低）
------------------
    1. 进程环境变量          —— 部署方/CI 的显式覆盖
    2. `config/.env`         —— 本项目"随部署变化"的配置
    3. **项目外秘密文件**     —— 见下
    4. 调用方给的默认值

**环境变量优先于文件**是刻意的：容器/CI 里通常只能设环境变量，
若文件反而优先，那些环境就无法覆盖本机配置。

为什么还有第 3 层（项目外秘密）
-------------------------------
`config/.env` 虽然在项目内且被 `.gitignore` 排除，但它**位置在项目里** ——
而项目迟早会被打包、压缩、复制、分享。凭据放在项目里，
**安全性就依赖"`.gitignore` 恰好写对了"**，一次 `git add -f` 或
一次拷走整个目录就漏了。

项目外那份**从位置上就不可能进版本库**，不依赖任何规则记得写。

默认路径 `D:\\AI\\_secrets\\little-crew.env`，可用 `LC_SECRETS_FILE`
环境变量指向别处（换机器/多项目共用时用得上）。

**顺序说明**：`config/.env` 在秘密文件**之前** ——
因为本机配置应当能覆盖共享秘密（例如临时换个模型试），
反之则会让"改 `.env` 不生效"变成一个难查的问题。

⚠️ **取值只看"非空"**：空值**不**参与遮蔽。否则 `.env.example`
拷成 `.env` 后满屏的 `KEY=` 会挡住秘密文件里的真值 ——
而那种失败的表现是"明明配了却读不到"，极难排查。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Mapping

__all__ = ["env_value", "load_env", "parse_env", "env_path",
           "describe", "repo_root", "secrets_path", "load_secrets"]


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
    """取一个配置值。

    优先级：**进程环境变量 > `config/.env` > 项目外秘密文件 > default**。

    ⚠️ 只有**非空**值才参与遮蔽 —— 理由见模块文档末尾。
    """
    v = os.environ.get(key)
    if v:
        return v
    for src in (load_env(root), load_secrets(root)):
        got = src.get(key, "")
        if got:
            return got
    return default


def describe(keys: Iterable[str],
             root: Path | None = None) -> list[dict[str, str]]:
    """给每个键说明「值从哪来、有没有值」—— 排查配置问题用。

    `source` 字段是关键：**"没配" 与 "配成空串" 是两回事**，
    而只看值看不出来。前者该走默认，后者往往是个错误。

    `source` 取值：`env`（进程环境）/ `file`（`config/.env`）/
    `secrets`（项目外秘密文件）/ `unset`（三处都没有）。

    ⚠️ **`value` 字段会带上真实值，包括密钥。** 它只该用在
    **本机排查**里，不要打印到日志、不要放进 CI 输出、不要写进报告。
    要安全地看，用 :func:`masked`（值只留头尾）。

    （这段曾经写的是「用 `mask=True`」，而本函数**没有**这个参数 ——
    照着文档写会 `TypeError`。审计 M-8 就是它；而 H-5 正是因为
    没人照着做，`api/server.py` 误用 `describe()` 把密钥明文发进了 HTTP 响应。）
    """
    file_vals, secret_vals = load_env(root), load_secrets(root)
    out: list[dict[str, str]] = []
    for k in keys:
        if os.environ.get(k):
            src, val = "env", os.environ[k]
        elif file_vals.get(k):
            src, val = "file", file_vals[k]
        elif secret_vals.get(k):
            src, val = "secrets", secret_vals[k]
        else:
            # 区分「键存在但为空」与「键根本没有」：前者常见于
            # 从 .env.example 拷来的模板，后者是真没配。
            src = ("file" if k in file_vals
                   else "secrets" if k in secret_vals else "unset")
            val = ""
        out.append({"key": k, "source": src, "value": val,
                    "empty": "yes" if (src != "unset" and not val) else "no"})
    return out


def masked(keys: Iterable[str], root: Path | None = None) -> list[dict[str, str]]:
    """与 `describe` 同样说明来源，但**值只留头尾**，可安全打印。

    密钥的排查只需要知道"配没配、从哪来、像不像"，不需要看到全文。
    """
    out = []
    for d in describe(keys, root):
        v = d["value"]
        if not v:
            shown = ""
        elif len(v) <= 10:
            shown = v[:2] + "*" * (len(v) - 2)
        else:
            shown = f"{v[:6]}…{v[-4:]}（{len(v)} 字符）"
        out.append({**d, "value": shown})
    return out


#: 项目外秘密文件的默认位置 —— **刻意在项目之外**。
#:
#: 为什么不放 `config/`：那样"不进版本库"就只靠 `.gitignore` 记得写对。
#: 放在项目外，从**位置**上就不可能被提交。
_DEFAULT_SECRETS = Path(r"D:\AI\_secrets\little-crew.env")


def secrets_path(root: Path | None = None) -> Path:
    """项目外秘密文件的路径。

    用 `LC_SECRETS_FILE` 覆盖默认位置（换机器 / 多项目共用一份时用）。
    """
    override = os.environ.get("LC_SECRETS_FILE", "").strip()
    return Path(override) if override else _DEFAULT_SECRETS


def load_secrets(root: Path | None = None) -> dict[str, str]:
    """读项目外秘密文件。**不存在就返回空字典，不抛异常。**

    不抛的理由与 `load_env` 相同：缺它是"这台机器没配秘密"，
    应当走默认值或由真正需要它的地方给出具体报错，
    而不是让所有命令（包括 `--help`）都失败。
    """
    p = secrets_path(root)
    if not p.is_file():
        return {}
    try:
        return parse_env(p.read_text(encoding="utf-8"))
    except OSError:
        return {}

