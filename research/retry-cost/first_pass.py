"""首轮通过率：如果不允许返工，有多少集能直接交付？

数据来源（两处都扫）：
  research/script-rounds/*.json   新格式，带每轮 issues
  research/retry-cost/raw-runs.json  旧格式，只有分数

判据：第一轮 verdict 是否为 PASS（允许返工的链路里，第一轮通过就不用返工）。
"""
import glob
import json
import pathlib
import statistics as st
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = pathlib.Path(r"D:\AI\ds.8\Little_Crew")
ROWS = []

for f in sorted(glob.glob(str(ROOT / "research" / "script-rounds" / "*.json"))):
    d = json.loads(pathlib.Path(f).read_text(encoding="utf-8"))
    rounds = d.get("rounds") or []
    if not rounds:
        continue
    first = str(rounds[0].get("verdict") or "")
    ROWS.append({"id": d.get("episodeId") or pathlib.Path(f).stem,
                 "kind": "new", "n": len(rounds),
                 "firstPass": "PASS" in first.upper(),
                 "first": rounds[0].get("total"),
                 "last": rounds[-1].get("total"),
                 "accepted": d.get("accepted")})

p = ROOT / "research" / "retry-cost" / "raw-runs.json"
if p.is_file():
    d = json.loads(p.read_text(encoding="utf-8"))
    for r in (d.get("runs") or []):
        det = r.get("detail") or []
        if not det:
            continue
        first = str(det[0].get("verdict") or "")
        ROWS.append({"id": "legacy-%s" % r.get("run"), "kind": "old",
                     "n": len(det), "firstPass": "PASS" in first.upper(),
                     "first": det[0].get("total"),
                     "last": det[-1].get("total"),
                     "accepted": r.get("accepted")})

n = len(ROWS)
fp = sum(1 for x in ROWS if x["firstPass"])
print("  可统计的集数: %d（新格式 %d / 旧格式 %d）"
      % (n, sum(1 for x in ROWS if x["kind"] == "new"),
         sum(1 for x in ROWS if x["kind"] == "old")))
print()
print("  ★ 首轮通过率: %d/%d = %.0f%%" % (fp, n, fp / max(1, n) * 100))
print("    即：**不允许返工的话，只有 %.0f%% 的集会直接交付成功**"
      % (fp / max(1, n) * 100))
print()
acc = [x for x in ROWS if x["accepted"]]
print("  允许返工后最终接受: %d/%d = %.0f%%"
      % (len(acc), n, len(acc) / max(1, n) * 100))
print()
multi = [x for x in ROWS if x["n"] > 1]
if multi:
    print("  需要返工的 %d 集：最终接受 %d 集（%.0f%%）"
          % (len(multi), sum(1 for x in multi if x["accepted"]),
             sum(1 for x in multi if x["accepted"]) / len(multi) * 100))
dist = {}
for x in ROWS:
    dist[x["n"]] = dist.get(x["n"], 0) + 1
print("  轮次分布:", dict(sorted(dist.items())))
