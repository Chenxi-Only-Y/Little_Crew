"""三镜 6 张图的盲判集（A/B 混合、标签隐藏、固定种子可复现）。

判据同单镜版：
  问一 薰衣草花田是否画面主体？1=完全不是 / 3=约一半 / 5=铺满
  问二 人物是否在画面偏左三分之一？（是 / 否 / 看不清）

**评分前不要打开 answer_key.json。**
"""
from __future__ import annotations

import json
import random
import shutil
import sys
from pathlib import Path

BASE = Path(r"D:\AI\ds.8\Little_Crew\research\p0-2-control-ab")
SRC = BASE / "three"
OUT = BASE / "blind3"

#: 6 张：三镜 × A(无控制)/B(强度 0.5)
ITEMS = []
for i in (1, 2, 3):
    ITEMS.append((f"{i}A.png", f"镜{i} · A 无控制"))
    ITEMS.append((f"{i}B.png", f"镜{i} · B 强度0.5"))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")             # type: ignore[attr-defined]
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    items = list(ITEMS)
    random.seed(20261101)                                # 固定种子，别人可复现
    random.shuffle(items)

    key, rows = {}, ["文件,花田是否主体(1-5),人物是否偏左(是/否/看不清),备注"]
    for i, (fn, label) in enumerate(items, 1):
        shutil.copyfile(SRC / fn, OUT / f"b{i}.png")
        key[f"b{i}.png"] = {"label": label, "origin": fn}
        rows.append(f"b{i}.png,,,")

    (OUT / "answer_key.json").write_text(
        json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "scores.csv").write_text("\n".join(rows) + "\n", encoding="utf-8-sig")

    html = ["<!doctype html><meta charset='utf-8'><title>P0-2 三镜盲判</title>",
            "<style>body{background:#15171b;color:#e6e8ec;font:14px/1.6 system-ui;padding:24px}",
            "h2{font-size:15px;color:#9aa3b2}.g{display:grid;grid-template-columns:1fr 1fr;gap:16px}",
            "figure{margin:0;background:#1d2026;border:1px solid #2c313a;border-radius:8px;padding:8px}",
            "img{width:100%;border-radius:5px}figcaption{color:#7dd3fc;font-weight:600;margin-bottom:6px}",
            ".q{background:#1a1f27;border-left:3px solid #fbbf24;padding:10px 14px;border-radius:6px;",
            "margin:14px 0;font-size:13px}</style>",
            "<h1>P0-2 三镜盲判（6 张）</h1>",
            "<div class='q'><b>预注册判据</b><br>",
            "问一：薰衣草花田是否画面主体？1=完全不是 / 3=约一半 / 5=铺满画面<br>",
            "问二：人物是否在画面偏左三分之一？（是 / 否 / 看不清）<br>",
            "<b>先写判据再打分；不要打开 answer_key.json。</b><br>",
            "三镜成对，但**顺序已打乱**，不要试图猜哪两张是一对。</div>",
            "<h2>六张图</h2><div class='g'>"]
    for i in range(1, len(items) + 1):
        html.append(f"<figure><figcaption>b{i}</figcaption><img src='b{i}.png'></figure>")
    html.append("</div><h2>分数填进 scores.csv</h2>")
    (OUT / "index.html").write_text("".join(html), encoding="utf-8")

    print(f"  生成 {len(items)} 张（种子 20261101）")
    print("  看:", OUT / "index.html")
    print("  填:", OUT / "scores.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
