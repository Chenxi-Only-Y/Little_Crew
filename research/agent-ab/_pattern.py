"""从 48 条已盲判样本里，反推「节奏好的样本长什么样」。

判分来自人（节奏 1-5）。这里只看**可执行的结构**：
每场的景别序列、每场镜数、景别种类数。

目标：找出只在 5 分组出现、4 分组没有的结构 —— 那才配当模板。
"""
import json
import pathlib
import sys
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding="utf-8")
R = pathlib.Path(r"D:\AI\ds.8\Little_Crew\research\agent-ab")
key = json.loads((R / "blind-e1" / "answer_key.json").read_text(encoding="utf-8"))
raw = json.loads((R / "raw.json").read_text(encoding="utf-8"))
# 注意：raw.json 现在是「回退后」的数据吗？不 —— 盲判用的是当时的 raw。
# 为保证可复算，改用 raw.prev2.json（与盲判同批）之前先比对长度。
rhy = [5,5,5,5,4,5,5,1,4,5,4,5,4,5,5,3,4,5,1,1,5,5,5,4,5,5,5,1,5,5,
       4,1,5,5,1,1,5,4,5,5,5,4,1,5,1,1,5,1,5,4,1,5,4,5,5,4,5,5,4,5]

idx = {(r["script"], r.get("repeat"), r["condition"]): r for r in raw}

hi, lo = defaultdict(Counter), defaultdict(Counter)
per_scene_hi, per_scene_lo = Counter(), Counter()
sizes_hi, sizes_lo = Counter(), Counter()

for i in range(1, 61):
    label = key["b%d.png" % i]["label"]
    script, rep_s, arm = label.rsplit("·", 2)
    r = idx.get((script, int(rep_s[1:]), arm))
    if r is None or not r.get("ok"):
        continue
    sl = r.get("shotList") or []
    if not sl:
        continue
    by_scene = defaultdict(list)
    for s in sl:
        by_scene[s.get("sceneId")].append(s.get("shotSize"))
    score = rhy[i - 1]
    for sid, seq in by_scene.items():
        t = tuple(seq)
        (hi if score >= 5 else lo)[len(t)][t] += 1
        (per_scene_hi if score >= 5 else per_scene_lo)[len(t)] += 1
        for x in seq:
            (sizes_hi if score >= 5 else sizes_lo)[x] += 1

print("  每场镜数分布")
print("    5 分组:", dict(sorted(per_scene_hi.items())))
print("    4-分组:", dict(sorted(per_scene_lo.items())))
print()
print("  5 分组最常见景别序列（按镜数）")
for n in (2, 3, 4, 5):
    c = hi.get(n)
    if c:
        print("    %d 镜: %s" % (n, " | ".join("%s×%d" % ("/".join(k), v) for k, v in c.most_common(3))))
print()
print("  4-分组最常见景别序列")
for n in (2, 3, 4, 5):
    c = lo.get(n)
    if c:
        print("    %d 镜: %s" % (n, " | ".join("%s×%d" % ("/".join(k), v) for k, v in c.most_common(3))))
print()
print("  景别总数 5分组:", sum(sizes_hi.values()), " 4-分组:", sum(sizes_lo.values()))

