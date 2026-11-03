import json, pathlib, sys
from collections import defaultdict
sys.stdout.reconfigure(encoding="utf-8")
R = pathlib.Path(r"D:\AI\ds.8\Little_Crew\research\agent-ab")
new = json.loads((R / "raw.json").read_text(encoding="utf-8"))
old = json.loads((R / "raw.prev.json").read_text(encoding="utf-8"))

def rate(rows, tag):
    agg = defaultdict(lambda: [0, 0])
    for r in rows:
        arm = "B-multi" if "多智能体" in r["condition"] else "A-single"
        agg[arm][1] += 1
        if r.get("ok"):
            agg[arm][0] += 1
    nbad = sum(1 for r in rows if not r.get("ok"))
    print("  %s: fail %d/%d = %.0f%%" % (tag, nbad, len(rows), nbad / len(rows) * 100))
    for arm in ("A-single", "B-multi"):
        ok, n = agg[arm]
        print("    %s: %d/%d = %.0f%%" % (arm, ok, n, ok / n * 100))

rate(old, "BEFORE")
print()
rate(new, "AFTER per-scene")
print()
bad = [r for r in new if not r.get("ok")]
print("  remaining %d:" % len(bad))
for r in bad:
    print("   ", r["condition"], "|", str(r.get("reason"))[:110])
