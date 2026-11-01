"""判据 v2：**与镜头内容无关**的两问。

## 为什么换

v1 问「薰衣草花田是否画面主体」—— 只对拍花田的镜头有意义。
实测：镜 2 拍的是倒地的旅人，该分数**恒为 1**，A/B 必然打平，
于是把"测不到"误读成"没差别"（文档 §六 E7、§八 8.2 已记）。

## v2 的两问（对任何镜头都成立）

**问一 · 画面是否成立？**（有没有明显崩坏：手、脸、肢体、结构、穿模）
```
1 严重崩坏，明显不成立
2 有多处明显错误
3 有瑕疵但整体能看
4 基本成立，小瑕疵
5 完全成立，看不出问题
```

**问二 · 人物与场景的空间关系是否合理？**
（脚有没有踩在地上、有没有被切掉、与背景比例是否协调、有没有悬空/嵌进物体）
```
1 完全不合理（悬空 / 嵌进背景 / 比例明显错）
2 明显不合理
3 说不准
4 基本合理
5 完全合理
```

**判据设计原则（写下来，防再犯）**：
1. **对每张图都问得出口** —— 不许有"这类镜头不适用"的分支
2. **不需要知道镜头内容** —— 只看图就能答
3. **允许"说不准"** —— 强行打分会产生假信号
"""
from __future__ import annotations

import json
import random
import shutil
import sys
from pathlib import Path

BASE = Path(r"D:\AI\ds.8\Little_Crew\research\p0-2-control-ab")
SRC = BASE / "three"
OUT = BASE / "blind4"

ITEMS = []
for i in (1, 2, 3):
    ITEMS.append((f"{i}A.png", f"镜{i} · A 无控制"))
    ITEMS.append((f"{i}B.png", f"镜{i} · B 强度0.5"))

Q1 = "画面是否成立(1-5)"
Q2 = "空间关系是否合理(1-5)"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")             # type: ignore[attr-defined]
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    items = list(ITEMS)
    random.seed(20261101)          # 与 v1 同种子 —— 顺序一致，便于对照
    random.shuffle(items)

    key, rows = {}, [f"文件,{Q1},{Q2},备注"]
    for i, (fn, label) in enumerate(items, 1):
        shutil.copyfile(SRC / fn, OUT / f"b{i}.png")
        key[f"b{i}.png"] = {"label": label, "origin": fn}
        rows.append(f"b{i}.png,,,")
    (OUT / "answer_key.json").write_text(
        json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "scores.csv").write_text("\n".join(rows) + "\n", encoding="utf-8-sig")

    html = ["<!doctype html><meta charset='utf-8'><title>P0-2 盲判 v2</title>",
            "<style>body{background:#15171b;color:#e6e8ec;font:14px/1.6 system-ui;padding:24px}",
            "h2{font-size:15px;color:#9aa3b2}.g{display:grid;grid-template-columns:1fr 1fr;gap:16px}",
            "figure{margin:0;background:#1d2026;border:1px solid #2c313a;border-radius:8px;padding:8px}",
            "img{width:100%;border-radius:5px}figcaption{color:#7dd3fc;font-weight:600;margin-bottom:6px}",
            "table{border-collapse:collapse;font-size:13px;margin-top:6px}td,th{border:1px solid #2c313a;",
            "padding:4px 10px}.q{background:#1a1f27;border-left:3px solid #fbbf24;padding:10px 14px;",
            "border-radius:6px;margin:14px 0}</style>",
            "<h1>P0-2 盲判 v2（6 张）</h1>",
            "<div class='q'><b>两问，对每张图都适用；不知道镜头内容也能答。</b>",
            "<table><tr><th>分</th><th>问一 画面是否成立</th><th>问二 空间关系是否合理</th></tr>",
            "<tr><td>1</td><td>严重崩坏</td><td>悬空 / 嵌进背景 / 比例明显错</td></tr>",
            "<tr><td>3</td><td>有瑕疵但能看</td><td>说不准</td></tr>",
            "<tr><td>5</td><td>完全成立</td><td>完全合理</td></tr></table>",
            "<b>先写判据再打分；不要打开 answer_key.json。</b></div>",
            "<h2>六张图</h2><div class='g'>"]
    for i in range(1, len(items) + 1):
        html.append(f"<figure><figcaption>b{i}</figcaption><img src='b{i}.png'></figure>")
    html.append("</div><h2>分数填进 scores.csv</h2>")
    (OUT / "index.html").write_text("".join(html), encoding="utf-8")

    print(f"  生成 {len(items)} 张（种子 20261101，与 v1 同序）")
    print("  看:", OUT / "index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
