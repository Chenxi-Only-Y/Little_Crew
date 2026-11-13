"""批量试验：5 次文生图 + 4 次图生图（用用户提供的参考图）。

参考图来自用户本轮附件（10 张），这里取 4 张不同风格：
  ref_brown   棕毛衣+米白阔腿裤（温柔通勤）
  ref_purple  灰紫露肩+黑阔腿裤（与本次提示词最接近）
  ref_school  藏蓝开衫+灰百褶裙（校园）
  ref_y2k     豹纹吊带+军绿工装裤（甜酷 Y2K）

输出：
  outputs/_usertest/batch5c/run{1..5}.png       5 次文生图
  outputs/_usertest/refs5/*.jpg                 压缩后的参考图
  outputs/_usertest/edits_ref_{tag}.png         图生图结果
  outputs/_usertest/trial_v4.json               全部结果摘要
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from sudorelay_edit import edit_image  # noqa: E402

PROMPT = (ROOT / "outputs" / "_usertest" / "user_prompt_3.txt").read_text(encoding="utf-8")
OUT = ROOT / "outputs" / "_usertest"
FFMPEG = r"D:\AI\FFmpeg\bin\ffmpeg.exe"

REFS = {
    "ref_brown": "56b15a0a7ee3de1cc5f314cf4b479ff35867a68295be71ed4c1db7109113558f",
    "ref_purple": "630e23fbac6c4c51ae8cd8964f0f9e440fc87abd0aa9f5d8dc55619ab2ff9c2e",
    "ref_school": "1f425dbb8f49c68067e252d72875c3209af4ef7514d8bb43918c845b197b5fcc",
    "ref_y2k": "c242fd5cb84ff06a5bd23cb4532c5dde7cb4495638f1f64c26a9cb2947923dfb",
}
OBJ = Path(r"C:\Users\OnlyY\.dsh\attachments\v1\objects")


def compress(name: str, sha: str) -> Path | None:
    src = OBJ / sha[:2] / sha
    if not src.is_file():
        print(f"  ✗ 找不到参考图 {name}: {src}")
        return None
    dst = OUT / "refs5" / f"{name}.jpg"
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(src), "-vf", "scale='min(1536,iw)':-2", "-q:v", "3",
                    str(dst)], check=False)
    if not dst.is_file():
        return None
    print(f"  ✓ {name}: {dst.stat().st_size // 1024}KB")
    return dst


def main() -> int:
    from little_crew.models.registry import build_default_registry
    from little_crew.runtime.render import http_job
    from little_crew.runtime.channels.http import HttpChannel

    rows: dict = {"text2img": [], "img2img": []}

    # ---- 5 次文生图 -------------------------------------------------
    be = build_default_registry().get("gpt-image-2.6-sunburst").backends[0]
    ch = HttpChannel(be, timeout_s=900)
    out5 = OUT / "batch5c"
    out5.mkdir(parents=True, exist_ok=True)
    for i in range(1, 6):
        job = http_job(f"p4-{i}", be, {"prompt": PROMPT, "size": "1536x1024",
                                       "model": "gpt-image-2.6-sunburst"},
                       idempotency_key=f"p4-{i}", expect="image", timeout_s=900)
        t0 = time.time()
        h = ch.submit(job)
        dt = time.time() - t0
        if "FAIL" in str(h.status):
            print(f"  text run{i}: 失败")
            continue
        art = ch.collect(h)[0]
        dst = out5 / f"run{i}{Path(art.uri).suffix}"
        dst.write_bytes(Path(art.uri).read_bytes())
        rows["text2img"].append({"run": i, "seconds": round(dt, 1),
                                 "kb": dst.stat().st_size // 1024})
        print(f"  text run{i}: {dt:.1f}s · {dst.stat().st_size // 1024}KB")

    # ---- 4 次图生图 -------------------------------------------------
    for name, sha in REFS.items():
        ref = compress(name, sha)
        if ref is None:
            continue
        dst = OUT / f"edits5_{name}.png"
        res = edit_image(ref, PROMPT, model="gpt-image-2.5-sunburst",
                         size="1536x1024", out_path=dst, timeout_s=900)
        rows["img2img"].append({"case": name, "ref": str(ref),
                                **{k: v for k, v in res.items() if k != "detail"}})
        if res.get("ok"):
            print(f"  edit {name}: {res['seconds']}s · {res['bytes'] // 1024}KB")
        else:
            print(f"  edit {name} ✗ {res.get('error')}")

    (OUT / "trial_v4.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
