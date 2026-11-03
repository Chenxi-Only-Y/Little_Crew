import json, pathlib, sys, statistics as st
from collections import Counter, defaultdict
sys.stdout.reconfigure(encoding="utf-8")
R = pathlib.Path(r"D:\AI\ds.8\Little_Crew\research\agent-ab")
cur = json.loads((R / "raw.json").read_text(encoding="utf-8"))
p3 = json.loads((R / "raw.prev3.json").read_text(encoding="utf-8"))

def report(rows, tag):
    agg = defaultdict(lambda: [0, 0])
    two = defaultdict(lambda: [0, 0])   # 2 镜场占比
    for r in rows:
        arm = "B" if "多智能体" in r["condition"] else "A"
        agg[arm][1] += 1
        if r.get("ok"):
            agg[arm][0] += 1
        sl = r.get("shotList") or []
        if not sl:
            continue
        per = Counter(s.get("sceneId") for s in sl)
        for v in per.values():
            two[arm][1] += 1
            if v <= 2:
                two[arm][0] += 1
    nbad = sum(1 for r in rows if not r.get("ok"))
    print("  %s" % tag)
    print("    失败 %d/%d = %.0f%%" % (nbad, len(rows), nbad / len(rows) * 100))
    for a in ("A", "B"):
        ok, n = agg[a]
        r2 = two[a]
        print("      %s: 达成 %d/%d=%.0f%%   2镜场 %d/%d=%.0f%%"
              % (a, ok, n, ok / max(1, n) * 100, r2[0], r2[1], r2[0] / max(1, r2[1]) * 100))

report(p3, "改前（无 3~5 镜约束）")
print()
report(cur, "改后（每场 3~5 镜）")
print()
agg = defaultdict(list)
for r in cur:
    if not r.get("ok"):
        continue
    arm = "B" if "多智能体" in r["condition"] else "A"
    agg[arm].append(r["shots"])
for a in ("A", "B"):
    xs = agg[a]
    if xs:
        print("  镜头数 %s: 均值 %.1f  中位 %d" % (a, st.mean(xs), st.median(xs)))
