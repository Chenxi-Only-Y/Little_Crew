import json, pathlib, sys
from collections import defaultdict
sys.stdout.reconfigure(encoding="utf-8")
R = pathlib.Path(r"D:\AI\ds.8\Little_Crew\research\agent-ab")
raw = json.loads((R / "raw.json").read_text(encoding="utf-8"))
agg = defaultdict(list)
for r in raw:
    if not r.get("ok"):
        continue
    sl = r.get("shotList") or []
    if not sl:
        continue
    lens = [len(str(s.get("description") or "")) for s in sl]
    arm = "B" if "多智能体" in r["condition"] else "A"
    agg[arm].append((len(sl), sum(lens) / len(lens)))
for arm in ("A", "B"):
    xs = agg[arm]
    if xs:
        print("  %s  镜头数 %.1f   平均描述字数 %.1f   总字数 %.0f"
              % (arm, sum(x[0] for x in xs) / len(xs),
                 sum(x[1] for x in xs) / len(xs),
                 sum(x[0] * x[1] for x in xs) / len(xs)))
