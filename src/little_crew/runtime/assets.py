"""**资产库**：把一集里产出的"可复用资产"落成目录，并能读回来。

## 为什么要有它

在此之前，角色定妆表只活在两个地方：

1. `EpisodeRunner.characters`（内存）
2. `delivery/run.report.json` 的 `characters.sheets`（那次运行的报告里）

后果：**换一集，6 个角色从头再生成** —— 无法"还是这个少女"。
而这是"角色一致"的前置：连同一个角色对象都留不下来，谈不上一致。

## 目录形状

```
<workdir>/assets/
  manifest.json                     ← 清单：每个资产在哪、来自哪一集、seed
  characters/
    少女/
      sheet.json                    ← 定妆表（文字半）
      anchor.png                    ← 定妆图（**待做，本模块不生成**）
    旅人/
      sheet.json
```

## 三条刻意的设计

**① 目录名用角色名（人看得懂），ID 写在文件里（机器认得出）**

目录叫 `少女`，文件里有 `characterId: "CHAR-069A"`。
两者都要 —— 只有 ID 没人看得懂，只有名字会重名。

**② 同名但不同 ID：不覆盖，另起一个目录**

`少女` 已存在且 ID 是 `CHAR-069A`，这次是 `CHAR-0B12` →
新目录 `少女@CHAR-0B12`，并在清单里标 `conflict: true`。

> **静默覆盖是最坏的：两个"少女"其实是两个人，覆盖之后没人能发现。**
> 宁可多一个目录，也要让冲突显性。

**③ 每份资产记来源（`sourceEpisodeId` / `seed` / `createdAt`）**

这是「可追溯」的最低要求：复用之后出了问题，
要能回答"这个少女到底是哪一集、哪一次、哪个 seed 定的"。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

__all__ = [
    "ASSETS_DIR",
    "MANIFEST_NAME",
    "safe_name",
    "write_characters",
    "read_characters",
    "load_manifest",
]

#: 资产根目录名（放在 workdir 下）
ASSETS_DIR = "assets"

#: 清单文件名
MANIFEST_NAME = "manifest.json"

#: 文件名里不要的字符（Windows 保留字符 + 路径分隔符）
_BAD = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def safe_name(name: str, *, fallback: str = "unnamed") -> str:
    """把角色名变成可用作目录名的字符串。**不改变可读性优先**。

    中文原样保留（Windows/NTFS 支持），只清掉路径分隔符与控制字符。
    空名或全是坏字符时退回 `fallback`。
    """
    s = _BAD.sub("_", str(name or "").strip())
    # 连"只由分隔符构成"也算空名（`///` -> `_` -> 空 -> 兜底）。
    # 一个叫 `_` 的目录既认不出是谁，又极易和别的 `_` 撞。
    s = s.strip(" ._-")
    if not s:
        return fallback
    # 目录名过长会顶到 Windows 的 260 字符路径限制
    return s[:40]


def _char_dir(root: Path, name: str, cid: str) -> tuple[Path, bool]:
    """决定这个角色写到哪个目录。返回 (目录, 是否发生同名冲突)。"""
    base = safe_name(name)
    d = root / "characters" / base
    if not d.exists():
        return d, False
    old = d / "sheet.json"
    if not old.is_file():
        return d, False
    try:
        oid = str(json.loads(old.read_text(encoding="utf-8")).get("characterId") or "")
    except Exception:                                        # noqa: BLE001
        return d, False
    if not oid or not cid or oid == cid:
        return d, False
    # 同名不同 ID：另起目录，**不覆盖**
    suffix = cid.replace("CHAR-", "")[-8:] or "x"
    return root / "characters" / f"{base}@{suffix}", True


def write_characters(
    workdir: Path,
    sheets: Iterable[Mapping[str, Any]],
    *,
    episode_id: str = "",
    seed: str = "",
) -> dict[str, Any]:
    """把角色定妆表落成 `assets/characters/<角色>/sheet.json`。

    返回写好的清单（也写进 `assets/manifest.json`）。
    **幂等**：同一 ID 重复写只是刷新时间戳。
    """
    workdir = Path(workdir)
    root = workdir / ASSETS_DIR
    (root / "characters").mkdir(parents=True, exist_ok=True)

    entries: list[dict[str, Any]] = []
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for sh in sheets or ():
        if not isinstance(sh, Mapping):
            continue
        name = str(sh.get("name") or "")
        cid = str(sh.get("characterId") or sh.get("character_id") or "")
        d, conflict = _char_dir(root, name, cid)
        d.mkdir(parents=True, exist_ok=True)

        doc = dict(sh)
        # 来源三件套 —— 复用它之后出问题，靠这三个回答"哪来的"
        doc["sourceEpisodeId"] = episode_id
        doc["sourceSeed"] = seed
        doc["writtenAt"] = now

        (d / "sheet.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

        anchor = d / "anchor.png"
        entries.append({
            "kind": "character",
            "name": name,
            "characterId": cid,
            # 清单里存**相对 workdir 的 POSIX 路径**，换机器也能读
            "path": (d / "sheet.json").relative_to(workdir).as_posix(),
            "anchor": (anchor.relative_to(workdir).as_posix()
                       if anchor.is_file() else None),
            "conflict": conflict,
        })

    manifest = {
        "kind": "assets.manifest",
        "episodeId": episode_id,
        "seed": seed,
        "updatedAt": now,
        "characters": entries,
    }
    (root / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def load_manifest(workdir: Path) -> dict[str, Any]:
    """读清单。没有就返回空清单（**不抛**，调用方不该为此写 try）。"""
    p = Path(workdir) / ASSETS_DIR / MANIFEST_NAME
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:                                        # noqa: BLE001
        return {"kind": "assets.manifest", "characters": []}


def read_characters(workdir: Path) -> dict[str, dict[str, Any]]:
    """读回全部角色定妆表，键是 `sheet.json` 里记的**名字**。

    读回来是为了**下一集复用** —— 拿到之后就能跳过"重新设计角色"，
    也让 `anchor_sets` 有图路径可传（图存在时）。
    """
    workdir = Path(workdir)
    root = workdir / ASSETS_DIR / "characters"
    out: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        f = d / "sheet.json"
        if not (d.is_dir() and f.is_file()):
            continue
        try:
            sh = json.loads(f.read_text(encoding="utf-8"))
        except Exception:                                    # noqa: BLE001
            continue
        if not isinstance(sh, dict):
            continue
        # 名字优先取文件里的（目录名可能带了 @ID 后缀）
        key = str(sh.get("name") or d.name)
        if key in out:
            key = f"{key}@{d.name}"
        out[key] = sh
    return out
