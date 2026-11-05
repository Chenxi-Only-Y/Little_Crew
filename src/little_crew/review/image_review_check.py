"""`review/image_review` 自检 —— 只用标准库，不联网。"""
from __future__ import annotations

import struct
import sys
import tempfile
import zlib
from pathlib import Path

from little_crew.review.image_review import MIN_BYTES, png_size, review

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def _png(path: Path, w: int, h: int, fill: bytes = b"\x00" * 16) -> Path:
    """写一个**最小合法 PNG**（只为了读头，不需要真像素）。"""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    raw = b""
    for _ in range(h):
        raw += b"\x00" + b"\x00\x00\x00" * w
    idat = zlib.compress(raw)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                     + chunk(b"IDAT", idat) + chunk(b"IEND", b""))
    return path


class _S:
    def __init__(self, sid):
        self.shot_id = sid


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("渲染产物可数审片 —— 自检")
    print("=" * 66)

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        good = _png(d / "a.png", 64, 64)
        bad = _png(d / "b.png", 32, 32)
        tiny = d / "tiny.png"
        tiny.write_bytes(b"x" * 100)
        notpng = d / "x.jpg"
        notpng.write_bytes(b"x" * (MIN_BYTES + 10))

        print(); print("A. PNG 头解析")
        check("A1 ★ 读到宽高", png_size(good) == (64, 64), str(png_size(good)))
        check("A2 非 PNG -> None", png_size(notpng) is None, "")
        check("A3 文件不存在 -> None", png_size(d / "nope.png") is None, "")

        print(); print("B. 各类缺陷")
        shots = [_S("S1"), _S("S2"), _S("S3"), _S("S4"), _S("S5")]
        arts = {"S1": [str(good)], "S2": [str(bad)], "S3": [str(tiny)],
                "S4": [str(notpng)], "S5": []}
        r = review(shots, artifacts_by_shot=arts, expect_size=(64, 64))
        kinds = {(f["shotId"], f["kind"]) for f in r["findings"]}
        check("B1 ★ 尺寸不符被抓", ("S2", "size-mismatch") in kinds, str(kinds))
        check("B2 ★ 过小文件被抓", ("S3", "tiny-file") in kinds, "")
        check("B3 ★★ 没有产物被抓", ("S5", "missing-artifact") in kinds, "")
        check("B4 非 PNG 只记 warn（不误杀）",
              any(f["kind"] == "not-png" and f["severity"] == "warn"
                  for f in r["findings"]), "")

        print(); print("C. 串镜（多镜同图）")
        r2 = review([_S("A"), _S("B")],
                    artifacts_by_shot={"A": [str(good)], "B": [str(good)]},
                    hashes_by_shot={"A": "sha256:1", "B": "sha256:1"})
        dup = [f for f in r2["findings"] if f["kind"] == "duplicate-artifact"]
        check("C1 ★★ 内容哈希相同 -> 报串镜", len(dup) == 1, str(dup))
        check("C2 ★ 说明和哪个镜重复", dup and dup[0]["detail"]["sameAs"] == "A",
              str(dup[0]["detail"]) if dup else "")

        print(); print("D. 不许假装审过")
        check("D1 ★★★ 明确写出「没审什么」",
              any("画面质量" in x for x in r["notReviewed"]), str(r["notReviewed"])[:60])
        check("D2 空产物清单也返回结构", review([])["total"] == 0, "")

    n = sum(1 for _, ok, _ in _r if ok)
    print()
    for name, ok, detail in _r:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 66)
    print(f"结果：{n}/{len(_r)} 通过")
    return 0 if n == len(_r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
