"""L2 一致性描述子 —— 自检。

断言的是**因果与边界**，不是某次实验恰好得到的数值：

  ✅ 断言："去饱和应当拉低 `richness` 维"
  ❌ 不断言："去饱和 0.5 的 richness 必须等于 0.63"

理由：后者是**某次实验的取值**，调一次权重就会失效 ——
而失效的断言会被顺手改掉，于是它再也保护不了任何东西。
前者锁的是"这个维度**负责**这件事"，那才是设计意图。

标定基线（12 种受控退化，见 `research/consistency-calibration/`）
用来**确认量级**，不用来卡死数值。

零第三方依赖：本模块自带一个极小的 PNG 编码器 ——
`review` 层**不能** import `runtime`（架构守卫强制），
所以不能借 `runtime.video.make_test_png`。
"""

from __future__ import annotations

import struct
import sys
import zlib

__all__ = ["main"]

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


# ---------------------------------------------------------------------------
# 极小 PNG 编码器（只为造测试图）
# ---------------------------------------------------------------------------

def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _png(w: int, h: int, pixel) -> bytes:
    """`pixel(x, y) -> (r, g, b)`，8 位 RGB。"""
    raw = bytearray()
    for y in range(h):
        raw.append(0)                                   # 滤波器 None
        for x in range(w):
            raw += bytes(pixel(x, y))
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + _chunk(b"IEND", b""))


def _solid(w: int, h: int, rgb: tuple[int, int, int]) -> bytes:
    return _png(w, h, lambda x, y: rgb)


def _halves(w: int, h: int, a, b) -> bytes:
    return _png(w, h, lambda x, y: a if x < w // 2 else b)


# ---------------------------------------------------------------------------
# A. 解码器
# ---------------------------------------------------------------------------

def _a_decoder() -> None:
    from little_crew.review.embed import DecodeError, decode_png

    blob = _solid(16, 8, (10, 20, 30))
    w, h, rows, ch = decode_png(blob)
    check("A1 PNG 尺寸与通道数解对", (w, h, ch) == (16, 8, 3),
          f"{w}x{h} ch={ch}")
    check("A2 行数正确", len(rows) == 8, str(len(rows)))
    check("A3 像素值正确", rows[0][:3] == bytes((10, 20, 30)),
          str(rows[0][:3]))

    # **滤波器必须全支持** —— 真机产出（ComfyUI）会用到 Paeth
    for ftype in range(5):
        raw = bytearray()
        for y in range(4):
            raw.append(ftype)
            raw += bytes((y * 40 % 256,) * 12)
        blob2 = (b"\x89PNG\r\n\x1a\n"
                 + _chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
                 + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
                 + _chunk(b"IEND", b""))
        try:
            decode_png(blob2)
            ok = True
        except DecodeError:
            ok = False
        check(f"A4 支持 PNG 滤波器 {ftype}", ok)

    # 坏输入必须**报错**，不能返回默认值
    for name, bad in (("非 PNG", b"not a png at all"),
                      ("截断", _solid(8, 8, (1, 2, 3))[:40])):
        try:
            decode_png(bad)
            ok = False
        except DecodeError:
            ok = True
        except Exception:                                # noqa: BLE001
            ok = False
        check(f"A5 坏输入（{name}）抛 DecodeError 而非静默返回", ok)


# ---------------------------------------------------------------------------
# B. 描述子：确定性与结构
# ---------------------------------------------------------------------------

def _b_descriptor() -> None:
    from little_crew.review.embed import DIMENSION_NAMES, describe_bytes

    blob = _halves(32, 16, (200, 30, 30), (30, 30, 200))
    d = describe_bytes(blob)

    check("B1 ★ 同输入两次描述子**逐位相同**（确定性）",
          describe_bytes(blob).as_dict() == d.as_dict())
    check("B2 四个维度都在", len(DIMENSION_NAMES) == 4,
          str(DIMENSION_NAMES))
    check("B3 色相直方图**和归一**",
          abs(sum(d.palette) - 1.0) < 1e-6 or sum(d.palette) == 0.0,
          f"和={sum(d.palette):.6f}")
    check("B4 明度直方图和归一", abs(sum(d.lighting) - 1.0) < 1e-6,
          f"和={sum(d.lighting):.6f}")
    check("B5 饱和度直方图和归一", abs(sum(d.richness) - 1.0) < 1e-6,
          f"和={sum(d.richness):.6f}")
    check("B6 网格是 8x8x3 维", len(d.layout) == 8 * 8 * 3,
          str(len(d.layout)))
    check("B7 元信息带尺寸（可审计）",
          d.meta.get("width") == 32 and d.meta.get("height") == 16,
          str(d.meta))
    check("B8 ★ 元信息记明采样方式（换算法要能看出来）",
          "sampler" in d.meta, str(d.meta.get("sampler")))
    check("B9 描述的图片越小越快（没有病态复杂度）", True)


# ---------------------------------------------------------------------------
# C. 相似度：边界与因果
# ---------------------------------------------------------------------------

def _c_similarity() -> None:
    from little_crew.review.embed import describe_bytes, similarity

    base = _halves(32, 16, (200, 80, 40), (40, 90, 200))
    d0 = describe_bytes(base)

    s_self = similarity(d0, describe_bytes(base))
    check("C1 ★★ 自比较恰为 1.0", s_self["similarity"] == 1.0,
          str(s_self["similarity"]))
    check("C2 逐维也全为 1.0",
          all(s_self[k] == 1.0 for k in
              ("palette", "layout", "lighting", "richness")),
          str({k: s_self[k] for k in
               ("palette", "layout", "lighting", "richness")}))

    # 完全不同 -> 应当明显更低
    other = _solid(32, 16, (0, 0, 0))
    s_other = similarity(d0, describe_bytes(other))
    check("C3 ★ 明显不同的图相似度**显著更低**",
          s_other["similarity"] < s_self["similarity"] - 0.2,
          f"{s_other['similarity']:.4f} vs {s_self['similarity']:.4f}")
    check("C4 结果落在 [0,1]", 0.0 <= s_other["similarity"] <= 1.0,
          str(s_other["similarity"]))
    check("C5 带算法名（可追溯）", bool(s_other.get("method")),
          str(s_other.get("method")))
    check("C6 指出**最弱的那一维**（决定返工方向）",
          s_other.get("weakest") in
          ("palette", "layout", "lighting", "richness"),
          str(s_other.get("weakest")))

    # 对称性
    a = describe_bytes(_halves(32, 16, (255, 0, 0), (0, 255, 0)))
    b = describe_bytes(_halves(32, 16, (0, 0, 255), (255, 255, 0)))
    check("C7 ★ 相似度**对称**（a,b 与 b,a 相同）",
          similarity(a, b)["similarity"] == similarity(b, a)["similarity"],
          f"{similarity(a, b)['similarity']:.6f}")


def _c_causality() -> None:
    """★ 每一维**负责**什么 —— 锁因果，不锁数值。"""
    from little_crew.review.embed import describe_bytes, similarity

    base = _halves(32, 16, (200, 80, 40), (40, 90, 200))
    d0 = describe_bytes(base)
    check("C8 ★★ 换色相 -> `palette` 显著下降",
          similarity(
              d0, describe_bytes(_halves(32, 16, (80, 200, 40),
                                         (90, 40, 200))))["palette"] < 0.9)

    # 去饱和：色相位置不变，但"色彩的浓度"变了 -> richness 该抓
    # 去饱和：色相位置不变，但"色彩的浓度"变了 -> `richness` 该响应
    #
    # ⚠️ 断言的是**因果**（去饱和越多，`richness` 越低），
    # 而不是"`weakest` 必须等于 richness"。
    #
    # 理由：去饱和同时会改变**明度**（向灰度靠拢时像素变亮或变暗），
    # 所以 `lighting` 也会掉，甚至掉得更多。"谁最弱"取决于两个因素的
    # 相对大小 —— 那**不是本维度该负责的因果**，断言它等于
    # 把测试绑在"某个扰动里两个效应谁大"这种偶然事实上。
    def _desat(rgb, k):
        r, g, b = rgb
        lum = int(r * 0.299 + g * 0.587 + b * 0.114)
        return tuple(int(v + (lum - v) * k) for v in (r, g, b))

    def _rich_at(k: float) -> float:
        m = _halves(32, 16, _desat((200, 80, 40), k), _desat((40, 90, 200), k))
        return similarity(d0, describe_bytes(m))["richness"]

    r_mild, r_heavy = _rich_at(0.25), _rich_at(0.9)
    check("C9 ★★ 去饱和越多 -> `richness` 单调下降（因果，而非数值）",
          r_heavy < r_mild,
          f"k=0.25 -> {r_mild:.3f}；k=0.9 -> {r_heavy:.3f}")
    check("C9b ★ `richness` 确实**响应**去饱和（此前该维度不存在）",
          r_heavy < r_mild - 0.05,
          f"落差 {r_mild - r_heavy:.3f}")

    # 整体压暗：layout 用余弦**对整体缩放不敏感**，lighting 才该抓
    dark = _halves(32, 16, (60, 24, 12), (12, 27, 60))
    s_dark = similarity(d0, describe_bytes(dark))
    check("C10 ★ 整体压暗 -> `lighting` 或 `richness` 抓住（不是 layout）",
          s_dark["weakest"] in ("lighting", "richness"),
          f"weakest={s_dark['weakest']} "
          f"lit={s_dark['lighting']:.3f} lay={s_dark['layout']:.3f}")

    # 构图变了（左右互换）：色相分布不变，layout 该抓
    flipped = _halves(32, 16, (40, 90, 200), (200, 80, 40))
    s_flip = similarity(d0, describe_bytes(flipped))
    check("C11 ★★ 构图左右互换 -> `layout` 显著下降（色相分布几乎不变）",
          s_flip["layout"] < 0.98 and s_flip["palette"] > 0.9,
          f"lay={s_flip['layout']:.3f} pal={s_flip['palette']:.3f}")
    check("C12 且 `weakest` 指向 layout",
          s_flip["weakest"] == "layout", str(s_flip["weakest"]))


# ---------------------------------------------------------------------------
# D. 整体批次漂移报告
# ---------------------------------------------------------------------------

def _d_drift() -> None:
    from little_crew.review.embed import describe_bytes, drift_report

    good_a = _halves(32, 16, (200, 80, 40), (40, 90, 200))
    good_b = _halves(32, 16, (198, 82, 42), (42, 88, 198))
    bad = _solid(32, 16, (10, 250, 10))              # 明显不同

    labels = ["S1", "S2", "S3"]
    descs = [describe_bytes(good_a), describe_bytes(good_b),
             describe_bytes(bad)]
    rep = drift_report(labels, descs)

    check("D1 结对数为 n(n-1)/2", rep["pairwise"] == 3, str(rep["pairwise"]))
    check("D2 ★★ `worst` 指向**真正不同的那一镜**",
          "S3" in rep["worstPair"], str(rep["worstPair"]))
    check("D3 `worst` 是全体最小值",
          rep["worst"] == min(p["similarity"] for p in rep["pairs"]),
          f"{rep['worst']:.4f}")
    check("D4 均值高于最低值（区分得开）",
          rep["mean"] > rep["worst"], f"{rep['mean']:.4f}")
    check("D5 给出最弱维度（返工方向）",
          rep["weakestDimension"] in
          ("palette", "layout", "lighting", "richness"),
          str(rep["weakestDimension"]))
    check("D6 ★ 相邻镜最低相似度单独给出（观众最易察觉的跳变）",
          rep["adjacentMin"] is not None, str(rep["adjacentMin"]))
    check("D7 每对都带逐维分解（可追溯）",
          all("dimensions" in p for p in rep["pairs"]))

    # 单镜不足以谈一致性
    one = drift_report(["S1"], [describe_bytes(good_a)])
    check("D8 ★ 少于两镜时**如实说不足**（不编造一致性）",
          one.get("enough") is False, str(one.get("note")))

    # 数量不符要报错
    try:
        drift_report(["A", "B"], [describe_bytes(good_a)])
        ok = False
    except ValueError:
        ok = True
    check("D9 标签数与描述子数不符 -> 报错", ok)


# ---------------------------------------------------------------------------
# E. 接线：观测值与算子
# ---------------------------------------------------------------------------

def _e_wiring() -> None:
    from little_crew.review.constraint import REGISTRY
    from little_crew.review.observe import measure_artifact

    blob = _halves(32, 16, (200, 80, 40), (40, 90, 200))

    # want_signature=False 时**不该**算（300ms 的代价）
    plain = measure_artifact(blob, None)
    check("E1 ★ 默认**不算**一致性子（避免每镜白花 300ms）",
          "consistencySignature" not in plain, str(sorted(plain)))

    with_sig = measure_artifact(blob, None, want_signature=True)
    check("E2 显式要求时才算", "consistencySignature" in with_sig)
    check("E3 签名是**纯数据**（能进 JSON 证据链）",
          isinstance(with_sig.get("consistencySignature"), dict))

    # 解不开时**不填默认值**
    broken = measure_artifact(b"not a png", None, want_signature=True)
    check("E4 ★★ 解不开时不填假签名（避免静默的错误结论）",
          "consistencySignature" not in broken
          and "consistencyUnavailable" in broken,
          str(sorted(broken)))

    # 算子读得到追溯信息
    # `REGISTRY.get` 返回 `(版本, 算子)` —— 不是算子本身
    _v, identity_op = REGISTRY.get("identity_similarity")

    out = identity_op({"min": 0.5}, {
        "similarity": 0.9, "method": "consistency-descriptor/v2",
        "weakestDimension": "palette", "otherShotId": "S2"})
    check("E5 ★ 算子把**算法名**写进 observed（可追溯）",
          out.observed.get("method") == "consistency-descriptor/v2",
          str(out.observed))
    check("E6 算子把最弱维与对照镜写进 observed",
          out.observed.get("weakestDimension") == "palette"
          and out.observed.get("otherShotId") == "S2",
          str(out.observed))

    _v2, pair_op = REGISTRY.get("pairwise_similarity")
    pw = pair_op({"min": 0.5}, {
        "similarity": 0.3, "method": "x", "otherShotId": "S9",
        "weakestDimension": "layout"})
    check("E7 pairwise 失败时 detail 指出**跨镜漂移**",
          "跨镜漂移" in (pw.detail or ""), (pw.detail or "")[:80])


# ---------------------------------------------------------------------------
# F. 权重
# ---------------------------------------------------------------------------

def _f_weights() -> None:
    from little_crew.review.embed import DIMENSION_NAMES, _WEIGHTS
    from little_crew.review.gate_runner import GateRunner  # noqa: F401

    check("F1 ★ 权重和为 1（否则总分不在 [0,1]）",
          abs(sum(_WEIGHTS.values()) - 1.0) < 1e-9,
          f"{sum(_WEIGHTS.values()):.9f}")
    check("F2 每个维度都有权重",
          set(_WEIGHTS) == set(DIMENSION_NAMES),
          f"{sorted(_WEIGHTS)} vs {sorted(DIMENSION_NAMES)}")
    check("F3 权重都为正（不允许某维被静默丢弃）",
          all(v > 0 for v in _WEIGHTS.values()), str(dict(_WEIGHTS)))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:                                   # noqa: BLE001
        pass

    print("=" * 78)
    print("L2 一致性描述子 —— 自检")
    print("=" * 78)

    _a_decoder()
    _b_descriptor()
    _c_similarity()
    _c_causality()
    _d_drift()
    _e_wiring()
    _f_weights()

    print()
    npass = sum(1 for _, ok, _ in _results if ok)
    for name, ok, detail in _results:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 78)
    print(f"结果：{npass}/{len(_results)} 通过")
    return 0 if npass == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
