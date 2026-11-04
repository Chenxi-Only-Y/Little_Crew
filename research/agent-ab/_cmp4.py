import json, pathlib, sys
from collections import Counter, defaultdict
sys.stdout.reconfigure(encoding="utf-8")
R = pathlib.Path(r"D:\AI\ds.8\Little_Crew\research\agent-ab")
cur = json.loads((R / "raw.json").read_text(encoding="utf-8"))
p4 = json.loads((R / "raw.prev4.json").read_text(encoding="utf-8"))

def report(rows, tag):
    agg = defaultdict(lambda: [0, 0])
    for r in rows:
        arm = "B" if "多智能体" in r["condition"] else "A"
        agg[arm][1] += 1
        if r.get("ok"):
            agg[arm][0] += 1
    nbad = sum(1 for r in rows if not r.get("ok"))
    kinds = Counter()
    for r in rows:
        if r.get("ok"):
            continue
        w = str(r.get("reason") or "")
        if "下限 2" in w or "只有 1 个镜头" in w:
            kinds["场次镜头不足"] += 1
        elif "bad-json" in w:
            kinds["JSON 截断"] += 1
        else:
            kinds["其它"] += 1
    print("  %-18s 失败 %d/%d = %.0f%%   A %d/%d  B %d/%d  %s"
          % (tag, nbad, len(rows), nbad / len(rows) * 100,
             agg["A"][0], agg["A"][1], agg["B"][0], agg["B"][1],
             dict(kinds) if kinds else ""))
    return nbad

report(p4, "改前(无 max_tokens)")
report(cur, "改后(8192)")
