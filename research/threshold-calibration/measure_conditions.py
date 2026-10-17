"""条件对比实验：短剧本敏感性 + 换题材稳定性。

## 要回答的两个问题

**问题一（短剧本敏感性）**：完整性是"有缺陷的场次数 / 总场次"这个**比例**。
若剧本只有 2 场，缺 1 场就是 50% 不完整 → 分数被压得很低；
而 8 场的剧本缺 1 场只有 12.5%。
**所以同一个绝对缺陷量，在短剧本上会被罚得重得多。**
如果这一点成立，"一个全局阈值"对短剧本就是**系统性不公平**。

**问题二（换题材稳定性）**：`measure_threshold.py` 只测了一份素材
（薰衣草）。若换成另一个题材分布就明显不同，
那么"一个全局阈值"本身是错的设计 —— 应按题材分组，或改为
不依赖题材的度量。

## 三个条件

| 条件 | 素材 | max_scenes | 目的 |
|---|---|---|---|
| **B 短剧本** | 薰衣草（同 A） | **2** | 只改"长度"这一个变量 |
| **C 换题材** | 都市悬疑（新） | 8 | 只改"题材"这一个变量 |
| （A 基准） | 薰衣草 | 8 | 已有 10 次，见 raw.json |

**为什么不重跑 A**：已有 10 次数据，重跑等于重复花同样的钱。
两组新条件各 5 次，够看出系统差异（不是估计精确分位数）。
"""
from __future__ import annotations

import json
import pathlib
import statistics
import sys
import time

sys.path.insert(0, r"D:\AI\ds.8\Little_Crew\src")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from little_crew.agent.crew_demo import DEMO_SOURCE_LAVENDER
from little_crew.agent.crew_llm import DramaturgeLlm, ScreenwriterLlm
from little_crew.agent.crew_stubs import default_crew_stubs
from little_crew.agent.llm import LlmClient
from little_crew.agent.orchestrator import PASS_THRESHOLD, CrewOrchestrator

OUT = pathlib.Path(r"D:\AI\ds.8\Little_Crew\research\threshold-calibration")
N_PER_CONDITION = 5
MAX_ROUNDS = 3

#: 条件 C 用的另一份素材：**都市悬疑**，与薰衣草的"民间传说抒情"完全不同的调性。
#: 选它是因为：场景更碎（室内短场多）、人物关系更紧、冲突更外显 ——
#: 若阈值对题材敏感，这组应当与 A 明显不同。
SOURCE_SUSPENSE = """凌晨两点，第七通电话。

陈默没有接。他把手机屏幕朝下扣在桌上，继续盯着那杯已经凉透的茶。

第八通。他还是没接。

门铃响了。

他走过去，从猫眼里往外看——走廊的灯是坏的，只有应急灯幽幽的绿光。
没有人。但地上放着一个牛皮纸信封，边缘被雨洇湿了一角。

他把信封捡起来，没有拆。先看背面：没有寄件人，只有一行铅笔字：
"你哥出事那天，你其实在家。"

陈默的手指停在封口上。

三年前的那个雨夜，警察问他："你最后见到你哥是什么时候？"
他说："前一天晚上。"

他说了谎。

信封里只有一张照片，底片时代的质感，泛黄。
照片上是两个男孩站在天台上，一个在笑，一个在往下看。
笑的那个是他哥。往下看的那个是他。

背面还有一行字，这次的笔迹在抖：
"你数过吗？那天你挂了几次电话。"

陈默的手开始抖。他记得。他记得自己坐在沙发上，看着来电显示，
一次，两次，三次……他数到了七次。

然后他关了机。

窗外开始下雨。他抬起头，看见对面楼里有一扇窗亮着灯，
一个人影正举着手机，屏幕的光照亮他的下半张脸。

手机又亮了。第九通。
"""


def _build_source(name: str) -> str:
    return {"lavender": DEMO_SOURCE_LAVENDER,
            "suspense": SOURCE_SUSPENSE}[name]


def one_run(idx: int, *, source_name: str, max_scenes: int) -> dict:
    stubs = default_crew_stubs()
    writer = LlmClient("deepseek-v4")
    reviewer = LlmClient("mimo-v2.6-flash")
    w = ScreenwriterLlm(client=writer, max_scenes=max_scenes)
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
        {"source.novel": _build_source(source_name),
         "episodeId": f"E{idx:04d}", "title": source_name})
    wall = time.perf_counter() - t0

    rounds = []
    for r in cyc.rounds:
        scenes_hit = len({i.location.scene for rev in (r.reviews or ())
                          for i in (getattr(rev, "issues", ()) or ())})
        rounds.append({"round": r.round_index,
                       "verdict": r.verdict.value,
                       "total": round(r.total, 2),
                       "scenesWithIssues": scenes_hit,
                       # ⚠️ **必须记作废原因**：0 分有两种完全不同的含义 ——
                       #   "产物不合格"（分数低）与"判定不可信"（作废），
                       # 而只看 0 分分不出来。第一次跑 C 组时漏记了它，
                       # 只能事后猜（候选：无审查意见 / 无维度评分 /
                       # 自报分不自洽 / 同族 / 阻塞问题无指令）。
                       "invalidatedReason": (r.invalidated_reason or "")[:200]})
    # 场景数从最后一轮的稿子里取（它反映实际产出长度）
    n_scenes = 0
    last = cyc.best or (cyc.rounds[-1] if cyc.rounds else None)
    if last is not None and last.draft is not None:
        n_scenes = len(last.draft.scenes)

    return {"idx": idx, "source": source_name, "maxScenes": max_scenes,
            "accepted": cyc.accepted, "rounds": len(cyc.rounds),
            "sceneCount": n_scenes,
            "firstScore": rounds[0]["total"] if rounds else None,
            "finalScore": rounds[-1]["total"] if rounds else None,
            "bestScore": max((r["total"] for r in rounds), default=None),
            "wallSeconds": round(wall, 1), "detail": rounds}


def pct(v: list[float], p: float) -> float:
    if not v:
        return float("nan")
    x = sorted(v)
    return x[max(0, min(len(x) - 1, round(p / 100 * (len(x) - 1))))]


def main() -> int:
    print("=" * 78)
    print(f"条件对比：短剧本（B）vs 换题材（C）—— 各 {N_PER_CONDITION} 次")
    print(f"当前阈值 {PASS_THRESHOLD}")
    print("=" * 78)

    # A 基准：从既有标定里读
    raw_a = json.loads((OUT / "raw.json").read_text(encoding="utf-8"))
    A = {"cond": "A 基准（薰衣草 / 8 场上限）",
         "best": [max(d["total"] for d in r["detail"]) for r in raw_a if r["detail"]],
         "last": [r["finalScore"] for r in raw_a if r["finalScore"] is not None],
         "scenes": [], "accepted": sum(1 for r in raw_a if r["accepted"])}
    print(f"  A 基准：读已有 {len(raw_a)} 次")

    results = []
    for cond, src, ms in (("B 短剧本（薰衣草 / 2 场上限）", "lavender", 2),
                          ("C 换题材（都市悬疑 / 8 场上限）", "suspense", 8)):
        print()
        print(f"  ── {cond}")
        rows = []
        for i in range(1, N_PER_CONDITION + 1):
            print(f"     [{i}/{N_PER_CONDITION}]…", flush=True)
            try:
                rec = one_run(900 + i, source_name=src, max_scenes=ms)
            except Exception as e:                          # noqa: BLE001
                print(f"        ❌ {type(e).__name__}: {e}")
                continue
            rows.append(rec)
            print(f"        {'✅' if rec['accepted'] else '⛔'} "
                  f"轮 {rec['rounds']} 场 {rec['sceneCount']} "
                  f"首 {rec['firstScore']} 末 {rec['finalScore']}")
        results.extend(rows)
        (OUT / "conditions.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    # 分组对比
    groups: dict[str, dict] = {"A": A}
    for tag, src in (("B", "lavender"), ("C", "suspense")):
        rows = [r for r in results
                if r["source"] == src
                and (r["maxScenes"] == 2 if tag == "B" else r["maxScenes"] == 8)]
        groups[tag] = {"cond": next(r["source"] for r in rows) if rows else src,
                       "best": [r["bestScore"] for r in rows if r["bestScore"]],
                       "last": [r["finalScore"] for r in rows if r["finalScore"]],
                       "scenes": [r["sceneCount"] for r in rows],
                       "accepted": sum(1 for r in rows if r["accepted"])}

    print()
    print("=" * 78)
    print("  分组对比（权威总分，按「取最好一轮」口径）")
    print("=" * 78)
    print(f"  {'组':>2}  {'n':>3} {'场景数':>7} {'均值':>7} {'σ':>6} "
          f"{'p50':>7} {'范围':>14} {'过线':>6}")
    print("  " + "-" * 68)
    names = {"A": "A 基准", "B": "B 短剧本", "C": "C 换题材"}
    for tag in ("A", "B", "C"):
        g = groups[tag]
        v = g["best"]
        if not v:
            continue
        sc = g["scenes"]
        sc_txt = (f"{statistics.mean(sc):.1f}" if sc else "—")
        print(f"  {tag:>2}  {len(v):>3} {sc_txt:>7} "
              f"{statistics.mean(v):>7.1f} "
              f"{(statistics.stdev(v) if len(v) > 1 else 0):>6.1f} "
              f"{pct(v, 50):>7.1f} "
              f"{f'[{min(v):.1f},{max(v):.1f}]':>14} "
              f"{g['accepted']}/{len(v):>3}")

    print()
    print("=" * 78)
    print("  结论")
    print("=" * 78)
    ab = [abs(statistics.mean(groups[t]["best"]) -
              statistics.mean(groups["A"]["best"])) for t in ("B", "C")
          if groups[t]["best"]]
    print(f"  B 与 A 的均值差: "
          f"{statistics.mean(groups['B']['best']) - statistics.mean(groups['A']['best']):+.1f}")
    print(f"  C 与 A 的均值差: "
          f"{statistics.mean(groups['C']['best']) - statistics.mean(groups['A']['best']):+.1f}")
    if groups["B"]["scenes"] and groups["A"]["scenes"]:
        print(f"  B 的场景数: {groups['B']['scenes']}（上限被设成 2）")
    print()
    print("  判据：")
    print("    · B 明显低于 A -> **短剧本被比例公式系统性惩罚**，全局阈值不公平")
    print("    · C 明显不同于 A -> **题材间分布不同**，全局阈值本身是错的设计")
    (OUT / "conditions-summary.json").write_text(
        json.dumps({k: {kk: vv for kk, vv in v.items()}
                    for k, v in groups.items()},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
