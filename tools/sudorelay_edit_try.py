"""图生图批量试验：Sudorelay `/images/edits`（2026-11-13）。

用法：`python tools/sudorelay_edit_try.py`
读 `outputs/_usertest/user_prompt_3.txt` 作为提示词，跑两组参考图：
  ① ref_better.jpg      —— 用户提供的"更好那版"参考图
  ② batch5b/run3.png    —— 本次文生图里较好的一张（自我精修）
结果写到 `outputs/_usertest/edits_*.png`，并打一行摘要。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from sudorelay_edit import edit_image  # noqa: E402

PROMPT = (ROOT / "outputs" / "_usertest" / "user_prompt_3.txt").read_text(encoding="utf-8")
OUT = ROOT / "outputs" / "_usertest"

CASES = [
    ("ref_better", OUT / "refs" / "ref_better.jpg"),
    ("self_run3", OUT / "batch5b" / "run3.png"),
]

rows = []
for tag, ref in CASES:
    if not ref.is_file():
        rows.append({"case": tag, "ok": False, "error": f"参考图不存在：{ref}"})
        print(f"  ✗ {tag}: 参考图不存在")
        continue
    dst = OUT / f"edits_{tag}.png"
    res = edit_image(ref, PROMPT, model="gpt-image-2.5-sunburst",
                     size="1536x1024", out_path=dst, timeout_s=900)
    rows.append({"case": tag, "ref": str(ref), **{k: v for k, v in res.items() if k != "detail"}})
    if res.get("ok"):
        print(f"  ✓ {tag}: {res['seconds']}s · {res['bytes'] // 1024}KB -> {dst.name}")
    else:
        print(f"  ✗ {tag}: {res.get('error')} | {str(res.get('detail'))[:160]}")

(OUT / "edits_result.json").write_text(
    json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
print("  done:", len(rows), "case(s)")
