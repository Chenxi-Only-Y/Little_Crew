"""量 `cast_consistent_with_text` 的**真实发生率** —— 决定要不要把它从 `audit` 升 `hard`。

## 为什么这个测量可以跳过出图

这条断言只看**镜头元数据**（`characters` + `description`），
它们由**分镜阶段**产出，与图无关。

所以本脚本只跑：

    剧本闭环 -> 分镜 -> 逐镜应用断言

**不出图** —— 单集成本从天级降到分钟级，样本量才能上去。

> 代价要说清：这样量到的是"**文本层**的发生率"。
> 若将来把断言换成看图（如人数检测），发生率会不同，那时要重新量。
> 本脚本量的东西与门禁里跑的东西**是同一个算子、同一份输入** ——
> 这点必须成立，否则测的就不是同一件事。
> （`_inputs_for` 复制的是 `episode.py::_run_gate` 里的 `context` 构造，见下。）

## 关键区分：两种"命中"

只报"总命中率"是不够的 —— 它把两件不同的事混在一起：

| 量 | 含义 | 对升 `hard` 的意义 |
|---|---|---|
| **有"独自"标记的镜头数** | 分母：这类镜头本来就不多 | 若分母很小，命中数再高也不影响大局 |
| **其中角色表 > 1 的** | **真违规** | 这才是 `hard` 会拦下的镜头比例 |

所以下面同时报"分母"和"违规/分母"。
"""
from __future__ import annotations

import json
import pathlib
import sys
import time
from collections import Counter

sys.path.insert(0, r"D:\AI\ds.8\Little_Crew\src")
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from little_crew.agent.crew_llm import DramaturgeLlm, ScreenwriterLlm
from little_crew.agent.crew_stubs import default_crew_stubs
from little_crew.agent.llm import LlmClient
from little_crew.agent.orchestrator import CrewOrchestrator
from little_crew.agent.storyboard_llm import StoryboardArtistLlm
from little_crew.review.ops import cast_consistent_with_text

OUT = pathlib.Path(r"D:\AI\ds.8\Little_Crew\research\cast-consistency")
OUT.mkdir(parents=True, exist_ok=True)

#: 每份素材跑几次
RUNS_PER_SOURCE = 2

#: 素材：**不同题材**，因为分镜风格会随题材变（悬疑短场多、抒情长镜多）。
SOURCES: dict[str, str] = {
    "薰衣草": """山脚下有一片薰衣草田，是祖母留下的。
少女每天清晨去田里采花，编成花束，挑到镇上的集市卖。
那天她在田边遇见一个陌生人，旅人说他在找一个人，
一个三十年前离开这座山、再没回来的人。
少女回家问母亲，母亲沉默了很久，才说：那个人是你父亲。
夜里，少女在祖母的旧木箱里翻出一张照片——
照片上，祖母身边站着一个年轻人，眉眼和旅人一模一样。""",

    "都市悬疑": """凌晨两点，第七通电话。陈默没有接。
他把手机屏幕朝下扣在桌上，继续盯着那杯已经凉透的茶。
第八通。他还是没接。门铃响了。
他走过去，从猫眼里往外看——走廊的灯是坏的，只有应急灯幽幽的绿光。
没有人。但地上放着一个牛皮纸信封，边缘被雨洇湿了一角。
信封里只有一张照片，底片时代的质感，泛黄。
照片上是两个男孩站在天台上，一个在笑，一个在往下看。
笑的那个是他哥。往下看的那个是他。""",

    "山野奇谭": """伐木人老赵在雾灵山深处发现一口井。
井沿上刻着一圈字，他不认得，但摸上去很新，像刚刻的。
他探头往下看，井里没有水，只有一片天。
天上有云在走，还有一只鹰，转了三圈才不见。
老赵直起身，忽然觉得背后有人。
他回头。什么都没有，只有他自己的脚印，从林子那头一直通到这里。
可是他明明是今天才来的，脚印刷新得像刚踩下的。""",
}


def _inputs_for(shot: object) -> tuple[dict[str, object], dict[str, object]]:
    """构造 (expected, observed)，**与门禁里的完全一致**。

    这两行必须与 `episode.py::_run_gate` 里 `context[s.shot_id]` 的构造
    一模一样 —— 否则量到的不是门禁里跑的那件事。
    （那边写的是：
        "characters": list(getattr(s, "characters", ()) or ()),
        "description": str(getattr(s, "description", "") or "")）
    """
    return {}, {
        "characters": list(getattr(shot, "characters", ()) or ()),
        "description": str(getattr(shot, "description", "") or ""),
    }


def one_run(source_name: str, source: str, idx: int) -> dict:
    stubs = default_crew_stubs()
    writer = LlmClient("deepseek-v4", timeout_s=120)
    reviewer = LlmClient("mimo-v2.6-flash", timeout_s=120)
    stubs["screenwriter"] = ScreenwriterLlm(client=writer)
    stubs["dramaturge"] = DramaturgeLlm(client=reviewer)
    stubs["storyboard_artist"] = StoryboardArtistLlm(client=writer)
    orch = CrewOrchestrator(
        max_rounds=3,
        model_families={"screenwriter": "deepseek",
                        "storyboard_artist": "deepseek",
                        "dramaturge": "mimo",
                        "story_editor": "deepseek", "review_board": "mimo"})
    orch.bind_many(stubs)

    t0 = time.perf_counter()
    cyc = orch.run_script_cycle(
        {"source.novel": source, "episodeId": f"M{idx:03d}",
         "title": source_name})
    # 剧本没过也用**最好那一轮**继续 —— 本轮要的是镜头文本，
    # 不是"剧本能不能过线"（那是另一个问题）。
    draft = cyc.final or (cyc.best.draft if cyc.best else None)
    if draft is None:
        return {"source": source_name, "ok": False, "why": cyc.reason[:120]}

    out = orch.run_stage("storyboard_artist", "shot.list",
                         {"script.final": draft})
    if not out.ok:
        return {"source": source_name, "ok": False, "why": out.error[:120]}

    shots = tuple(orch.blackboard.value("shot.list").shots)
    rows = []
    for s in shots:
        exp, obs = _inputs_for(s)
        r = cast_consistent_with_text(exp, obs)
        desc = str(obs["description"])
        rows.append({
            "shotId": s.shot_id, "shotSize": s.shot_size,
            "characters": obs["characters"], "nChars": len(obs["characters"]),
            "description": desc[:200],
            "passed": bool(r.passed),
            "direction": (r.observed or {}).get("direction", ""),
            "soloMarkers": list((r.observed or {}).get("soloMarkers") or ()),
            "detail": r.detail[:200],
        })
    return {"source": source_name, "ok": True,
            "scriptAccepted": cyc.accepted,
            "scriptRounds": len(cyc.rounds),
            "shotCount": len(shots),
            "wallSeconds": round(time.perf_counter() - t0, 1),
            "shots": rows}


def main() -> int:
    print("=" * 80)
    print(f"cast_consistent_with_text 真实发生率 —— {len(SOURCES)} 份素材 × {RUNS_PER_SOURCE} 次")
    print("（只跑剧本+分镜，**不出图**）")
    print("=" * 80)

    runs: list[dict] = []
    idx = 0
    for name, src in SOURCES.items():
        for k in range(RUNS_PER_SOURCE):
            idx += 1
            print(f"\n  [{idx}] {name} 第 {k+1} 次…", end=" ", flush=True)
            try:
                rec = one_run(name, src, idx)
            except Exception as e:                              # noqa: BLE001
                print(f"❌ {type(e).__name__}: {e}")
                continue
            if not rec.get("ok"):
                print(f"⛔ {rec.get('why')}")
                runs.append(rec)
                continue
            print(f"✅ 剧本 {rec['scriptRounds']} 轮"
                  f"{'（通过）' if rec['scriptAccepted'] else '（未过，用最好稿）'}"
                  f"  分镜 {rec['shotCount']} 镜  {rec['wallSeconds']}s")
            runs.append(rec)
            (OUT / "runs.partial.json").write_text(
                json.dumps(runs, ensure_ascii=False, indent=2), encoding="utf-8")

    good = [r for r in runs if r.get("ok")]
    shots = [s for r in good for s in r["shots"]]
    total = len(shots)
    if not total:
        print("\n  没有有效样本")
        return 1

    # ---- 分母：描述里出现"独自"类标记的镜头 ----
    solo = [s for s in shots if s["soloMarkers"]]
    solo_bad = [s for s in solo if not s["passed"] and s["direction"] == "solo-vs-many"]
    expl = [s for s in shots if s["direction"] == "explicit-count"]
    allbad = [s for s in shots if not s["passed"]]

    print()
    print("=" * 80)
    print("  结果")
    print("=" * 80)
    print(f"  有效集数        {len(good)}")
    print(f"  总镜头          **{total}**")
    print(f"  " + "-" * 70)
    print(f"  ① 有「独自」标记的镜头   {len(solo)}  "
          f"（占全部 {len(solo)/total*100:.1f}%）")
    print(f"  ② 其中「角色表 > 1」     **{len(solo_bad)}**  "
          f"→ 占①的 {len(solo_bad)/max(len(solo),1)*100:.1f}%")
    print(f"  ③ 显式人数矛盾           {len(expl)}")
    print(f"  " + "-" * 70)
    print(f"  **升 hard 会拦下的镜头**  {len(allbad)}/{total} = "
          f"**{len(allbad)/total*100:.1f}%**")

    # ---- 按素材看 ----
    by_src = Counter()
    by_src_tot = Counter()
    for r in good:
        for s in r["shots"]:
            by_src_tot[r["source"]] += 1
            if not s["passed"]:
                by_src[r["source"]] += 1
    print()
    print(f"  {'素材':<10} {'镜头':>5} {'命中':>5} {'占比':>7}")
    for s_name in SOURCES:
        if by_src_tot[s_name]:
            print(f"  {s_name:<10} {by_src_tot[s_name]:>5} {by_src[s_name]:>5} "
                  f"{by_src[s_name]/by_src_tot[s_name]*100:>6.1f}%")

    # ---- 命中样例 ----
    if allbad:
        print()
        print("  命中样例（最多 5 条）：")
        for s in allbad[:5]:
            print(f"    {s['shotId']}  {s['nChars']} 人 {s['characters']}")
            print(f"      {s['description'][:90]}")

    # ---- 结论 ----
    print()
    print("=" * 80)
    print("  怎么读这个数（决定升不升 hard）")
    print("=" * 80)
    rate = len(allbad) / total
    if rate < 0.03:
        print(f"  命中率 {rate*100:.1f}% —— 很低，可以升 hard（拦下的很少）")
    elif rate < 0.10:
        print(f"  命中率 {rate*100:.1f}% —— 可接受，升 hard 后每 10~30 镜拦一次")
    else:
        print(f"  ⚠️ 命中率 {rate*100:.1f}% —— 偏高。升 hard 会让约 1/{int(1/rate)} 镜被拦，")
        print(f"      **先查为什么**：是分镜师总把全场角色塞进每镜，还是文本真在自相矛盾？")
        print(f"      前者该修提示词/桥接，后者才该靠门禁拦。")

    summary = {
        "nSources": len(SOURCES), "runsPerSource": RUNS_PER_SOURCE,
        "validRuns": len(good), "totalShots": total,
        "soloMarkedShots": len(solo),
        "soloViolations": len(solo_bad),
        "explicitCountMismatches": len(expl),
        "allViolations": len(allbad),
        "incidenceRate": round(rate, 4),
        "soloIncidence": round(len(solo_bad) / max(len(solo), 1), 4),
        "bySource": {k: {"shots": by_src_tot[k], "fired": by_src[k]}
                     for k in SOURCES if by_src_tot[k]},
        "examples": [{k: s[k] for k in ("shotId", "characters", "description", "detail")}
                     for s in allbad[:10]],
    }
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "runs.json").write_text(
        json.dumps(runs, ensure_ascii=False, indent=2), encoding="utf-8")
    partial = OUT / "runs.partial.json"
    if partial.exists():
        partial.unlink()
    print(f"\n  数据写入 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
