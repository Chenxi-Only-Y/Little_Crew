"""只换**分镜师模型**的对照实验（2026-11-08，v2）。

## 与 v1 的差别（v1 全失败，是我的错）

v1 我直接 `chat_json(prompt, system="你是分镜师。")` —— **绕过了真实的分镜师提示词
与系统消息**，于是模型返回的结构不对（`shots` 缺失 / sceneId 为空）。
v2 走**真实链路**：`storyboard_per_scene` + `CallEnvelope`，
与整集跑时完全同一套调用（含逐场生成、约束校验、次数上限）。
"""
import json, statistics as st, sys, time
from pathlib import Path

P = Path(r"D:\AI\ds.8\Little_Crew")
sys.path.insert(0, str(P / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from little_crew.agent.llm import LlmClient
from little_crew.agent.protocol import Budget, CallEnvelope
from little_crew.agent.storyboard_llm import StoryboardArtistLlm, storyboard_per_scene
from little_crew.governance.plan_alloc import audit as quota_audit
from little_crew.governance.plan_alloc import quotas_for

DRAFT = {
    "episodeId": "AB-MODEL", "title": "灯下",
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
import os as _os

#: 可用环境变量只跑指定模型（便于单独补一个臂）
MODELS = ([x.strip() for x in _os.environ["SB_MODELS"].split(",") if x.strip()]
          if _os.environ.get("SB_MODELS")
          else ["deepseek-flash", "mimo-v2.6-flash", "GLM-5.3-flash"])
REPEATS = int(sys.argv[1]) if len(sys.argv) > 1 else 6


def _make_draft(d):
    """必须是真 `ScriptDraft`：`_as_draft()` 只认它或 Mapping，
    自造的鸭子类型会被拒（实测报"分镜师未收到定稿剧本"）。"""
    from little_crew.agent.messages import ScriptDraft

    return ScriptDraft(frm="screenwriter", to="*",
                       episode_id=d["episodeId"], title=d["title"],
                       scenes=tuple(d["scenes"]))


def run_once(model: str) -> dict:
    client = LlmClient(model)
    sb = StoryboardArtistLlm(client=client, plan=PLAN)

    def _call(draft_sub):
        return sb(envelope=CallEnvelope.root(
            "storyboard_artist", "shot.list", {"script.final": draft_sub},
            Budget.unlimited(), callee_kind="agent"), runtime=None)

    draft = _make_draft(DRAFT)
    quotas = quotas_for(PLAN, DRAFT["scenes"])
    t0 = time.time()
    try:
        res = storyboard_per_scene(_call, draft)
        ok = bool(getattr(res, "ok", False))
        _out = getattr(res, "output", None) or {}
        _sl = _out.get("shot.list") if isinstance(_out, dict) else None
        shots = list(getattr(_sl, "shots", ()) or ())
        why = "" if ok else str(getattr(res, "error_message", "")
                                or getattr(res, "detail", "") or "fail")[:80]
    except Exception as e:                                    # noqa: BLE001
        ok, shots, why = False, [], f"{type(e).__name__}: {e}"[:80]
    dt = round(time.time() - t0, 1)
    au = quota_audit(quotas, shots)
    return {"model": model, "ok": ok, "shots": len(shots), "why": why,
            "seconds": dt, "compliance": au["complianceRate"],
            "findings": len(au["findings"]),
            "perScene": [s["shots"] for s in au["scenes"]]}


def main() -> int:
    out = Path(__file__).parent / ("raw.json" if len(MODELS) > 1 else f"raw.{MODELS[0]}.json")
    recs = []
    for i in range(REPEATS):
        for m in MODELS:
            r = run_once(m)
            recs.append({**r, "repeat": i + 1})
            print(f"  [{i+1}/{REPEATS}] {m:18s} ok={str(r['ok']):5s} 镜={r['shots']:2d} "
                  f"遵守={r['compliance']*100:.0f}% {r['seconds']:5.0f}s {r['why'][:36]}")
    out.write_text(json.dumps(recs, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n  === 汇总 ===")
    for m in MODELS:
        rs = [r for r in recs if r["model"] == m]
        oks = [r for r in rs if r["ok"]]
        print(f"  {m:18s} 成功 {len(oks)}/{len(rs)} · "
              f"镜均 {st.mean([r['shots'] for r in oks]) if oks else 0:4.1f} · "
              f"遵守 {st.mean([r['compliance'] for r in oks])*100 if oks else 0:3.0f}% · "
              f"耗时 {st.mean([r['seconds'] for r in rs]):3.0f}s")
    print(f"\n  原始数据: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
