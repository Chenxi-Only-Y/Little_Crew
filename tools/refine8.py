"""保图微调：对用户提供的 8 张设定板做 img2img 精修（2026-11-13）。

**微调的语义边界**（写死在这里，避免"一调就变脸"）：
- 保留：人物身份、发型、妆容、服饰结构与配色、四视图布局、背景
- 只修：手部与手指数量、鞋型结构、发丝走向、布料纹理、四视图一致性
- 禁止：改变脸型五官、换装、加/减视图、加文字水印

提示词刻意写**短**：长提示词会压过参考图（本轮实测结论）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from sudorelay_edit import edit_image  # noqa: E402

OUT = ROOT / "outputs" / "_usertest"
FFMPEG = r"D:\AI\FFmpeg\bin\ffmpeg.exe"
OBJ = Path(r"C:\Users\OnlyY\.dsh\attachments\v1\objects")

#: 用户本轮给的 8 张（文件名 -> sha256）
REFS = {
    "r1_brown": "be62dc648da28120d31e6f2c0f23d8bf34d71b29df540dcd8d900f3ea817794c",
    "r2_purple": "052b1fa29eb816f0ec23792b001d33aa029b8ed9473857ef017bd421e70150a9",
    "r3_stripe": "14fdf338dd6242239f6c10babe272c3ad76f10c7f6d36167499b1bb116beb005",
    "r4_y2k": "46359e30b00ec58930c37b18a2a346190372829a5be0c7b0c7168a33ebed817d",
    "r5_school": "f5999dd07391b5f27a994c3f6fa658eb9a892590ae62974591e9fcc406276195",
    "r6_bluebow": "f7717c778f83f071fb9dc1a5d0ef385c152a628952218d80db113259f3872205",
    "r7_argyle": "b7b1f22f2f0ded2a85a83fa2fe6ac90b4b7cfd66ee42808a0a25fa63a3b99745",
    "r8_stripe2": "eac81f203744ea33db5123316cb1697e498f9d76f338e32cccde1032c3702e2e",
}

PROMPT = (
    "保持与参考图完全相同的人物身份、脸型五官、发型、妆容、服饰结构与配色、"
    "四视图布局与浅灰影棚背景，不改变任何设计与长相，只做细节精修："
    "①手部结构与手指数量正确；②鞋型与鞋底结构清晰、左右对称；"
    "③发丝走向顺滑、发束层次自然；④布料纹理与缝线更清晰；"
    "⑤四个视图的脸、发型、服装、配饰完全一致。"
    "高精度 3D 半写实国漫 CG 质感，柔和棚光，无文字无水印。"
)


def compress(name: str, sha: str) -> Path | None:
    src = OBJ / sha[:2] / sha
    if not src.is_file():
        print(f"  ✗ 缺参考图 {name}")
        return None
    dst = OUT / "refs8" / f"{name}.jpg"
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(src), "-vf", "scale='min(1536,iw)':-2", "-q:v", "2",
                    str(dst)], check=False)
    return dst if dst.is_file() else None


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gpt-image-2.6-sunburst")
    ap.add_argument("--tag", default="refined")
    args = ap.parse_args()
    rows = []
    for name, sha in REFS.items():
        ref = compress(name, sha)
        if ref is None:
            continue
        dst = OUT / "refine8" / f"{name}_{args.tag}.png"
        res = edit_image(ref, PROMPT, model=args.model,
                         size="1536x1024", out_path=dst, timeout_s=900)
        rec = {"case": name, "ref": str(ref),
               **{k: v for k, v in res.items() if k != "detail"}}
        rows.append(rec)
        if res.get("ok"):
            print(f"  ✓ {name}: {res['seconds']}s · {res['bytes'] // 1024}KB")
        else:
            print(f"  ✗ {name}: {res.get('error')} | {str(res.get('detail'))[:120]}")
    (OUT / f"refine8_result_{args.tag}.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print("done:", len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
