"""**预注册盲判**：把 P0-2 的五张图打乱、去掉标签，生成一份评分页 + 空表。

## 为什么不用自动指标

`metric_hard.py` 实测：紫色占比与视觉损害**不相关**（最差的 1.0 反而紫色最多），
地平线指标**假设"天空在上"**，1.0 那张上半是金黄土坡 → 前提崩了。

> **"构图是否听白模"没法用通用像素统计自动判。**

## 预注册（**先写判据，再看图**）

**问题**：这张图里，**薰衣草花田是否占据画面的主体**（≥ 一半面积）？

| 分 | 含义 |
|---|---|
| **1** | 花田明显不是主体（地形/其他东西占主导） |
| **2** | 花田只在边缘或底部一条 |
| **3** | 花田约占一半 |
| **4** | 花田明显是主体，但有较大其他元素 |
| **5** | 花田铺满画面、是绝对主体 |

**第二问**：人物是否站在**画面偏左三分之一**？（是/否/看不清）

**盲判规则**：文件名已随机化，评分时**不要打开 `answer_key.json`**。
**样本量**：5 张（n=5 只能看方向，不能下断言 —— 这一点必须写在结论里）。

## 产出

```
blind/        打乱后的图（b1.png … b5.png）+ 空的 scores.csv
blind/answer_key.json   评分完成后才看
```
"""
from __future__ import annotations

import json
import random
import shutil
import sys
from pathlib import Path

BASE = Path(r"D:\AI\ds.8\Little_Crew\research\p0-2-control-ab")
OUT = BASE / "blind"

#: 参与盲判的图（**不包含 diff.png / 控制图**）
SRC = [
    ("A_no_control.png", "A 无控制（基线）"),
    ("B_with_control.png", "B strength=1.0"),
    ("B_strength_0.5.png", "B strength=0.5"),
    ("B_strength_0.35.png", "B strength=0.35"),
    ("B_with_props_0.35.png", "B 0.35 + 道具"),
]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")                # type: ignore[attr-defined]
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    items = list(SRC)
    random.seed(20261029)          # 固定种子 —— 别人能复现同一份打乱
    random.shuffle(items)

    key = {}
    rows = ["文件,花田是否主体(1-5),人物是否偏左(是/否/看不清),备注"]
    for i, (fn, label) in enumerate(items, 1):
        dst = OUT / f"b{i}.png"
        shutil.copyfile(BASE / fn, dst)
        key[dst.name] = {"label": label, "origin": fn}
        rows.append(f"b{i}.png,,,")

    (OUT / "answer_key.json").write_text(
        json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "scores.csv").write_text("\n".join(rows) + "\n", encoding="utf-8-sig")

    # 一个能直接看的评分页（图按随机顺序排，不显示标签）
    html = ["<!doctype html><meta charset='utf-8'><title>P0-2 盲判</title>",
            "<style>body{background:#15171b;color:#e6e8ec;font:14px/1.6 system-ui;",
            "padding:24px}h2{font-size:15px;color:#9aa3b2;font-weight:600}",
            ".g{display:grid;grid-template-columns:1fr 1fr;gap:16px}",
            "figure{margin:0;background:#1d2026;border:1px solid #2c313a;",
            "border-radius:8px;padding:8px}img{width:100%;border-radius:5px}",
            "figcaption{color:#7dd3fc;font-weight:600;margin-bottom:6px}",
            ".q{background:#1a1f27;border-left:3px solid #fbbf24;padding:10px 14px;",
            "border-radius:6px;margin:14px 0;font-size:13px}</style>",
            "<h1>P0-2 盲判</h1>",
            "<div class='q'><b>预注册判据</b><br>",
            "问一：薰衣草花田是否占据画面主体？1=完全不是 / 3=约一半 / 5=铺满画面<br>",
            "问二：人物是否在画面偏左三分之一？（是 / 否 / 看不清）<br>",
            "<b>先写下判据再打分；不要打开 answer_key.json。</b></div>",
            "<h2>五张图（顺序已打乱，标签已隐藏）</h2><div class='g'>"]
    for i in range(1, len(items) + 1):
        html.append(f"<figure><figcaption>b{i}</figcaption>"
                    f"<img src='b{i}.png'></figure>")
    html.append("</div><h2>把分数填进 scores.csv</h2>")
    (OUT / "index.html").write_text("".join(html), encoding="utf-8")

    print(f"  生成 {len(items)} 张（顺序已打乱，种子固定 20261029）")
    print("  看:", OUT / "index.html")
    print("  填:", OUT / "scores.csv")
    print("  评分完再开:", OUT / "answer_key.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
