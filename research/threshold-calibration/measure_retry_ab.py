"""A/B 验证：加了传输层重试后，"整轮作废"还出现吗？

## 对照

改之前（`measure_conditions.py` 的 C 组，5 次）：
  **2/5 整轮作废、分数 0.0**（评审调用失败）
  -> 通过率 3/5，均值 42.0，σ 38.4

现在同一个条件（都市悬疑 / max_scenes=8）再跑 5 次，看：
  · 作废率降没降
  · σ 是否收紧

## 注意

样本都只有 5，**不足以做统计检验**。这只是一次**方向性**验证：
如果作废从 2/5 降到 0/5，说明重试确实挡住了那类失败；
如果仍是 2/5，说明失败不是"一次抖动"能救回来的（比如配额/限流）。
"""
from __future__ import annotations

import json
import pathlib
import statistics
import sys

sys.path.insert(0, r"D:\AI\ds.8\Little_Crew\src")
sys.path.insert(0, r"D:\AI\ds.8\Little_Crew\research\threshold-calibration")
sys.stdout.reconfigure(encoding="utf-8", errors="replace",
                       line_buffering=True)

from little_crew.agent.crew_llm import DramaturgeLlm, ScreenwriterLlm
from little_crew.agent.crew_stubs import default_crew_stubs
from little_crew.agent.llm import LlmClient
from little_crew.agent.orchestrator import CrewOrchestrator
from measure_conditions import SOURCE_SUSPENSE

OUT = pathlib.Path(r"D:\AI\ds.8\Little_Crew\research\threshold-calibration")
N = 5

rows = []
print(f"  {'#':>2} {'轮':>3} {'场':>3} {'各轮分数':>24} {'最好':>7} "
      f"{'重试':>5} {'通过':>4}  作废原因")
print("  " + "-" * 92)
for a in range(1, N + 1):
    stubs = default_crew_stubs()
    wr = LlmClient("deepseek-v4", timeout_s=90, max_retries=1)
    rv = LlmClient("mimo-v2.6-flash", timeout_s=90, max_retries=1)
    stubs["screenwriter"] = ScreenwriterLlm(client=wr)
    stubs["dramaturge"] = DramaturgeLlm(client=rv)
    orch = CrewOrchestrator(
        max_rounds=3,
        model_families={"screenwriter": "deepseek",
                        "storyboard_artist": "deepseek",
                        "dramaturge": "mimo",
                        "story_editor": "deepseek", "review_board": "mimo"})
    orch.bind_many(stubs)
    cyc = orch.run_script_cycle(
        {"source.novel": SOURCE_SUSPENSE, "episodeId": f"AB{a}", "title": "悬疑"})
    scores = [r.total for r in cyc.rounds]
    best = cyc.best
    nsc = len(best.draft.scenes) if (best and best.draft) else 0
    why = next((r.invalidated_reason for r in cyc.rounds
                if r.invalidated_reason), "")
    retries = wr.retries + rv.retries
    rows.append({"run": a, "rounds": len(cyc.rounds), "scenes": nsc,
                 "scores": scores,
                 "best": max(scores) if scores else 0.0,
                 "accepted": cyc.accepted, "retries": retries,
                 "invalidated": why})
    (OUT / "retry-ab.partial.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  {a:>2} {len(cyc.rounds):>3} {nsc:>3} "
          f"{' -> '.join(f'{s:.1f}' for s in scores):>24} "
          f"{(max(scores) if scores else 0):>7.1f} "
          f"{retries:>5} {'✅' if cyc.accepted else '⛔':>4}  {why[:40]}")

bests = [r["best"] for r in rows]
zeros = sum(1 for r in rows if r["best"] == 0.0)
pass_n = sum(1 for r in rows if r["accepted"])
tot_retries = sum(r["retries"] for r in rows)

print()
print("=" * 78)
print("  对照")
print("=" * 78)
print(f"  {'':20} {'改前':>14} {'改后（有重试）':>16}")
print(f"  {'整轮作废（0 分）':20} {'2/5':>14} {f'{zeros}/{N}':>16}")
print(f"  {'通过':20} {'3/5':>14} {f'{pass_n}/{N}':>16}")
print(f"  {'均值（取最好）':20} {42.0:>14.1f} {statistics.mean(bests):>16.1f}")
print(f"  {'σ':20} {38.4:>14.1f} "
      f"{(statistics.stdev(bests) if len(bests) > 1 else 0):>16.1f}")
print(f"  {'累计重试次数':20} {'—':>14} {tot_retries:>16}")
print()
if zeros == 0 and tot_retries > 0:
    print("  ✅ 作废清零，且重试确实发生过 —— **重试挡住了那类失败**")
elif zeros == 0:
    print("  ⚠️ 作废清零，但**一次重试都没触发** —— 这次只是运气好，不能归因于重试")
elif zeros < 2:
    print("  ⚠️ 作废减少但未清零 —— 重试有帮助，但不是全部")
else:
    print("  ❌ 作废没有减少 —— 失败不是'一次抖动'能救的（查配额/限流）")

(OUT / "retry-ab.json").write_text(
    json.dumps({"n": N, "rows": rows,
                "zeros": zeros, "accepted": pass_n,
                "retries": tot_retries,
                "before": {"zeros": 2, "n": 5, "mean": 42.0, "std": 38.4}},
               ensure_ascii=False, indent=2), encoding="utf-8")
print(f"  数据写入 {OUT / 'retry-ab.json'}")
