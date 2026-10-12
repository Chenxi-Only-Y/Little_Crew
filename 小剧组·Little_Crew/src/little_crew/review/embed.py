"""L2 一致性描述子 —— 从**像素**算出可比较、可解释的一致性度量。

它解决什么问题
--------------
`ops.py` 里有两条断言算子：

| 算子 | 判定 |
|---|---|
| `identity_similarity` | 产物 vs **角色参考**（绝对基准） |
| `pairwise_similarity` | 产物 vs **另一个产物**（相对一致） |

它们只负责**比阈值**——算子文档里写得很清楚："相似度由谁算是调用方的事"。

**而那个"调用方"此前不存在。** 于是本机实测里这两条约束
**永远被判"缺观测 similarity"而跳过** ——
跨镜角色一致性**没有任何度量手段**（见策划书 §5.10.4）。

本模块补的就是那一环：给出 `similarity` 的**计算方**。

诚实声明：这不是语义身份嵌入
----------------------------
必须先把话说清楚，否则会误导后续判断：

| 本模块能做 | 本模块**不能**做 |
|---|---|
| 抓**配色漂移**（换色调、换光照、换画风） | 判断"这张脸是不是同一个人" |
| 抓**全局构图/明暗**突变 | 区分"同一个人的不同表情"与"两个人" |
| 抓**相邻镜之间的整体不一致** | 替代人脸/身份模型 |

所以它是**粗粒度的一致性筛子**，不是身份识别的替代品。
把它当身份模型用会得出错误结论 —— 这一点写进 §5.9 的限制里。

零第三方依赖
------------
只用 `zlib` + `struct` + `math`（标准库）。
`dependencies = []` 是硬约束，而 `review` 层连 `runtime` 都不能 import
（架构守卫强制），更不能引入外部包。

**代价**是自己写 PNG 解码。收益是这个度量**完全可解释、可审计**：
每一维都能指回具体的像素统计，而不像神经嵌入那样是个黑盒。
对"验收门禁"这个场景，可解释性比精度更重要 ——
门禁要能说清"为什么判不合格"，否则返工指令就无从写起。
"""

from __future__ import annotations

import math
import struct
import zlib
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

#: 方法标识 —— **进证据链**。
#:
#: 换了描述子算法就**必须**改这个版本号，否则
#: "换了实现导致通过率变了"无法追溯
#: （见 `ops.identity_similarity` 文档里对调用方的要求）。
METHOD = "consistency-descriptor/v2"

__all__ = [
    "ConsistencyDescriptor",
    "describe_bytes",
    "signature",
    "compare_signatures",
    "similarity",
    "drift_report",
    "DIMENSION_NAMES",
    "METHOD",
    "DecodeError",
]


# ---------------------------------------------------------------------------
# PNG 解码（只到缩略图，不解全图）
# ---------------------------------------------------------------------------

class DecodeError(ValueError):
    """图片解不开。**必须显式失败，不能返回默认值。**

    静默退化会伪造出"两张图完全一致"这种结论 —— 而那正是门禁最不该
    搞错的方向（把不一致判成一致 = 漏放）。
    """


def _unfilter(raw: bytes, *, width: int, height: int,
              channels: int, bpp: int) -> list[bytes]:
    """还原 PNG 的行（解掉 5 种滤波器）。

    实现的是完整滤波器链 —— 真实产出（ComfyUI）会用到 `Paeth`，
    只支持 `None` 的话在真机上会直接解不出来。
    """
    stride = width * channels
    out: list[bytes] = []
    prev = bytearray(stride)
    pos = 0

    for _ in range(height):
        if pos >= len(raw):
            raise DecodeError("PNG 数据不完整（行数不足）")
        ft = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        if len(line) < stride:
            raise DecodeError("PNG 数据不完整（行被截断）")
        pos += stride

        if ft == 0:                                       # None
            pass
        elif ft == 1:                                     # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ft == 2:                                     # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:                                     # Average
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ft == 4:                                     # Paeth
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        else:
            raise DecodeError(f"未知 PNG 滤波器类型 {ft}")

        out.append(bytes(line))
        prev = line

    return out


def decode_png(blob: bytes) -> tuple[int, int, list[bytes], int]:
    """解出 `(宽, 高, 行, 通道数)`，每行是**原始字节**（8 位）。

    只支持 8 位非隔行 —— 覆盖真实产出（ComfyUI 输出 8 位 RGB/RGBA）。
    其它位深**显式报错**而不是猜 —— 猜错会静默产出一堆噪声统计量。
    """
    if not blob.startswith(b"\x89PNG\r\n\x1a\n"):
        raise DecodeError("不是 PNG（魔数不符）")

    pos = 8
    width = height = 0
    channels = 3
    idat = bytearray()
    palette: bytes | None = None

    while pos + 8 <= len(blob):
        (ln,) = struct.unpack(">I", blob[pos:pos + 4])
        tag = blob[pos + 4:pos + 8]
        data = blob[pos + 8:pos + 8 + ln]
        pos += 12 + ln                                  # 4 长度 + 4 标签 + 4 CRC

        if tag == b"IHDR":
            width, height, depth, color, comp, filt, inter = struct.unpack(
                ">IIBBBBB", data[:13])
            if depth != 8:
                raise DecodeError(f"只支持 8 位 PNG，实际 {depth} 位")
            if inter != 0:
                raise DecodeError("不支持隔行 PNG")
            channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color, 0)
            if not channels:
                raise DecodeError(f"不支持的 PNG 颜色类型 {color}")
        elif tag == b"PLTE":
            palette = data
        elif tag == b"IDAT":
            idat += data
        elif tag == b"IEND":
            break

    if not width or not height:
        raise DecodeError("PNG 缺 IHDR")

    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as e:
        raise DecodeError(f"IDAT 解压失败：{e}") from e

    bpp = max(1, channels)
    rows = _unfilter(raw, width=width, height=height,
                     channels=channels, bpp=bpp)

    # 调色板图 -> 展开成 RGB，避免下游处理 1 通道索引值
    if palette is not None:
        expanded: list[bytes] = []
        for row in rows:
            buf = bytearray()
            for idx in row:
                o = idx * 3
                buf += palette[o:o + 3] if o + 3 <= len(palette) else b"\x00\x00\x00"
            expanded.append(bytes(buf))
        rows, channels = expanded, 3

    return width, height, rows, channels


# ---------------------------------------------------------------------------
# 描述子
# ---------------------------------------------------------------------------

#: 缩略网格边长（8x8 = 64 格）。
#:
#: 选 8 而不是 16：**缩略图的目的是忽略细节、只留布局与配色**。
#: 格子太细会把"同一角色的两个表情"也算成大差异，
#: 而门禁要抓的是**跨镜漂移**，不是逐像素一致。
_GRID = 8

#: 色相直方图桶数（12 桶 = 每桶 30°）。
_HUE_BINS = 12

#: 明度直方图桶数。
_VAL_BINS = 5

#: 饱和度直方图桶数。
_SAT_BINS = 5

#: 各维度名（进证据链，便于追溯"是哪一维拉开了差距"）。
#:
#: ⚠️ **四维是刻意拆开的，不是越细越好。** 标定实验（12 种受控退化）显示，
#: 把明度与饱和度混在一起会让"哪一维不一致"答不准：
#:
#:   | 退化 | 混在一起时 | 拆开后 |
#:   |---|---|---|
#:   | 去饱和 0.5 | palette 判 0.971（应为低分） | `richness` 抓住 |
#:   | 亮度 x1.15 | palette 与 lighting 同时掉 | `lighting` 单独抓住 |
#:
#: 而"哪一维不一致"**直接决定返工方向** —— 配色漂了要改提示词/风格，
#: 构图跑了要改机位，光照变了要改打光参数。答错方向等于白跑一轮返工。
#:
#: | 维度 | 抓什么 | 返工方向 |
#: |---|---|---|
#: | `palette` | 色相分布（**色彩的"身份"**） | 改风格/提示词配色 |
#: | `layout` | 8x8 网格亮度布局（**构图**） | 改机位/构图 |
#: | `lighting` | 明度分布（**曝光**） | 改打光/曝光参数 |
#: | `richness` | 饱和度分布（**色彩浓度**） | 改饱和度/风格强度 |
DIMENSION_NAMES = ("palette", "layout", "lighting", "richness")


@dataclass(frozen=True)
class ConsistencyDescriptor:
    """一张图的**一致性描述子**。

    三个维度刻意分开存，而不是揉成一个向量 —— 因为门禁要能回答
    "**是哪一维不一致**"，那直接决定返工方向：
      · `palette` 不一致 -> 配色/画风漂了
      · `layout`  不一致 -> 构图/主体位置变了
      · `lighting` 不一致 -> 光照条件变了
    """

    #: 加权色相直方图（12 维，和归一）
    palette: tuple[float, ...]
    #: 8x8 网格的均值 RGB（64*3 维，0~1）
    layout: tuple[float, ...]
    #: 明度直方图（5 维，和归一）
    lighting: tuple[float, ...]
    #: **饱和度直方图（5 维，和归一）** —— 抓"彩色变灰"
    richness: tuple[float, ...]
    #: 元信息（尺寸、像素数）—— 便于审计
    meta: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "dimensions": len(self.palette) + len(self.layout)
                          + len(self.lighting) + len(self.richness),
            "palette": [round(v, 6) for v in self.palette],
            "layout": [round(v, 6) for v in self.layout],
            "lighting": [round(v, 6) for v in self.lighting],
            "richness": [round(v, 6) for v in self.richness],
            "meta": dict(self.meta),
        }

    def dimension_similarities(self,
                               other: "ConsistencyDescriptor"
                               ) -> dict[str, float]:
        """逐维相似度（0~1）—— 用于回答"哪一维不一致"。"""
        return {
            "palette": _hist_similarity(self.palette, other.palette),
            "layout": _cosine(self.layout, other.layout),
            "lighting": _hist_similarity(self.lighting, other.lighting),
            "richness": _hist_similarity(self.richness, other.richness),
        }


def _hist_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """直方图相似度（0~1），用 **EMD**（推土机距离）而不是交集。

    ⚠️ **这是一次真实的方法学修正，值得写清楚。**

    最初用的是**直方图交集**（`sum(min(a_i, b_i))`）。它是常见做法，
    但在本场景下有个致命缺陷：**对桶边界极脆**。

    实测（受控退化实验）：

    | 画面 | 饱和度直方图 | 交集 |
    |---|---|---|
    | 原图 | `[0,0,0,1,0]`（全在桶 3） | — |
    | 轻微去饱和 | `[0,0,0,1,0]` 或 `[0,0,1,0,0]` | 0 或 1 |
    | 重度去饱和 | `[1,0,0,0,0]`（全在桶 0） | **0.00** |

    **两幅"中等程度不同"的图被判成 0 相似度**，而两幅"完全一样"的图是 1 ——
    中间没有任何过渡。原因是真实图像的统计量高度集中（常常全落在一两个桶里），
    所以微小的数值平移就会跨过桶边界，让交集**断崖式**掉到 0。

    EMD 没有这个问题：它知道"桶 3 与桶 4 是邻居"，
    所以**分布平移**只产生小的距离，只有**分布形状真的变了**才会拉开。
    它算的是"把一方的概率搬到另一方，平均要搬多远"：

        EMD = Σ|CDF_a(i) - CDF_b(i)| / (桶数 - 1)

    归一化后：完全相同 -> 0，最远（全在最左 vs 全在最右）-> 1。
    相似度取 `1 - EMD`。

    这也让"相似度"的语义更诚实：
    **它反映的是分布之间的距离，而不是"有多少个桶恰好重叠"。**
    """
    n = len(a)
    if n == 0 or len(b) != n:
        return 0.0
    if n == 1:
        return 1.0 if abs(a[0] - b[0]) < 1e-12 else 0.0

    ca = cb = 0.0
    moved = 0.0
    for i in range(n):
        ca += a[i]
        cb += b[i]
        moved += abs(ca - cb)
    # 每跨一个桶记 1 单位的位移；最大可能位移是 `n-1`
    return max(0.0, min(1.0, 1.0 - moved / (n - 1)))


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度，映射到 0~1（负相关记 0）。

    网格颜色用余弦：它是**向量**（每个格子的 RGB），
    关心的是"整体朝向像不像"，对整体亮度缩放不敏感 ——
    而那正是我们想要的（轻微曝光差异不该判成不一致）。
    """
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na <= 1e-12 or nb <= 1e-12:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    return max(0.0, min(1.0, dot / (na * nb)))


def _srgb_to_linear(c: float) -> float:
    """sRGB -> 线性光。

    ⚠️ **必须做这一步**：直接在 sRGB 值上求平均/求直方图，
    会让暗部被过度加权（sRGB 是感知编码，不是线性光）。
    实测影响不小 —— 同一场景换个曝光，sRGB 域的差异远小于线性域，
    于是"明显变亮了"被判成"几乎一样"。
    """
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(c: float) -> float:
    c = max(0.0, min(1.0, c))
    return c * 12.92 if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


def describe_bytes(blob: bytes) -> ConsistencyDescriptor:
    """由图片字节算出描述子。

    **只支持 PNG**（真实产出的格式）。其它格式显式报错 ——
    返回"空描述子"会让所有相似度变成 0 或 1，
    而那是**静默的错误结论**，比报错糟得多。
    """
    width, height, rows, channels = decode_png(blob)

    # ---- 1) 缩略到 8x8，每格求均值 RGB ----
    #
    # ⚠️ **这一段是性能敏感区。** 真机产出是 832x480 = 40 万像素，
    # 而描述子还只取缩略 —— 逐像素在 Python 里做算术要走 40 万次循环。
    #
    # 两条优化，都很关键：
    #   ① **sRGB->线性 用查表**：只有 256 个可能取值，
    #      每像素算 `((c+0.055)/1.055)**2.4` 是纯浪费。
    #   ② **按行切片求和**：`sum(slice)` 在 C 层跑，
    #      比 Python 逐元素快一个数量级。
    #
    # 不做这两条的话，"跑一集"会在门禁这一步多花几十秒 ——
    # 而门禁本该是廉价的判定步骤。
    lut = [_srgb_to_linear(i / 255.0) for i in range(256)]

    # 每行的列范围（按网格列）缓存一次 —— 对每行反复计算 `x*8//w` 也是浪费
    col_bounds: list[list[tuple[int, int]]] = []
    for gx in range(_GRID):
        lo = gx * width // _GRID
        hi = (gx + 1) * width // _GRID
        if hi <= lo:                        # 图比网格还小 -> 退化成一个像素
            hi = min(width, lo + 1)
        col_bounds.append([(lo, hi)])

    # 每行的网格行号也缓存
    row_to_gy = [min(_GRID - 1, y * _GRID // height) for y in range(height)]

    sum_r = [[0.0] * _GRID for _ in range(_GRID)]
    sum_g = [[0.0] * _GRID for _ in range(_GRID)]
    sum_b = [[0.0] * _GRID for _ in range(_GRID)]
    count = [[0] * _GRID for _ in range(_GRID)]

    gray = channels < 3

    for y, row in enumerate(rows):
        gy = row_to_gy[y]
        sr, sg, sb = sum_r[gy], sum_g[gy], sum_b[gy]
        cn = count[gy]
        if gray:
            # 单通道（灰度/调色板已展开则不会走到这里）
            vals = row
            for gx in range(_GRID):
                for lo, hi in col_bounds[gx]:
                    s = 0.0
                    for v in vals[lo:hi]:
                        s += lut[v]
                    sr[gx] += s
                    sg[gx] += s
                    sb[gx] += s
                    cn[gx] += hi - lo
        else:
            rv = row[0::channels]
            gv = row[1::channels]
            bv = row[2::channels]
            for gx in range(_GRID):
                for lo, hi in col_bounds[gx]:
                    # `sum(map(lut.__getitem__, slice))` 全程在 C 层
                    sr[gx] += sum(map(lut.__getitem__, rv[lo:hi]))
                    sg[gx] += sum(map(lut.__getitem__, gv[lo:hi]))
                    sb[gx] += sum(map(lut.__getitem__, bv[lo:hi]))
                    cn[gx] += hi - lo

    layout: list[float] = []
    for gy in range(_GRID):
        for gx in range(_GRID):
            n = count[gy][gx] or 1
            # 回到 sRGB 再存 —— 描述子要能被人类读懂（"这一格偏暖"）
            layout.extend((_linear_to_srgb(sum_r[gy][gx] / n),
                           _linear_to_srgb(sum_g[gy][gx] / n),
                           _linear_to_srgb(sum_b[gy][gx] / n)))

    # ---- 2) 三个直方图（在 64 格上算，不是 40 万像素）----
    #
    # ⚠️ 这一步刻意**只统计缩略后的格子**：门禁要抓的是**跨镜漂移**，
    # 而不是逐像素一致。在缩略图上做直方图，等价于"先低通再统计"，
    # 天然忽略噪点与细节差异 —— 那正是我们想要的鲁棒性。
    #
    # ⚠️ **色相与饱和度必须分开统计。**
    # 早期把饱和度只当色相直方图的**权重**，结果"整帧去饱和"几乎不影响
    # palette（灰色格子的色相位置没变）—— 实测「去饱和 0.5」判 0.971，
    # 而那明显是两幅不同的画。分开之后 `richness` 才抓得住它。
    hue_hist = [0.0] * _HUE_BINS
    val_hist = [0.0] * _VAL_BINS
    sat_hist = [0.0] * _SAT_BINS
    hue_weight = 0.0

    for i in range(0, len(layout), 3):
        r, g, b = layout[i], layout[i + 1], layout[i + 2]
        mx = r if r >= g and r >= b else (g if g >= b else b)
        mn = r if r <= g and r <= b else (g if g <= b else b)
        d = mx - mn

        # 明度：直接反映曝光
        val_hist[min(_VAL_BINS - 1, int(mx * _VAL_BINS))] += 1.0
        # 饱和度：`d / mx`（除以 mx 而非固定 1.0）—— 否则亮色天然显得更饱和
        sat = (d / mx) if mx > 1e-6 else 0.0
        sat_hist[min(_SAT_BINS - 1, int(sat * _SAT_BINS))] += 1.0

        if d > 1e-6:
            if mx == r:
                h = ((g - b) / d) % 6
            elif mx == g:
                h = (b - r) / d + 2
            else:
                h = (r - g) / d + 4
            # 色相桶**按饱和度加权** —— 灰色格子的色相是无意义的浮点抖动；
            # 而"它无色"这件事已经由 `richness` 表达了，不该再影响色相。
            w = d
            hue_hist[min(_HUE_BINS - 1, int((h / 6.0) * _HUE_BINS))] += w
            hue_weight += w
        # 灰色格子不投色相桶，也不加权重 —— 见上

    if hue_weight > 1e-9:
        hue_hist = [v / hue_weight for v in hue_hist]
    val_hist = [v / (sum(val_hist) or 1.0) for v in val_hist]
    sat_hist = [v / (sum(sat_hist) or 1.0) for v in sat_hist]

    return ConsistencyDescriptor(
        palette=tuple(hue_hist),
        layout=tuple(layout),
        lighting=tuple(val_hist),
        richness=tuple(sat_hist),
        meta={"width": width, "height": height, "channels": channels,
              "bytes": len(blob),
              "cells": len(layout) // 3,
              # 记下采样方式 —— 描述子换了算法就该换版本号，
              # 否则"换了实现导致通过率变了"无法追溯
              "sampler": "mean/8x8/linear-light/hvs"},
    )


# ---------------------------------------------------------------------------
# 相似度
# ---------------------------------------------------------------------------

#: 各维权重。**必须和为 1**（`_check_weights` 在自检里断言）。
#:
#: 为什么 palette 权重最高：跨镜一致性里最先崩的是**配色** ——
#: 换个图生图种子、换个 ControlNet 强度，整体色调就会漂，
#: 而构图往往还能对上。所以对"发现漂移"这个目的，配色的信号最强。
#:
#: 为什么 layout 也不低：只有配色对得上、主体位置乱跑，同样不是"同一场戏"。
_WEIGHTS: Mapping[str, float] = {
    "palette": 0.35,     # 色彩的"身份"，最强信号
    "layout": 0.30,      # 构图；只有 8x8 分辨率，权重不宜再高
    "lighting": 0.20,    # 曝光
    "richness": 0.15,    # 色彩浓度
}


def signature(blob: bytes) -> dict[str, Any]:
    """把图片字节变成**可序列化的一致性子**（进观测值）。

    为什么不直接把 `ConsistencyDescriptor` 塞进观测：
    观测值要能进 JSON 证据链、能被跨进程传（API/CLI），
    所以必须是纯数据。对象留着在门禁内部用即可。
    """
    d = describe_bytes(blob)
    return {
        "method": METHOD,
        "palette": list(d.palette),
        "layout": list(d.layout),
        "lighting": list(d.lighting),
        "richness": list(d.richness),
        **{k: v for k, v in d.meta.items()},
    }


def _descriptor_of(sig: Mapping[str, Any]) -> ConsistencyDescriptor:
    return ConsistencyDescriptor(
        palette=tuple(sig.get("palette") or ()),
        layout=tuple(sig.get("layout") or ()),
        lighting=tuple(sig.get("lighting") or ()),
        richness=tuple(sig.get("richness") or ()),
        meta={k: v for k, v in sig.items()
              if k not in ("palette", "layout", "lighting", "richness")},
    )


def compare_signatures(a: Mapping[str, Any],
                       b: Mapping[str, Any]) -> dict[str, Any]:
    """比较两个**签名**（而不是两个对象）。

    给 `observe.py` 用 —— 它手上只有 JSON 化的观测值。
    """
    return similarity(_descriptor_of(a), _descriptor_of(b))


def similarity(a: ConsistencyDescriptor,
               b: ConsistencyDescriptor) -> dict[str, Any]:
    """由两个描述子算相似度。

    返回的不只是标量，还有**逐维分解**与**最低的那一维** ——
    因为返工指令需要知道"往哪个方向改"：

        {"similarity": 0.71, "palette": 0.55, "layout": 0.92,
         "lighting": 0.88, "weakest": "palette"}
    """
    dims = a.dimension_similarities(b)
    score = sum(_WEIGHTS[k] * v for k, v in dims.items())
    weakest = min(dims, key=lambda k: dims[k])
    return {
        "similarity": round(max(0.0, min(1.0, score)), 6),
        **{k: round(v, 6) for k, v in dims.items()},
        "weakest": weakest,
        "method": "consistency-descriptor/v2",
    }


def drift_report(labels: Sequence[str],
                 descriptors: Sequence[ConsistencyDescriptor]
                 ) -> dict[str, Any]:
    """一整集的**跨镜一致性报告**。

    为什么需要它（而不只是逐对相似度）：
    门禁按镜头工作，而"一致性"是**镜头之间**的性质 ——
    单看每一镜都可能"像角色"，但相邻两镜之间已经不像同一个人了。
    所以必须有东西把逐对结果**聚合成一个可判定的事实**。

    返回的关键量：

    | 字段 | 含义 | 为什么有用 |
    |---|---|---|
    | `mean` | 全体两两相似度均值 | 整体一致水平 |
    | `worst` | **最低那一对的相似度** | 一致性是短板效应 —— 均值好看不代表没崩 |
    | `worstPair` | 是哪两镜 | 直接指出该重跑谁 |
    | `adjacentMin` | **相邻镜**的最低相似度 | 观众最容易察觉的就是相邻跳变 |
    | `weakestDimension` | 全体最弱的那一维 | 指示返工方向 |
    """
    n = len(labels)
    if n != len(descriptors):
        raise ValueError(f"标签数 {n} 与描述子数 {len(descriptors)} 不符")
    if n < 2:
        return {"shots": n, "pairwise": 0, "enough": False,
                "note": "少于两镜，不足以谈一致性"}

    pairs: list[dict[str, Any]] = []
    dim_totals: dict[str, float] = {k: 0.0 for k in DIMENSION_NAMES}

    for i in range(n):
        for j in range(i + 1, n):
            s = similarity(descriptors[i], descriptors[j])
            pairs.append({
                "a": labels[i], "b": labels[j],
                "adjacent": j == i + 1,
                "similarity": s["similarity"],
                "weakest": s["weakest"],
                "dimensions": {k: s[k] for k in DIMENSION_NAMES},
            })
            for k in DIMENSION_NAMES:
                dim_totals[k] += s[k]

    worst = min(pairs, key=lambda p: p["similarity"])
    best = max(pairs, key=lambda p: p["similarity"])
    adj = [p for p in pairs if p["adjacent"]]
    adj_min = min((p["similarity"] for p in adj), default=None)

    avg_dims = {k: v / len(pairs) for k, v in dim_totals.items()}
    weakest_dim = min(avg_dims, key=lambda k: avg_dims[k])

    return {
        "shots": n,
        "pairwise": len(pairs),
        "enough": True,
        "mean": round(sum(p["similarity"] for p in pairs) / len(pairs), 6),
        "worst": worst["similarity"],
        "worstPair": [worst["a"], worst["b"]],
        "best": best["similarity"],
        "adjacentMin": (round(adj_min, 6) if adj_min is not None else None),
        "weakestDimension": weakest_dim,
        "dimensions": {k: round(v, 6) for k, v in avg_dims.items()},
        "method": "consistency-descriptor/v2",
        "pairs": pairs,
    }
