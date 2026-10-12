"""L2 一致性描述子的标定实验（可复现）。

用法
----
    python measure_consistency.py <图片目录或文件...>

它做两件事：

  ① **受控退化标定** —— 拿一张真实产出当基准，施加 11 种确定性退化
     （色相旋转 / 亮度 / 去饱和 / 构图平移 / 局部崩坏），
     量出描述子对每种退化的响应。

     目的不是"证明它准"，而是**说清它的能力边界**：
     它能抓住什么、抓不住什么、归因是否准确。

  ② **真实批次分布** —— 对给定目录下所有 PNG 两两比较，
     给出分位数，用于**判断阈值定在哪里才站得住**。

为什么证据要留档
----------------
这张表是"为什么 `identity_similarity` 只做 `audit` 而不是 `hard`"
的**唯一依据**。没有它，那个决定看起来像是偷懒；
有了它，它是一个**有数据的结论**。
"""

from __future__ import annotations

import colorsys
import pathlib
import struct
import sys
import zlib

# 本文件在 `research/<主题>/` 下 —— 项目根是 `parents[2]`。
# ⚠️ 别数错：`research/` 与 `src/` 是**同级**的，所以包在
# `<根>/src/little_crew`，而根往上还要两级。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

from little_crew.review.embed import (DIMENSION_NAMES, decode_png,  # noqa: E402
                                      describe_bytes, drift_report,
                                      similarity)


# ---------------------------------------------------------------------------
# 极小编码器（只为造退化图）
# ---------------------------------------------------------------------------

def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _encode_rgb(w: int, h: int, rows: list[bytearray]) -> bytes:
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw, 6))
            + _chunk(b"IEND", b""))


def _to_rows(blob: bytes) -> tuple[int, int, list[bytearray]]:
    w, h, rows, ch = decode_png(blob)
    out: list[bytearray] = []
    for r in rows:
        if ch == 3:
            out.append(bytearray(r))
        elif ch == 4:
            out.append(bytearray(v for i, v in enumerate(r) if i % 4 != 3))
        else:
            b = bytearray()
            for v in r:
                b += bytes((v, v, v))
            out.append(b)
    return w, h, out


# ---------------------------------------------------------------------------
# 退化算子（全部确定性）
# ---------------------------------------------------------------------------

def hue_shift(rows: list[bytearray], deg: float) -> list[bytearray]:
    out = []
    for r in rows:
        b = bytearray()
        for x in range(0, len(r), 3):
            rr, gg, bb = r[x] / 255, r[x + 1] / 255, r[x + 2] / 255
            h, l, s = colorsys.rgb_to_hls(rr, gg, bb)
            rr, gg, bb = colorsys.hls_to_rgb((h + deg / 360.0) % 1.0, l, s)
            b += bytes((int(rr * 255), int(gg * 255), int(bb * 255)))
        out.append(b)
    return out


def brightness(rows: list[bytearray], factor: float) -> list[bytearray]:
    lut = [min(255, int(i * factor)) for i in range(256)]
    return [bytearray(lut[v] for v in r) for r in rows]


def desaturate(rows: list[bytearray], amount: float) -> list[bytearray]:
    out = []
    for r in rows:
        b = bytearray()
        for x in range(0, len(r), 3):
            g = (r[x] * 299 + r[x + 1] * 587 + r[x + 2] * 114) // 1000
            b += bytes((int(r[x] + (g - r[x]) * amount),
                        int(r[x + 1] + (g - r[x + 1]) * amount),
                        int(r[x + 2] + (g - r[x + 2]) * amount)))
        out.append(b)
    return out


def block_corrupt(rows: list[bytearray], frac: float) -> list[bytearray]:
    h, w = len(rows), len(rows[0]) // 3
    y0, x0 = int(h * (1 - frac)), int(w * (1 - frac))
    out = []
    for y, r in enumerate(rows):
        b = bytearray(r)
        if y >= y0:
            for x in range(x0, w):
                b[x * 3:x * 3 + 3] = bytes((255, 0, 255))
        out.append(b)
    return out


def shift_h(rows: list[bytearray], frac: float) -> list[bytearray]:
    w = len(rows[0]) // 3
    d = int(w * frac)
    return [bytearray(r[d * 3:] + r[:d * 3]) for r in rows]


VARIANTS = [
    ("基准", lambda r: r),
    ("色相 +10°", lambda r: hue_shift(r, 10)),
    ("色相 +25°", lambda r: hue_shift(r, 25)),
    ("色相 +60°", lambda r: hue_shift(r, 60)),
    ("亮度 x1.15", lambda r: brightness(r, 1.15)),
    ("亮度 x1.40", lambda r: brightness(r, 1.40)),
    ("去饱和 0.5", lambda r: desaturate(r, 0.5)),
    ("去饱和 0.9", lambda r: desaturate(r, 0.9)),
    ("构图移 5%", lambda r: shift_h(r, 0.05)),
    ("构图移 15%", lambda r: shift_h(r, 0.15)),
    ("局部崩坏 10%", lambda r: block_corrupt(r, 0.10)),
    ("局部崩坏 30%", lambda r: block_corrupt(r, 0.30)),
]


def calibrate(base_png: pathlib.Path, out_dir: pathlib.Path) -> None:
    blob = base_png.read_bytes()
    w, h, rows = _to_rows(blob)
    d0 = describe_bytes(blob)

    print(f"基准：{base_png.name}  {w}x{h}")
    print()
    print(f"  {'退化':16} {'总相似':>8} {'配色':>7} {'构图':>7} "
          f"{'光照':>7} {'浓度':>7}  最弱")
    print("  " + "-" * 74)

    out_dir.mkdir(parents=True, exist_ok=True)
    for name, fn in VARIANTS:
        rws = fn(rows)
        p = out_dir / f"{name.replace(' ', '_').replace('%', 'pct')}.png"
        p.write_bytes(_encode_rgb(w, h, rws))
        s = similarity(d0, describe_bytes(p.read_bytes()))
        dims = "  ".join(f"{s[k]:7.3f}" for k in DIMENSION_NAMES)
        print(f"  {name:16} {s['similarity']:8.4f} {dims}  {s['weakest']}")


def distribution(root: pathlib.Path) -> None:
    pngs = sorted(root.rglob("*.png"))
    if not pngs:
        print(f"  {root} 下没有 PNG")
        return
    descs = [describe_bytes(p.read_bytes()) for p in pngs]
    labels = [p.stem[:22] for p in pngs]
    rep = drift_report(labels, descs)

    vals = sorted(p["similarity"] for p in rep["pairs"])
    print(f"  {len(pngs)} 张图，{rep['pairwise']} 对")
    print(f"    均值      {rep['mean']:.4f}")
    print(f"    最低      {rep['worst']:.4f}  {rep['worstPair']}")
    print(f"    相邻最低  {rep['adjacentMin']}")
    print(f"    最弱维度  {rep['weakestDimension']}")
    print(f"    各维均值  "
          + "  ".join(f"{k}={v:.3f}" for k, v in rep["dimensions"].items()))
    print("    分位      "
          + "  ".join(
              f"p{int(q * 100)}={vals[min(len(vals) - 1, int(q * (len(vals) - 1)))]:.4f}"
              for q in (0.05, 0.25, 0.5, 0.75, 0.95)))


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    targets = [pathlib.Path(a) for a in argv]
    pngs = []
    for t in targets:
        pngs.extend(sorted(t.rglob("*.png")) if t.is_dir() else [t])
    if not pngs:
        print("没找到 PNG")
        return 1

    print("=" * 78)
    print("① 受控退化标定")
    print("=" * 78)
    calibrate(pngs[0], pathlib.Path(__file__).parent / "degradations")

    print()
    print("=" * 78)
    print("② 真实批次分布")
    print("=" * 78)
    for t in targets:
        if t.is_dir():
            print(f"\n  [{t}]")
            distribution(t)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
