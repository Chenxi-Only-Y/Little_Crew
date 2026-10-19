"""参考视频的**规格解析与校核** —— 按目标视频模型的契约算出"刚好过线"的参数。

## 为什么不在代码里写死 864×486

白模视频最终要作为 `video_ref` 喂给视频模型（Seedance / MiniMax / Wan Animate）。
每家都有自己的硬约束，而且**会变**。写死一个分辨率会有两个后果：

1. 换一家就要改代码
2. 平台改了下限，**我们不会知道** —— 静默不合规，等 API 报错才发现

所以：**契约声明在一张表里，分辨率由约束算出来。**

## 一条来自实践的原则

> 白模视频**无需追求精细度** —— 多余的动作、纹理细节会导致生成内容人物僵硬，
> 还会造成 token 浪费。

参考视频的分辨率与时长**直接换 token**。所以目标是「**刚好过线**」，
不是"尽量清晰" —— 超出的每一个像素都是在花钱买"更僵硬的成片"。

## 三个容易漏的点

| # | 坑 | 这里怎么处理 |
|---|---|---|
| 1 | **h264 yuv420p 要求偶数边长** | 解析器强制偶数，否则编码器直接失败 |
| 2 | **时长应跟随该镜**，不是统一值 | `clamp` 到该模型的区间，不换成一个固定值 |
| 3 | **体积上限**（MiniMax 只有 50MB） | 体积**算不出来**（取决于画面复杂度）→ 只能渲完实测，见 `check_video` |
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "LIMITS",
    "Profile",
    "VideoRefLimit",
    "check_video",
    "resolve_profile",
    "violations",
]

#: h264 的 yuv420p 要求偶数边长 —— 奇数会直接编码失败，不是画质问题。
EVEN_SIDE_REQUIRED = True

#: **我们自己的实用下限**（不是平台契约）。
#:
#: 2026-10-19 实测发现：MiniMax / Wan Animate 对参考视频**没有像素下限**，
#: 于是「刚好过线」退化成最小边长 200 —— 算出 354×200、70,800 像素。
#: 数学上合规，**但那个尺寸下人只有几十个像素，动作读不出来**。
#:
#: 所以补一条：短边不低于 480。**这条写在这里，而不是塞进各家的表里** ——
#: 因为它不是任何一家的契约，是我们的判断。
PRACTICAL_MIN_SHORT_SIDE = 480


@dataclass(frozen=True)
class VideoRefLimit:
    """一个视频模型对**参考视频**的契约。值来自各自一手文档。"""
    model: str
    min_seconds: float
    max_seconds: float
    min_fps: int
    max_fps: int
    min_pixels: int = 0
    max_pixels: int = 0
    min_side: int = 200
    max_side: int = 4096
    max_mb: float = 0.0
    min_aspect: float = 0.0
    max_aspect: float = 0.0
    notes: str = ""

    def __post_init__(self) -> None:
        if self.min_seconds <= 0 or self.max_seconds < self.min_seconds:
            raise ValueError(f"{self.model}: 时长区间非法")
        if self.min_fps <= 0 or self.max_fps < self.min_fps:
            raise ValueError(f"{self.model}: 帧率区间非法")


#: 三家契约。**加一家只需要加一条** —— 这是这张表存在的意义。
LIMITS: dict[str, VideoRefLimit] = {
    "seedance-2.0": VideoRefLimit(
        model="seedance-2.0", min_seconds=2.0, max_seconds=15.0,
        min_fps=24, max_fps=60,
        # 火山方舟：参考视频**总像素**下限 407,696、上限 8,295,044
        min_pixels=407_696, max_pixels=8_295_044,
        max_side=2048, max_mb=200.0,
        notes="火山方舟 Seedance 2.0；最多 3 个参考视频；真人人脸受限（有 -face 变体）",
    ),
    "minimax-h3": VideoRefLimit(
        model="minimax-h3", min_seconds=2.0, max_seconds=15.0,
        min_fps=1, max_fps=60,
        max_side=2048, max_mb=50.0,          # ← 三家最紧的一条
        min_aspect=0.4, max_aspect=2.5,
        notes="MiniMax 视频 v2；role=reference_video；H.264/H.265",
    ),
    "wan-animate-2.2": VideoRefLimit(
        model="wan-animate-2.2", min_seconds=2.0, max_seconds=30.0,
        min_fps=1, max_fps=60,
        min_side=200, max_side=2048, max_mb=200.0,
        min_aspect=1 / 3, max_aspect=3.0,
        notes="阿里云 wan2.2-animate-move；另需 image_url；"
              "要求角色在图与视频中占画面比例相近",
    ),
}


@dataclass(frozen=True)
class Profile:
    """算出来的**可执行规格**。"""
    width: int
    height: int
    fps: int
    min_seconds: float
    max_seconds: float
    max_mb: float
    models: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def aspect(self) -> float:
        return self.width / self.height

    def clamp_seconds(self, wanted: float) -> float:
        """**跟随该镜时长**，只做区间夹取 —— 不换成一个固定值。

        统一成 5 秒会丢掉"这镜本来就短/长"的信息，而时长直接换 token。
        """
        return max(self.min_seconds, min(self.max_seconds, float(wanted)))

    def as_dict(self) -> dict[str, Any]:
        return {"width": self.width, "height": self.height, "fps": self.fps,
                "pixels": self.pixels, "aspect": round(self.aspect, 4),
                "minSeconds": self.min_seconds,
                "maxSeconds": self.max_seconds,
                "maxMB": self.max_mb, "models": list(self.models),
                "notes": list(self.notes)}


def _merge(models: Sequence[VideoRefLimit]) -> dict[str, Any]:
    """取交集。空缺的约束（0）不参与收紧。"""
    def _tight(getter, pick):
        vals = [getter(m) for m in models if getter(m)]
        return pick(vals) if vals else 0

    return {
        "min_seconds": max(m.min_seconds for m in models),
        "max_seconds": min(m.max_seconds for m in models),
        "min_fps": max(m.min_fps for m in models),
        "max_fps": min(m.max_fps for m in models),
        "min_pixels": _tight(lambda m: m.min_pixels, max),
        "max_pixels": _tight(lambda m: m.max_pixels, min),
        "min_side": max(m.min_side for m in models),
        "max_side": min(m.max_side for m in models),
        "max_mb": _tight(lambda m: m.max_mb, min),
        "min_aspect": _tight(lambda m: m.min_aspect, max),
        "max_aspect": _tight(lambda m: m.max_aspect, min),
    }


def resolve_profile(models: Iterable[str], *, aspect: float = 16 / 9,
                    fps: int = 24) -> Profile:
    """按目标模型**算出刚好过线**的分辨率。

    做法是**从最小可能边长往上搜第一个合法偶数解** ——
    不是为了精确，是为了**可解释**：报出来的每个数字都能追回到某条约束。
    """
    names = tuple(models)
    unknown = [n for n in names if n not in LIMITS]
    if unknown:
        raise KeyError(f"未登记的参考视频模型: {unknown}；已登记 {sorted(LIMITS)}")
    ms = [LIMITS[n] for n in names]
    if not ms:
        raise ValueError("至少要给一个目标模型")
    c = _merge(ms)

    if not (c["min_seconds"] <= c["max_seconds"]):
        raise ValueError(f"时长区间无交集：{names}")
    if not (c["min_fps"] <= c["max_fps"]):
        raise ValueError(f"帧率区间无交集：{names}")
    if c["min_aspect"] and c["max_aspect"] and c["min_aspect"] > c["max_aspect"]:
        raise ValueError(f"比例区间无交集：{names}")
    if not (c["min_aspect"] <= aspect <= c["max_aspect"] or not c["min_aspect"]):
        raise ValueError(
            f"目标比例 {aspect:.3f} 不在交集 "
            f"[{c['min_aspect']:.3f}, {c['max_aspect']:.3f}] 内")

    lo = max(c["min_side"], PRACTICAL_MIN_SHORT_SIDE, 64)
    hi = min(c["max_side"], 4096)
    step = 2 if EVEN_SIDE_REQUIRED else 1
    for w in range(lo + (lo % step), hi + 1, step):
        h = int(round(w / aspect))
        if h % step:
            h += 1
        if h < max(c["min_side"], PRACTICAL_MIN_SHORT_SIDE) or h > c["max_side"]:
            continue
        px = w * h
        if c["min_pixels"] and px < c["min_pixels"]:
            continue
        if c["max_pixels"] and px > c["max_pixels"]:
            break                       # 再大只会更超
        if c["min_aspect"] and not (c["min_aspect"] <= w / h <= c["max_aspect"]):
            continue
        return Profile(width=w, height=h,
                       fps=max(c["min_fps"], min(c["max_fps"], fps)),
                       min_seconds=c["min_seconds"],
                       max_seconds=c["max_seconds"],
                       max_mb=float(c["max_mb"]),
                       models=names,
                       notes=tuple(m.notes for m in ms if m.notes))
    raise ValueError(
        f"在比例 {aspect:.3f} 下找不到满足 {names} 的分辨率"
        f"（边长 {lo}~{hi}，像素下限 {c['min_pixels']}）")


def violations(meta: Mapping[str, Any], models: Iterable[str]) -> list[str]:
    """校核一条**已渲好**的参考视频。返回问题列表（空 = 合规）。

    `meta` 至少要有 `width` / `height` / `fps` / `seconds` / `bytes`。
    体积是**唯一算不出来**的一项（取决于画面复杂度），所以只能实测。
    """
    names = tuple(models)
    ms = [LIMITS[n] for n in names]
    if not ms:
        return ["没有目标模型 —— 无法校核（缺观测不等于通过）"]
    c = _merge(ms)
    out: list[str] = []
    w = int(meta.get("width") or 0)
    h = int(meta.get("height") or 0)
    fps = int(meta.get("fps") or 0)
    sec = float(meta.get("seconds") or 0.0)
    nbytes = int(meta.get("bytes") or 0)

    if not (w and h):
        out.append("缺分辨率 —— 无法校核")
    else:
        if EVEN_SIDE_REQUIRED and (w % 2 or h % 2):
            out.append(f"边长必须是偶数（h264 yuv420p）：{w}×{h}")
        if c["min_pixels"] and w * h < c["min_pixels"]:
            out.append(f"总像素 {w*h} < 下限 {c['min_pixels']}（差 "
                       f"{c['min_pixels'] - w*h}）")
        if c["max_pixels"] and w * h > c["max_pixels"]:
            out.append(f"总像素 {w*h} > 上限 {c['max_pixels']}")
        if min(w, h) < max(c["min_side"], PRACTICAL_MIN_SHORT_SIDE):
            out.append(f"短边 {min(w, h)} < 实用下限 {PRACTICAL_MIN_SHORT_SIDE}"
                       f"（平台没这条，但太小的白模读不出动作）")
        elif max(w, h) > c["max_side"]:
            out.append(f"边长越界：{w}×{h}（允许 "
                       f"{c['min_side']}~{c['max_side']}）")
        if c["min_aspect"] and not (c["min_aspect"] <= w / h <= c["max_aspect"]):
            out.append(f"比例 {w/h:.3f} 越界 "
                       f"[{c['min_aspect']:.3f}, {c['max_aspect']:.3f}]")
    if fps and not (c["min_fps"] <= fps <= c["max_fps"]):
        out.append(f"帧率 {fps} 越界 [{c['min_fps']}, {c['max_fps']}]")
    if sec and not (c["min_seconds"] <= sec <= c["max_seconds"]):
        out.append(f"时长 {sec:.2f}s 越界 "
                   f"[{c['min_seconds']}, {c['max_seconds']}]")
    if c["max_mb"] and nbytes:
        mb = nbytes / 1024 / 1024
        if mb > c["max_mb"]:
            out.append(f"体积 {mb:.1f}MB > 上限 {c['max_mb']}MB")
    return out


def check_video(meta: Mapping[str, Any], models: Iterable[str]) -> tuple[bool, list[str]]:
    """`violations` 的布尔包装，便于调用方直接判。"""
    probs = violations(meta, models)
    return (not probs), probs
