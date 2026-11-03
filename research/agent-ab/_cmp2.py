import json, pathlib, sys, statistics as st
from collections import defaultdict
sys.stdout.reconfigure(encoding="utf-8")
R = pathlib.Path(r"D:\AI\ds.8\Little_Crew\research\agent-ab")
cur = json.loads((R / "raw.json").read_text(encoding="utf-8"))
p2 = json.loads((R / "raw.prev2.json").read_text(encoding="utf-8"))

def rate(rows, tag):
    agg = defaultdict(lambda: [0, 0])
    for r in rows:
        arm = "B" if "多智能体" in r["condition"] else "A"
        agg[arm][1] += 1
        if r.get("ok"):
            agg[arm][0] += 1
    nbad = sum(1 for r in rows if not r.get("ok"))
    print("  %-22s fail %d/%d = %.0f%%   A %d/%d  B %d/%d"
          % (tag, nbad, len(rows), nbad / len(rows) * 100,
             agg["A"][0], agg["A"][1], agg["B"][0], agg["B"][1]))

rate(p2, "prev (per-scene only)")
rate(cur, "now (+ budget cap)")
print()
# 镜头数按剧本长度
agg = defaultdict(list)
for r in cur:
    if not r.get("ok"):
        continue
    arm = "B" if "多智能体" in r["condition"] else "A"
    agg[(r["script"].split("·")[0], arm)].append(r["shots"])
print("  shots by length:")
for L in ("短", "中", "长"):
    a = agg.get((L, "A"), [])
    b = agg.get((L, "B"), [])
    if a and b:
        print("    %s  A %.1f  B %.1f" % (L, st.mean(a), st.mean(b)))
bad = [r for r in cur if not r.get("ok")]
print()
print("  remaining failures: %d" % len(bad))
for r in bad:
    print("   ", r["condition"], "|", str(r.get("reason"))[:100])
