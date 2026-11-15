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


# ==========================================================================
# 造型版本化（2026-11-15）：角色可以有多套服装，资产按版本存
# ==========================================================================
#
# 目录约定：
#   assets/characters/<角色>/outfits/<variant>/anchor.png|sheet.png
#   assets/characters/<角色>/outfits/<variant>/outfit.json  ({"appearance": "..."})
#
# **换装是角色设计的事**：这里只负责"发现资产 + 按镜选版本"，
# 不生成、不判断审美。

OUTFIT_DIR = "outfits"
_OUTFIT_IMAGE_NAMES = ("anchor.png", "sheet_refined.png", "sheet.png")


def discover_outfits(root) -> dict[str, dict[str, dict]]:
    """扫描 `<root>/characters/*/outfits/*/`。

    返回 `{角色: {variant: {"image": 路径, "appearance": str, "name": str}}}`。
    没有 `outfits/` 目录就返回空 —— **不编造版本**。
    """
    import json

    base = Path(str(root)) / "characters"
    out: dict[str, dict[str, dict]] = {}
    if not base.is_dir():
        return out
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        odir = d / OUTFIT_DIR
        if not odir.is_dir():
            continue
        name = d.name.split("@")[0]
        variants: dict[str, dict] = {}
        for vd in sorted(odir.iterdir()):
            if not vd.is_dir():
                continue
            img = next((vd / n for n in _OUTFIT_IMAGE_NAMES
                        if (vd / n).is_file()), None)
            if img is None:
                continue
            appearance, label = "", vd.name
            try:
                meta = json.loads((vd / "outfit.json").read_text(encoding="utf-8"))
                if isinstance(meta, dict):
                    appearance = str(meta.get("appearance") or "")
                    label = str(meta.get("name") or vd.name)
            except Exception:                                 # noqa: BLE001
                appearance = ""
            if not appearance:
                # 没有外观描述 = 没有设计，仍可用图但会在证据里留空
                appearance = ""
            variants[vd.name] = {"image": str(img), "appearance": appearance,
                                 "name": label}
        if variants:
            out[name] = variants
    return out


def outfits_for(characters, outfits, variant: str) -> dict[str, dict]:
    """按出场角色取出指定版本的造型；**只返回真的存在的那几个**。"""
    if not outfits or not variant:
        return {}
    got: dict[str, dict] = {}
    for c in characters or ():
        name = str(c)
        ent = (outfits.get(name) or {}).get(str(variant))
        if isinstance(ent, dict) and ent.get("image"):
            got[name] = dict(ent)
    return got


def summary(anchors) -> dict:
    """给报告用的一句话摘要。"""
    names = sorted((anchors or {}).keys())
    return {"count": len(names), "names": names}
