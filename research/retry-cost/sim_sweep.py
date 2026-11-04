import glob, json, pathlib, sys
sys.stdout.reconfigure(encoding="utf-8")
ROOT = pathlib.Path(r"D:\AI\ds.8\Little_Crew")
TH, MAXR = 65.0, 3
rows = []
for f in sorted(glob.glob(str(ROOT / "research" / "script-rounds" / "*.json"))):
    d = json.loads(pathlib.Path(f).read_text(encoding="utf-8"))
    rs = [r.get("total") for r in (d.get("rounds") or []) if r.get("total") is not None]
    if rs:
        rows.append(rs)
for r in (json.loads((ROOT/"research"/"retry-cost"/"raw-runs.json").read_text(encoding="utf-8")).get("runs") or []):
    rs = [x.get("total") for x in (r.get("detail") or []) if x.get("total") is not None]
    if rs:
        rows.append(rs)
n = len(rows)

def sim(scores, min_gain):
    best, used = scores[0], 1
    if best >= TH:
        return best, used
    for s in scores[1:MAXR]:
        used += 1
        gain = s - best
        if s > best:
            best = s
        if best >= TH:
            break
        if gain < min_gain:
            break
    return best, used

old_pass = sum(1 for s in rows if s[-1] >= TH)
old_rounds = sum(len(s) for s in rows)
full_best = sum(1 for s in rows if max(s) >= TH)
print("  基准：现状 %d/%d=%.0f%%（%d 轮） · 全跑完取最优 %d/%d=%.0f%%（%d 轮）"
      % (old_pass, n, old_pass/n*100, old_rounds, full_best, n, full_best/n*100,
         sum(len(s) for s in rows)))
print()
print("  %-10s %-12s %-8s %-10s" % ("MIN_GAIN", "通过率", "总轮次", "省轮次"))
for g in (0.0, 0.5, 1.0, 2.0, 3.0):
    res = [sim(s, g) for s in rows]
    ps = sum(1 for b, _ in res if b >= TH)
    rd = sum(u for _, u in res)
    print("  %-10.1f %2d/%d=%3.0f%%   %3d 轮    %2.0f%%"
          % (g, ps, n, ps/n*100, rd, (1-rd/old_rounds)*100))
