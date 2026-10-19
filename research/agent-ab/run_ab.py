"""按 `DESIGN.md` 跑「多智能体 vs 单智能体」对照实验。

## 一个关键设计：两组用**同一个实现**

```python
A = StoryboardArtistLlm(client=c, plan=None)          # 单智能体
B = StoryboardArtistLlm(client=c, plan=导演的预算)     # 多智能体
```

**A 组不阉割，是结构上保证的** —— 不是我写提示词时自觉不看错。
两者用的是同一份 system prompt、同一份硬规则说明、同一份封闭词表、
同一个 `max_shots`。**唯一差别是"导演那一步有没有发生"。**

若另写一个"弱提示词"当 A 组，B 赢了也说明不了任何事 ——
那只是在证明"提示词写细一点更好"。

## 判据（全部机械，见 DESIGN.md §3）

| 判据 | 来源 |
|---|---|
| ① 硬规则违规数 | `_to_shots()` 返回的 `problems`（项目已有校验器） |
| ② 预算偏差 | 逐场 `|实际镜数 − 预算|` 之和 / 场数 |
| ③ 镜头多样性 | 景别分布熵 + 每场镜数方差 |

## 用法

```
python research/agent-ab/run_ab.py            # 全量（6 剧本 × 3 重复）
python research/agent-ab/run_ab.py --quick    # 1 剧本 × 1 重复，先验通路
```
"""
from __future__ import annotations

import json
import math
import pathlib
import statistics
import sys
import time

sys.path.insert(0, r"D:\AI\ds.8\Little_Crew\src")
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from little_crew.agent.director_llm import DirectorLlm, normalize_plan
from little_crew.agent.llm import LlmClient
from little_crew.agent.messages import ScriptDraft
from little_crew.agent.protocol import Budget, CallEnvelope
from little_crew.agent.storyboard_llm import LEGAL_VOCAB, StoryboardArtistLlm

OUT = pathlib.Path(r"D:\AI\ds.8\Little_Crew\research\agent-ab")
OUT.mkdir(parents=True, exist_ok=True)

MODEL = "deepseek-v4"
REPEATS = 3
MAX_SHOTS = 40


# ---------------------------------------------------------------- 剧本
def _draft(eid: str, title: str, scenes: list[tuple[str, str, tuple[str, ...]]]) -> ScriptDraft:
    return ScriptDraft(
        frm="screenwriter", to="storyboard_artist", episode_id=eid,
        title=title, revision=1,
        scenes=tuple({"scene_id": f"S{i+1:02d}", "heading": h,
                      "characters": c, "text": t}
                     for i, (h, t, c) in enumerate(scenes)))


#: 6 个样本：短 2 / 中 2 / 长 2，题材不同（见 DESIGN.md §5）
SCRIPTS: dict[str, ScriptDraft] = {
    "短·雨夜": _draft("A1", "雨夜", [
        ("内景·夜", "少女坐在窗前，雨下了一整夜。她手里捏着一封没写完的信。", ("少女",)),
        ("外景·晨", "天亮了，她把信塞进邮筒，转身走进雾里。", ("少女",)),
    ]),
    "短·集市": _draft("A2", "集市", [
        ("外景·日", "老人在集市上摆摊卖花，一整天没卖出去一枝。", ("老人",)),
        ("外景·黄昏", "收摊时，一个小女孩用一颗糖换走了一枝薰衣草。", ("老人", "女孩")),
    ]),
    "中·守夜": _draft("A3", "守夜", [
        ("内景·夜", "少女守着发烧的旅人，窗外的雪压弯了树枝。", ("少女", "旅人")),
        ("内景·深夜", "她换了三次湿毛巾，火快灭了，她添了一把柴。", ("少女",)),
        ("内景·黎明", "旅人退烧了，睁开眼，看见她趴在桌边睡着了。", ("少女", "旅人")),
        ("外景·晨", "雪停了，旅人留下一枚铜扣，独自下山。", ("旅人",)),
    ]),
    "中·信": _draft("A4", "信", [
        ("内景·日", "邮差把一封退回的信交到她手上，信封上写着'查无此人'。", ("她", "邮差")),
        ("内景·日", "她拆开信，读了三遍，然后把信折好放进抽屉最里层。", ("她",)),
        ("外景·黄昏", "她走到桥上，把抽屉钥匙丢进河里。", ("她",)),
        ("内景·夜", "她坐在黑暗里，桌上摊着那封信的复印件。", ("她",)),
    ]),
    "长·薰衣草": _draft("A5", "薰衣草传说", [
        ("外景·春", "少女在田边发现一个受伤的旅人，倒在薰衣草丛里。", ("少女", "旅人")),
        ("内景·夜", "她把他拖回屋，撕下披肩给他包扎，血浸透了布条。", ("少女", "旅人")),
        ("内景·夜", "父亲说：他是外乡人。你留他，村里人怎么看你。", ("少女", "父亲")),
        ("外景·晨", "旅人能站起来了，他说他要往北走。", ("少女", "旅人")),
        ("内景·日", "老奶奶塞给她一束薰衣草，说：让不洁的东西现形。", ("少女", "老奶奶")),
        ("外景·黄昏", "她追到田边，把花束掷向他，花散了一地。", ("少女", "旅人")),
        ("外景·黄昏", "旅人在花雨里化作一缕烟，风一吹就散了。", ("旅人",)),
    ]),
    "长·空屋": _draft("A6", "空屋", [
        ("外景·晨", "搬家公司的车停在楼下，她最后一次走上那截楼梯。", ("她",)),
        ("内景·日", "屋子空了，墙上留着家具压出的印子，像几块补丁。", ("她",)),
        ("内景·日", "她在厨房的水槽下发现一个铁盒，锈得打不开。", ("她",)),
        ("内景·日", "她用螺丝刀撬开，里面是十几张车票和一张陌生人的照片。", ("她",)),
        ("内景·黄昏", "她坐在空屋地板上，一张张看那些车票的日期。", ("她",)),
        ("外景·黄昏", "楼下司机按了两次喇叭，她把铁盒塞进包里。", ("她",)),
        ("内景·夜", "新家还没开灯，她坐在纸箱中间，照片摊在膝盖上。", ("她",)),
    ]),
}


# ---------------------------------------------------------------- 判据
def _violations_metric(shots: list, problems: list[str]) -> dict:
    """① 硬规则违规数 —— **直接用项目校验器的输出**，不另立标准。"""
    return {"violations": len(problems), "problems": list(problems)[:12],
            "shots": len(shots)}


def _budget_metric(shots: list, plan_scenes: list[dict]) -> dict:
    """② 预算偏差：逐场 |实际 − 预算| 之和 / 场数。"""
    want = {str(s.get("sceneId") or ""): int(s.get("shotBudget") or 0)
            for s in plan_scenes if s.get("sceneId")}
    if not want:
        return {"budgetDev": None, "perScene": {}}
    got: dict[str, int] = {}
    for sh in shots:
        sid = str(getattr(sh, "scene_id", "") or "")
        got[sid] = got.get(sid, 0) + 1
    diffs = {sid: got.get(sid, 0) - w for sid, w in want.items()}
    return {"budgetDev": round(sum(abs(d) for d in diffs.values()) / len(diffs), 3),
            "perScene": diffs}


def _diversity_metric(shots: list) -> dict:
    """③ 多样性：景别分布熵 + 每场镜数方差（**防"每场刚好 2 镜"刷分**）。"""
    sizes = [str(getattr(s, "shot_size", "") or "") for s in shots]
    n = len(sizes)
    ent = 0.0
    if n:
        for v in set(sizes):
            p = sizes.count(v) / n
            if p > 0:
                ent -= p * math.log(p)
    per_scene: dict[str, int] = {}
    for s in shots:
        sid = str(getattr(s, "scene_id", "") or "")
        per_scene[sid] = per_scene.get(sid, 0) + 1
    var = (round(statistics.pvariance(list(per_scene.values())), 3)
           if len(per_scene) > 1 else 0.0)
    return {"sizeEntropy": round(ent, 3),
            "scenesWithShots": len(per_scene),
            "shotsPerSceneVariance": var}


# ---------------------------------------------------------------- 跑一组
def _run_condition(name: str, draft: ScriptDraft, *, client: LlmClient,
                   plan: dict | None) -> dict:
    """跑一次。`plan=None` 就是单智能体（**同一个实现**）。"""
    sb = StoryboardArtistLlm(client=client, max_shots=MAX_SHOTS,
                             plan=plan)
    t0 = time.perf_counter()
    out = sb(envelope=CallEnvelope.root(
        "storyboard_artist", "shot.list", {"script.final": draft},
        Budget.unlimited(), callee_kind="agent"), runtime=None)
    dt = time.perf_counter() - t0
    if not out.ok:
        return {"condition": name, "ok": False,
                "reason": str(getattr(out, "error_message", ""))[:200],
                "seconds": round(dt, 1)}
    sl = out.output["shot.list"]
    shots = list(sl.shots)
    # 用同一个校验器重跑一遍，拿到 problems（`_to_shots` 里也会给）
    _, problems = sb._to_shots(
        {"shots": [s.as_dict() for s in shots]}, draft) if hasattr(
            sb, "_to_shots") else ([], [])
    mm = {
        "condition": name, "ok": True, "seconds": round(dt, 1),
        "hasPlan": bool(plan),
        **_violations_metric(shots, problems),
        **_budget_metric(shots, (plan or {}).get("scenes") or []),
        **_diversity_metric(shots),
        # ④ 镜头数达成率（**pilot 之后追加**，见 DESIGN.md §11）：
        # 判据①只数"错"，而"切得太少"不是错 —— 第一版漏了。
        "shotCountRatio": (
            round(len(shots) / max(1, int((plan or {}).get("totalShotBudget")
                                          or 20)), 3)),
        "shotList": [s.as_dict() for s in shots],
    }
    return mm


def main() -> int:
    quick = "--quick" in sys.argv
    repeats = 1 if quick else REPEATS
    scripts = {"短·雨夜": SCRIPTS["短·雨夜"]} if quick else SCRIPTS

    print("=" * 78)
    print(f"多智能体 vs 单智能体 —— {len(scripts)} 剧本 × {repeats} 重复")
    print("=" * 78)

    client = LlmClient(MODEL, timeout_s=300)
    raw: list[dict] = []
    t_start = time.perf_counter()

    for eid, draft in scripts.items():
        # ---- 导演（两组共用同一份预算）----
        d = DirectorLlm(client=client, total_shots=20)
        dout = d(envelope=CallEnvelope.root(
            "director", "episode.direct", {"script.draft": draft},
            Budget.unlimited(), callee_kind="agent"), runtime=None)
        plan = dout.output["episode.plan"] if dout.ok else normalize_plan({})
        print(f"\n【{eid}】{len(draft.scenes)} 场　导演预算 "
              f"{[(s['sceneId'], s['shotBudget']) for s in plan.get('scenes', [])]}")

        for r in range(repeats):
            # ⚠️ **顺序随机化**：按 (剧本序号 + 重复序号) 的奇偶决定先后，
            # 避免"永远先跑 A"带来的服务端状态/时间漂移偏差（DESIGN.md §4-4）。
            order = ["A", "B"] if (hash(eid) + r) % 2 == 0 else ["B", "A"]
            for cond in order:
                if cond == "A":
                    got = _run_condition("A-单智能体", draft, client=client,
                                         plan=None)
                else:
                    got = _run_condition("B-多智能体", draft, client=client,
                                         plan=plan)
                got.update({"script": eid, "repeat": r + 1, "order": order})
                raw.append(got)
                print(f"    r{r+1} {got['condition']}  "
                      f"{'ok' if got.get('ok') else 'FAIL'}  "
                      f"违规 {got.get('violations')}  镜头 {got.get('shots')}  "
                      f"预算偏差 {got.get('budgetDev')}  "
                      f"熵 {got.get('sizeEntropy')}  {got.get('seconds')}s")

    # ---- 汇总 ----
    def agg(cond: str, key: str) -> list[float]:
        return [g[key] for g in raw
                if g.get("ok") and g["condition"].startswith(cond)
                and g.get(key) is not None]

    print()
    print("=" * 78)
    print("  汇总（各条件取中位数；配对比较看同一剧本的差）")
    print("=" * 78)
    summary: dict[str, dict] = {}
    for cond, label in (("A", "单智能体"), ("B", "多智能体")):
        row = {}
        for key, cn in (("violations", "违规数"), ("budgetDev", "预算偏差"),
                        ("sizeEntropy", "景别熵"),
                        ("shotsPerSceneVariance", "每场镜数方差"),
                        ("shots", "镜头数"),
                        ("shotCountRatio", "镜头数达成率")):
            vals = agg(cond, key)
            row[key] = (round(statistics.median(vals), 3) if vals else None)
            print(f"  {label:<8} {cn:<12} {row[key]}")
        summary[label] = row
        print()

    # 配对差（同一剧本同一重复）
    pairs = {}
    for g in raw:
        if not g.get("ok"):
            continue
        k = (g["script"], g["repeat"])
        pairs.setdefault(k, {})[g["condition"][0]] = g
    diffs = {"violations": [], "budgetDev": [], "sizeEntropy": [],
             "shotCountRatio": []}
    for k, v in pairs.items():
        if "A" not in v or "B" not in v:
            continue
        for key in diffs:
            a, b = v["A"].get(key), v["B"].get(key)
            if a is not None and b is not None:
                diffs[key].append(b - a)
    print("  配对差（B − A，负=多智能体更少/更好）：")
    for key, cn in (("violations", "违规数"), ("budgetDev", "预算偏差"),
                    ("sizeEntropy", "景别熵"),
                    ("shotCountRatio", "镜头数达成率")):
        ds = diffs[key]
        if ds:
            print(f"    {cn:<8} n={len(ds)}  中位差 {round(statistics.median(ds),3)}"
                  f"  逐对 {[round(d,2) for d in ds]}")
    summary["pairedDiff"] = {k: (round(statistics.median(v), 3) if v else None)
                             for k, v in diffs.items()}

    (OUT / "raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    (OUT / "summary.json").write_text(json.dumps(
        {"model": MODEL, "repeats": repeats, "scripts": list(scripts),
         "summary": summary}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  用时 {time.perf_counter()-t_start:.0f}s")
    print(f"  原始输出 {OUT/'raw.json'}（可复查）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
