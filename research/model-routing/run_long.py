"""**长结构任务**的模型对照实验（2026-11-08）。

## 为什么重做实验

上一版测"同一分镜任务换模型"——三个模型产物**完全一样**（任务被约束死了）。
用户指出方向不对：要的是**让合适的模型做合适的事**。

所以这版测的是**任务类型**差异：`long_structure`（一次写 8 场剧本）。

## 判据（可数，不看文采）

| 指标 | 定义 |
|---|---|
| **场数齐全率** | 实际场数 / 要求场数 |
| **结构合法率** | 每场都有 scene_id/heading/characters/text/dialogue |
| **JSON 合法率** | 一次就解析成功 |
| **耗时** | 秒 |

这三项直接对应"长上下文 + 结构完整性"这个长处。
"""
import json, statistics as st, sys, time
from pathlib import Path

P = Path(r"D:\AI\ds.8\Little_Crew")
sys.path.insert(0, str(P / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from little_crew.agent.crew_llm import ScreenwriterLlm
from little_crew.agent.llm import LlmClient
from little_crew.agent.protocol import Budget, CallEnvelope

PREMISE = """题材：北方农村，冬天的集市。
人物：阿杏（十六岁，独生女）、父亲（腿有旧伤，脾气硬）、
      旅人（外乡人，话少，来路不明）、老奶奶（邻居，爱管闲事）。
要求：一集讲"父亲病倒、阿杏决定留下"这件事；
      要有铺陈、要有一次冲突、要有一个安静的收尾；
      对白要短，不要说满；结局不要解释。"""

MODELS = (sys.argv[2].split(",") if len(sys.argv) > 2 and sys.argv[2]
          else ["deepseek-flash", "mimo-v2.6-flash", "GLM-5.3-flash"])
REPEATS = int(sys.argv[1]) if len(sys.argv) > 1 else 3
SCENES = 8


def run_once(model: str) -> dict:
    sw = ScreenwriterLlm(client=LlmClient(model), max_scenes=SCENES)
    t0 = time.time()
    ok, scenes, why, parsed = False, [], "", False
    try:
        res = sw(envelope=CallEnvelope.root(
            "screenwriter", "script.draft",
            {"source.novel": PREMISE, "episodeId": "AB-LONG", "title": "留下"},
            Budget.unlimited(), callee_kind="agent"), runtime=None)
        ok = bool(getattr(res, "ok", False))
        out = getattr(res, "output", None) or {}
        draft = out.get("script.draft") if isinstance(out, dict) else None
        scenes = list(getattr(draft, "scenes", ()) or ())
        parsed = bool(scenes)
        if not ok:
            why = str(getattr(res, "error_message", "") or "fail")[:80]
    except Exception as e:                                    # noqa: BLE001
        why = f"{type(e).__name__}: {str(e)[:70]}"
    dt = round(time.time() - t0, 1)
    need = ("scene_id", "heading", "characters", "text")
    complete = sum(1 for s in scenes
                   if all(str(s.get(k) or "").strip() for k in need))
    dlg = sum(len(s.get("dialogue") or ()) for s in scenes)
    return {"model": model, "ok": ok, "scenes": len(scenes),
            "completeness": round(len(scenes) / SCENES, 3),
            "structOk": complete, "dialogue": dlg,
            "seconds": dt, "why": why}


def main() -> int:
    out = Path(__file__).parent / ("raw.json" if len(MODELS) > 1
                                   else f"raw.{MODELS[0]}.json")
    recs = []
    for i in range(REPEATS):
        for m in MODELS:
            r = run_once(m)
            recs.append({**r, "repeat": i + 1})
            print(f"  [{i+1}/{REPEATS}] {m:18s} ok={str(r['ok']):5s} "
                  f"场={r['scenes']:2d}/{SCENES} 结构完整={r['structOk']:2d} "
                  f"台词={r['dialogue']:2d} {r['seconds']:5.0f}s {r['why'][:30]}")
    out.write_text(json.dumps(recs, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n  === 汇总（长结构任务）===")
    for m in MODELS:
        rs = [r for r in recs if r["model"] == m]
        print(f"  {m:18s} 成功 {sum(1 for r in rs if r['ok'])}/{len(rs)} · "
              f"场数均 {st.mean([r['scenes'] for r in rs]):.1f}/{SCENES} · "
              f"结构完整均 {st.mean([r['structOk'] for r in rs]):.1f} · "
              f"耗时 {st.mean([r['seconds'] for r in rs]):.0f}s")
    print(f"\n  原始数据: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
