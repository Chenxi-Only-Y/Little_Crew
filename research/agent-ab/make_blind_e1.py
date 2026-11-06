"""E1 盲判包：从 raw.json 抽 A/B 两版**镜头表**，匿名打乱。

判者看不到"有没有导演预算"。只评两件事：
  ① 镜头是否覆盖了剧本的每一场？（1-5）
  ② 节奏是否合理（镜头数/时长分布）？（1-5）

不重跑、不用 GPU —— 原料本来就在 raw.json 的 shotList 里。
"""
import csv
import json
import random
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
R = Path(r"D:\AI\ds.8\Little_Crew\research\agent-ab")
OUT = R / "blind-e1"
raw = json.loads((R / "raw.json").read_text(encoding="utf-8"))

# ⚠️ **必须把剧本一起给人看** —— 否则"覆盖了每一场吗"这个问题没法答
# （第一版只给镜头表，判者不知道剧本有几场、每场讲什么）
import sys as _sys
_sys.path.insert(0, r"D:\AI\ds.8\Little_Crew\src")
_sys.path.insert(0, str(R))
import run_ab as _rab  # noqa: E402

SCRIPTS = _rab.SCRIPTS

# 按 (剧本, 重复) 配对
pairs = {}
for r in raw:
    key = (r["script"], r.get("repeat"))
    arm = "B" if "多智能体" in r["condition"] else "A"
    pairs.setdefault(key, {})[arm] = r

items = []
for key, ab in sorted(pairs.items()):
    if "A" not in ab or "B" not in ab:
        continue
    # ⚠️ **失败的记录不放进盲判包**（2026-11-06）：
    # 它们没有镜头表，判者只能打 1/1，白白消耗注意カ ——
    # 而失败本身已经计入达成率，两件事不要混在一张表里。
    if not (ab["A"].get("ok") and ab["B"].get("ok")):
        continue
    items.append((key, ab["A"]))
    items.append((key, ab["B"]))

random.seed(20261101)
random.shuffle(items)
if OUT.exists():
    import shutil
    shutil.rmtree(OUT)
OUT.mkdir(parents=True)

key_out, rows = {}, ["文件,覆盖场次(1-5),节奏合理(1-5),备注"]
html = ["<!doctype html><meta charset='utf-8'><title>E1 盲判</title>",
 "<style>body{background:#15171b;color:#e6e8ec;font:14px/1.6 system-ui;padding:24px}",
 "h2{font-size:15px;color:#9aa3b2}.c{background:#1d2026;border:1px solid #2c313a;",
 "border-radius:8px;padding:12px;margin:14px 0}table{border-collapse:collapse;",
 "font-size:12.5px;margin-top:8px}td,th{border:1px solid #2c313a;padding:3px 9px}",
 ".q{background:#1a1f27;border-left:3px solid #fbbf24;padding:10px 14px;border-radius:6px}",
 "</style><h1>E1 盲判（30 组 × 2 = 60 份镜头表）</h1>",
 "<div class='q'><b>两问（每份都适用）</b><br>",
 "① 这组镜头覆盖了剧本的每一场吗？1=大量漏场 / 3=基本覆盖 / 5=完整覆盖<br>",
 "② 节奏合理吗（镜头数、时长分布）？1=明显失衡 / 3=一般 / 5=很合理<br>",
 "<b>不要试图猜哪两份是一对；不要看 answer_key.json。</b></div>"]
for i, (k, r) in enumerate(items, 1):
    shots = r.get("shotList") or []
    key_out[f"b{i}.png"] = {"label": f"{k[0]}·r{k[1]}·{r['condition']}",
                            "shots": len(shots)}
    rows.append(f"b{i},,,")
    # ⚠️ 字段是**驼峰**（sceneId / shotSize / cameraMovement / durationSec）——
    # 第一版写成下划线，结果所有单元格是空的，页面看起来"什么都没有"。
    trs = "".join(
        f"<tr><td>{s.get('sceneId','')}</td><td>{s.get('shotSize','')}</td>"
        f"<td>{s.get('cameraMovement','')}</td><td>{s.get('durationSec','')}</td>"
        f"<td style='max-width:520px'>{str(s.get('description',''))[:44]}</td></tr>"
        for s in shots)
    sd = SCRIPTS.get(k[0])
    sc_html = ""
    if sd is not None:
        lis = "".join(
            f"<li><b>{s.get('scene_id','')} {s.get('heading','')}</b> —— "
            f"{str(s.get('text',''))[:90]}</li>" for s in sd.scenes)
        sc_html = f"<div style='color:#9aa3b2;margin:6px 0'>剧本共 {len(sd.scenes)} 场：</div><ul>{lis}</ul>"
    html.append(f"<div class='c'><b>b{i}</b> · 剧本「{k[0]}」· 共 <b>{len(shots)}</b> 镜"
                f"{sc_html}"
                f"<table><tr><th>场</th><th>景别</th><th>运镜</th><th>时长</th><th>镜头内容</th></tr>"
                f"{trs}</table></div>")
(OUT / "index.html").write_text("".join(html), encoding="utf-8")
(OUT / "answer_key.json").write_text(json.dumps(key_out, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
(OUT / "scores.csv").write_text("\n".join(rows) + "\n", encoding="utf-8-sig")
print(f"  生成 {len(items)} 份（{len(pairs)} 组配对，种子 20261101）")
print("  看:", OUT / "index.html")
