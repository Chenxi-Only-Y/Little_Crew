"""模拟两条策略：现状（跑满3轮取最后） vs 新策略（早停 + 取最优）。

新策略：
  ① 每轮结束，取当前最好分
  ② 最好分 >= 门槛 -> 停（过了就不用再改）
  ③ 新一轮相对最好分涨幅 < MIN_GAIN -> 停（没进步就别烧）
  ④ 最多 3 轮
  最终交付 = 历史最优那一轮
"""
import glob, json, pathlib, sys
sys.stdout.reconfigure(encoding="utf-8")
ROOT = pathlib.Path(r"D:\AI\ds.8\Little_Crew")
TH, MIN_GAIN, MAXR = 65.0, 2.0, 3

rows = []
for f in sorted(glob.glob(str(ROOT / "research" / "script-rounds" / "*.json"))):
    d = json.loads(pathlib.Path(f).read_text(encoding="utf-8"))
    rs = [r.get("total") for r in (d.get("rounds") or []) if r.get("total") is not None]
    if rs:
        rows.append(rs)
p = ROOT / "research" / "retry-cost" / "raw-runs.json"
if p.is_file():
    for r in (json.loads(p.read_text(encoding="utf-8")).get("runs") or []):
        rs = [x.get("total") for x in (r.get("detail") or []) if x.get("total") is not None]
        if rs:
            rows.append(rs)

def sim(scores):
    best, used = scores[0], 1
    if best >= TH:
        return best, used
    for s in scores[1:MAXR]:
        used += 1
        if s > best:
            gain, best = s - best, s
        else:
            gain = s - best
        if best >= TH:
            break
        if gain < MIN_GAIN:
            break
    return best, used

old_pass = sum(1 for s in rows if s[-1] >= TH)
old_rounds = sum(len(s) for s in rows)
new = [sim(s) for s in rows]
new_pass = sum(1 for b, _ in new if b >= TH)
new_rounds = sum(u for _, u in new)
n = len(rows)
print("  n = %d 集，门槛 %.0f 分" % (n, TH))
print()
print("  %-14s %-10s %-8s" % ("策略", "通过率", "总轮次"))
print("  %-14s %2d/%d=%3.0f%%   %d 轮" % ("现状(跑满取最后)", old_pass, n, old_pass/n*100, old_rounds))
print("  %-14s %2d/%d=%3.0f%%   %d 轮" % ("早停+取最优", new_pass, n, new_pass/n*100, new_rounds))
print()
print("  平均轮次: %.2f -> %.2f  (省 %.0f%%)"
      % (old_rounds/n, new_rounds/n, (1-new_rounds/old_rounds)*100))
print("  质量: 通过率 %+.0f 个百分点" % ((new_pass-old_pass)/n*100))
worse = [(i, max(s), b) for i, (s, (b, _)) in enumerate(zip(rows, new)) if b < max(s)]
print("  比「全跑完取最优」更差的集: %d（说明早停确实错过了更好轮）" % len(worse))
for i, mx, b in worse[:5]:
    print("     第%d集  最优 %.1f  早停得 %.1f" % (i, mx, b))
