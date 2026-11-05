"""角色锚点图（定妆图）的发现与匹配 —— 把资产接进 IR 的 `identity.reference`。

## 为什么要有这一层

`shot_to_ir(anchor_sets={角色: 图路径})` 从 `alpha.130` 起就支持，
编译器也有 `identity.ipadapter -> identity.lora -> identity.lora_model_only` 降级链，
**但从来没有任何调用方传过 `anchor_sets`**（2026-11-05 审计确认）。
于是「角色一致性」一直只停留在**提示词文字层**。

本模块只做两件纯事：
1. `discover(root)`：在 `assets/characters/<名字>/anchor.png` 里找图
2. `sets_for(characters, anchors)`：按镜头的出场角色取出对应图

**找不到就不传** —— 宁可不加参考节点，也不编一个不存在��路径。
"""
from __future__ import annotations

from pathlib import Path

ANCHOR_NAME = "anchor.png"


def discover(root) -> dict[str, str]:
    """扫描 `<root>/characters/*/anchor.png`，返回 `{名字: 绝对路径}`。

    目录名可能是 `少女` 或 `少女@5BF9`（同名不同 ID 时的去重后缀），
    两种都按**名字前缀**建键，避免下游匹配不到。
    """
    base = Path(str(root)) / "characters"
    out: dict[str, str] = {}
    if not base.is_dir():
        return out
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        png = d / ANCHOR_NAME
        if not png.is_file():
            continue
        name = d.name.split("@")[0]
        out[name] = str(png)
    return out


def sets_for(characters, anchors) -> dict[str, str]:
    """按出场角色取锚点图。**只返回真的存在的那几个。**"""
    if not anchors:
        return {}
    out: dict[str, str] = {}
    for c in characters or ():
        name = str(c)
        if name in anchors:
            out[name] = anchors[name]
    return out


def summary(anchors) -> dict:
    """给报告用的一句话摘要。"""
    names = sorted((anchors or {}).keys())
    return {"count": len(names), "names": names}
