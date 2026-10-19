"""阈值标定：跑 N 次剧本闭环，收集分数分布。

## 为什么要这个

`docs/00` §5.10.8 的诚实话：**总分本身有噪声** —— 问题列表由 LLM 生成、
每次不同，所以总分不可复现。而 `PASS_THRESHOLD=80` 现在只有 **2 个样本**
（81.1 / 77.8），**切在噪声带中间**。

**没有分布就定阈值是拍脑袋。** 本脚本收集分布，让下一个人有据可依。

## 记录什么

每次运行逐轮记录：
  · 权威总分（0~100）
  · 裁定（PASS / REVISE / ESCALATE）
  · 阻断级 / advisory 意见数
  · 命中的场次数（完整性公式的分子）
  · 每次调用的 token（算成本）

并统计：
  · **首轮分数**分布（"一次就写好"有多难）
  · **末轮分数**分布（3 轮后能到多好）← **这才是阈值该看的东西**
  · 收敛率（几次能在 3 轮内通过）

## 怎么用这个结果

看**末轮分数的分位数**：
  · 阈值定在 p50 附近 -> 约一半的题材能过
  · 阈值定在 p75 附近 -> 只让较好的过
**选哪个取决于"我们是想要产量还是想要质量"** —— 那是产品决策，不是数据能回答的。
数据的作用是**让那个决策有依据**。
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, r"D:\AI\ds.8\Little_Crew\src")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from little_crew.agent.crew_demo import DEMO_SOURCE_LAVENDER
from little_crew.agent.crew_llm import DramaturgeLlm, ScreenwriterLlm
from little_crew.agent.crew_stubs import default_crew_stubs
from little_crew.agent.llm import LlmClient
from little_crew.agent.orchestrator import PASS_THRESHOLD, CrewOrchestrator

#: 跑几次。10 次能看出分布形状，又不至于太贵。
N_RUNS = 10
#: 与生产一致的轮次上限
MAX_ROUNDS = 3
OUT = Path(r"D:\AI\ds.8\Little_Crew\research\threshold-calibration")
OUT.mkdir(parents=True, exist_ok=True)


def one_run(idx: int) -> dict:
    """跑一次剧本闭环，返回该次的结构化记录。"""
    stubs = default_crew_stubs()
    writer = LlmClient("deepseek-flash")
    reviewer = LlmClient("mimo-v2.6-flash")
    w = ScreenwriterLlm(client=writer)
    d = DramaturgeLlm(client=reviewer)
    stubs["screenwriter"] = w
    stubs["dramaturge"] = d

    orch = CrewOrchestrator(
        max_rounds=MAX_ROUNDS,
        model_families={"screenwriter": "deepseek",
                        "storyboard_artist": "deepseek",
                        "dramaturge": "mimo",
                        "story_editor": "deepseek",
                        "review_board": "mimo"})
    orch.bind_many(stubs)

    t0 = time.perf_counter()
    cyc = orch.run_script_cycle(
        {"source.novel": DEMO_SOURCE_LAVENDER,
         "episodeId": f"E{900 + idx:03d}", "title": "薰衣草"})
    wall = time.perf_counter() - t0

    rounds = []
    for r in cyc.rounds:
        blocker = sum(1 for rev in (r.reviews or ())
                      for i in (getattr(rev, "issues", ()) or ())
                      if i.severity.value == "blocker")
        adv = sum(1 for rev in (r.reviews or ())
                  for i in (getattr(rev, "issues", ()) or ())
                  if i.severity.value != "blocker")
        scenes_hit = len({i.location.scene for rev in (r.reviews or ())
                          for i in (getattr(rev, "issues", ()) or ())})
        rounds.append({"round": r.round_index,
                       "verdict": r.verdict.value,
                       "total": round(r.total, 2),
                       "blocker": blocker,
                       "advisory": adv,
                       "scenesWithIssues": scenes_hit})

    def _tok(c: LlmClient) -> dict:
        ins = sum(int((e.get("usage") or {}).get("prompt_tokens") or 0)
                  for e in c.call_log)
        outs = sum(int((e.get("usage") or {}).get("completion_tokens") or 0)
                   for e in c.call_log)
        return {"calls": c.calls, "in": ins, "out": outs}

    return {
        "run": idx,
        "accepted": cyc.accepted,
        "rounds": len(cyc.rounds),
        "firstScore": rounds[0]["total"] if rounds else None,
        "finalScore": rounds[-1]["total"] if rounds else None,
        "reason": cyc.reason[:120],
        "wallSeconds": round(wall, 1),
        "detail": rounds,
        "tokens": {"writer": _tok(writer), "reviewer": _tok(reviewer)},
    }


def _pct(vals: list[float], p: float) -> float:
    """分位数（最近秩法，样本少时更直观）。"""
    if not vals:
        return float("nan")
    s = sorted(vals)
    k = max(0, min(len(s) - 1, round(p / 100.0 * (len(s) - 1))))
    return s[k]


def main() -> int:
    print("=" * 78)
    print(f"阈值标定：跑 {N_RUNS} 次剧本闭环（每集最多 {MAX_ROUNDS} 轮）")
    print(f"当前阈值 PASS_THRESHOLD = {PASS_THRESHOLD}")
    print("=" * 78)
    print()

    runs: list[dict] = []
    for i in range(1, N_RUNS + 1):
        print(f"  [{i}/{N_RUNS}] 跑第 {i} 次…", flush=True)
        try:
            rec = one_run(i)
        except Exception as e:                              # noqa: BLE001
            print(f"     ❌ {type(e).__name__}: {e}")
            continue
        runs.append(rec)
        print(f"     {'✅ 通过' if rec['accepted'] else '⛔ 未过'}  "
              f"轮数 {rec['rounds']}  "
              f"首轮 {rec['firstScore']}  末轮 {rec['finalScore']}  "
              f"{rec['wallSeconds']}s")
        # 增量落盘 —— 中途崩了也不丢数据
        (OUT / "raw.json").write_text(
            json.dumps(runs, ensure_ascii=False, indent=2), encoding="utf-8")

    if not runs:
        print("  没有有效运行")
        return 1

    first = [r["firstScore"] for r in runs if r["firstScore"] is not None]
    final = [r["finalScore"] for r in runs if r["finalScore"] is not None]
    passed = sum(1 for r in runs if r["accepted"])
    tin = sum(r["tokens"]["writer"]["in"] + r["tokens"]["reviewer"]["in"]
              for r in runs)
    tout = sum(r["tokens"]["writer"]["out"] + r["tokens"]["reviewer"]["out"]
               for r in runs)
    calls = sum(r["tokens"]["writer"]["calls"] + r["tokens"]["reviewer"]["calls"]
                for r in runs)

    summary = {
        "nRuns": len(runs),
        "maxRounds": MAX_ROUNDS,
        "thresholdAtTimeOfRun": PASS_THRESHOLD,
        "accepted": passed,
        "convergenceRate": round(passed / len(runs), 3),
        "firstScore": {
            "values": first,
            "mean": round(statistics.mean(first), 2),
            "min": min(first), "max": max(first),
            "p25": _pct(first, 25), "p50": _pct(first, 50),
            "p75": _pct(final, 75),
            "stdev": round(statistics.stdev(first), 2) if len(first) > 1 else 0.0,
        },
        "finalScore": {
            "values": final,
            "mean": round(statistics.mean(final), 2),
            "min": min(final), "max": max(final),
            "p25": _pct(final, 25), "p50": _pct(final, 50),
            "p75": _pct(final, 75),
            "stdev": round(statistics.stdev(final), 2) if len(final) > 1 else 0.0,
        },
        "roundsDistribution": {str(k): sum(1 for r in runs if r["rounds"] == k)
                               for k in sorted({r["rounds"] for r in runs})},
        "tokens": {"calls": calls, "in": tin, "out": tout,
                   "total": tin + tout},
    }
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print("=" * 78)
    print(f"  完成 {len(runs)} 次")
    print("=" * 78)
    print(f"  收敛率         {passed}/{len(runs)} "
          f"({summary['convergenceRate'] * 100:.0f}%)")
    print(f"  轮数分布       {summary['roundsDistribution']}")
    print()
    for name, d in (("首轮分数", summary["firstScore"]),
                    ("末轮分数", summary["finalScore"])):
        print(f"  {name}")
        print(f"    均值 {d['mean']:.1f}  σ {d['stdev']:.1f}  "
              f"范围 [{d['min']:.1f}, {d['max']:.1f}]")
        print(f"    p25 {d['p25']:.1f}  p50 {d['p50']:.1f}  p75 {d['p75']:.1f}")
    print()
    print(f"  token: {summary['tokens']['total']} "
          f"(in {tin} / out {tout})  ·  {calls} 次调用")
    print(f"  结果写入 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
