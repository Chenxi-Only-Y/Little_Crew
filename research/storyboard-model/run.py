"""只换**分镜师模型**的对照实验（2026-11-08）。

## 问题

「多智能体」里分镜这一环，换个更强/不同的模型，能不能改善**可数指标**？
—— 不看盲判（噪声大），只看：JSON 合法率、0 镜率、配额遵守率、镜头数。

## 设计

同一份剧本、同一个导演方案（都固定），只换 `storyboard_artist` 的客户端模型：

    A: deepseek-flash      （默认）
    B: mimo-v2.6-flash     （另一个族）
    C: GLM-5.3-flash       （第三族）

每个模型跑 N 次（采样方差要能看出来），记录每次的：
    ok / 镜头数 / 配额遵守率 / 违例数 / 耗时

## 为什么用这个设计

整集 A/B 一次 25 分钟，且混入了渲染与配音的噪声。
**把分镜单独拿出来跑**，8 次只要十几分钟，而且变量只有"模型"一个。
"""
import json, statistics as st, sys, time
from pathlib import Path

P = Path(r"D:\AI\ds.8\Little_Crew")
sys.path.insert(0, str(P / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from little_crew.agent.llm import LlmClient
from little_crew.agent.storyboard_llm import StoryboardArtistLlm
from little_crew.governance.plan_alloc import audit as quota_audit
from little_crew.governance.plan_alloc import quotas_for

# ---- 固定的剧本与导演方案（两臂完全相同，只换分镜模型）----
DRAFT = {
    "episodeId": "AB-MODEL",
    "title": "灯下",
    "scenes": [
        {"scene_id": "S01", "heading": "内景·小屋·夜",
         "characters": ["少女", "父亲"],
         "text": "少女把信折好，塞进抽屉。父亲坐在门槛上，没有回头。",
         "dialogue": [{"character": "父亲", "line": "你娘走的时候，也是这个天。"}]},
        {"scene_id": "S02", "heading": "外景·村口·晨",
         "characters": ["少女"],
         "text": "少女提着包袱走到村口，回头看了一眼。晨雾很重。",
         "dialogue": [{"character": "少女", "line": "我走了。"}]},
        {"scene_id": "S03", "heading": "内景·屋子·夜",
         "characters": ["父亲"],
         "text": "父亲打开抽屉，拿出那封信，没拆。灯芯爆了一下。",
         "dialogue": []},
    ],
}
PLAN = {"tone": "克制", "pacing": "前松后紧",
        "scenes": [
            {"sceneId": "S01", "shotBudget": 4, "role": "铺垫", "priority": 3,
             "sceneSeconds": 20, "focus": "父亲的沉默"},
            {"sceneId": "S02", "shotBudget": 3, "role": "高潮", "priority": 5,
             "sceneSeconds": 16, "focus": "她回头那一眼"},
            {"sceneId": "S03", "shotBudget": 4, "role": "抒情", "priority": 4,
             "sceneSeconds": 20, "allowLong": True, "focus": "信没拆"},
        ]}

MODELS = ["deepseek-flash", "mimo-v2.6-flash", "GLM-5.3-flash"]
REPEATS = int(sys.argv[1]) if len(sys.argv) > 1 else 8


class _Draft:
    """故事板要的最小结构。"""
    def __init__(self, d):
        self.episode_id = d["episodeId"]
        self.title = d["title"]
        self.scenes = tuple(d["scenes"])


def run_once(model: str) -> dict:
    sb = StoryboardArtistLlm(client=LlmClient(model))
    sb.plan = PLAN
    quotas = quotas_for(PLAN, DRAFT["scenes"])
    t0 = time.time()
    ok, shots, why = False, [], ""
    try:
        # 直接走内部接口，避免包 envelope 的仪式
        prompt = sb._build_prompt(_Draft(DRAFT))
        data = sb.client.chat_json(prompt, system="你是分镜师。", what="分镜表")
        shots, problems = sb._to_shots(data, _Draft(DRAFT))
        ok = not problems and bool(shots)
        why = "；".join(problems[:3]) if problems else ("" if shots else "0-shot")
    except Exception as e:                                    # noqa: BLE001
        why = f"{type(e).__name__}: {str(e)[:80]}"
    dt = round(time.time() - t0, 1)
    au = quota_audit(quotas, shots)
    return {"model": model, "ok": ok, "shots": len(shots), "why": why,
            "seconds": dt,
            "compliance": au["complianceRate"],
            "findings": len(au["findings"]),
            "perScene": [s["shots"] for s in au["scenes"]]}


def main() -> int:
    out = Path(__file__).parent / "raw.json"
    recs = []
    for i in range(REPEATS):
        for m in MODELS:
            r = run_once(m)
            recs.append({**r, "repeat": i + 1})
            print(f"  [{i+1}/{REPEATS}] {m:18s} ok={r['ok']!s:5s} "
                  f"镜={r['shots']:2d} 遵守={r['compliance']*100:.0f}% "
                  f"{r['seconds']:.0f}s {r['why'][:40]}")
    out.write_text(json.dumps(recs, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n  === 汇总 ===")
    for m in MODELS:
        rs = [r for r in recs if r["model"] == m]
        oks = [r for r in rs if r["ok"]]
        print(f"  {m:18s} 成功 {len(oks)}/{len(rs)} · "
              f"镜头均 {st.mean([r['shots'] for r in oks]) if oks else 0:.1f} · "
              f"遵守率均 {st.mean([r['compliance'] for r in oks])*100 if oks else 0:.0f}% · "
              f"违例均 {st.mean([r['findings'] for r in oks]) if oks else 0:.1f} · "
              f"耗时均 {st.mean([r['seconds'] for r in rs]):.0f}s")
    print(f"\n  原始数据: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
