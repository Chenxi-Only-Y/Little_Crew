"""如果按「三轮里最高分」判定，通过率会是多少？（vs 现在的「最后一轮」）"""
import glob, json, pathlib, sys
sys.stdout.reconfigure(encoding="utf-8")
ROOT = pathlib.Path(r"D:\AI\ds.8\Little_Crew")
TH = 65.0
rows = []
for f in sorted(glob.glob(str(ROOT / "research" / "script-rounds" / "*.json"))):
    d = json.loads(pathlib.Path(f).read_text(encoding="utf-8"))
    rs = [r.get("total") for r in (d.get("rounds") or []) if r.get("total") is not None]
    if rs:
        rows.append({"id": d.get("episodeId") or pathlib.Path(f).stem, "scores": rs})
p = ROOT / "research" / "retry-cost" / "raw-runs.json"
if p.is_file():
    for r in (json.loads(p.read_text(encoding="utf-8")).get("runs") or []):
        rs = [x.get("total") for x in (r.get("detail") or []) if x.get("total") is not None]
        if rs:
            rows.append({"id": "legacy-%s" % r.get("run"), "scores": rs})
n = len(rows)
last = sum(1 for x in rows if x["scores"][-1] >= TH)
best = sum(1 for x in rows if max(x["scores"]) >= TH)
first = sum(1 for x in rows if x["scores"][0] >= TH)
print("  门槛 %.0f 分，n=%d" % (TH, n))
print("    首轮就过          %2d/%d = %3.0f%%" % (first, n, first / n * 100))
print("    最后一轮过（现状）%2d/%d = %3.0f%%" % (last, n, last / n * 100))
print("    ★ 取最高分        %2d/%d = %3.0f%%" % (best, n, best / n * 100))
gain = [x for x in rows if max(x["scores"]) >= TH > x["scores"][-1]]
print("    取最高才能过的集: %d 集 %s" % (len(gain), [x["id"] for x in gain]))
print()
print("  逐集分数（够 3 轮的）:")
for x in rows:
    if len(x["scores"]) >= 2:
        print("    %-12s %s  最高 %.1f" % (x["id"], " -> ".join("%.1f" % s for s in x["scores"]), max(x["scores"])))
