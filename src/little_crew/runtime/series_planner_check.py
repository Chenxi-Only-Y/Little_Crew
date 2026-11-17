"""`runtime/series_planner.py` 自检（2026-11-17）。

系列级档位：台账上下文 / 已有档位为下限 / 跨集升档 / Tier3 上限 / 人工覆盖 / LLM 失败兜底。
"""
from __future__ import annotations

import sys

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


class _Shot:
    def __init__(self, sid, scene):
        self.shot_id = sid
        self.scene_id = scene


class _FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.prompts = []

    def chat_json(self, prompt, *, system="", what=""):
        self.prompts.append(prompt)
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


SCRIPT = {"scenes": [
    {"id": "E2-S1", "heading": "地铁站台·夜", "text": "站台戏"},
    {"id": "E2-S2", "heading": "走廊·夜", "text": "走廊戏"},
]}
SHOTS = [_Shot("a", "E2-S1"), _Shot("b", "E2-S1"), _Shot("c", "E2-S2")]
CARDS = {"E2-S1": {"space": "地下站台"}, "E2-S2": {"space": "医院走廊"}}


def _bible():
    return {"seriesId": "T", "episodes": [{"episodeId": "E01"}],
            "scenes": {
                "地铁站台": {"sceneKey": "地铁站台", "tier": 2,
                           "episodes": ["E01"], "usage": 2,
                           "cardHash": "", "assets": {}},
                "走廊": {"sceneKey": "走廊", "tier": 1,
                       "episodes": ["E01"], "usage": 1, "cardHash": "", "assets": {}},
            }, "cast": {}, "usage": []}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("系列级档位决策 —— 自检")
    print("=" * 66)

    from little_crew.runtime import series_planner as SP

    print(); print("A. 提示词带系列台账")
    prompt = SP.build_prompt(episode_id="E02", scenes=[
        {"sceneId": "E2-S1", "heading": "地铁站台·夜", "text": "x"},
        {"sceneId": "E2-S2", "heading": "走廊·夜", "text": "y"}],
        shots_per_scene={"E2-S1": 2, "E2-S2": 1}, scene_cards=CARDS,
        data=_bible(), hero_max=1,
        keys={"E2-S1": "地铁站台", "E2-S2": "走廊"})
    check("A1 ★★★ 提示词含台账已有档位/出现集数",
          "地铁站台" in prompt and "T2" in prompt and "E01" in prompt, "")
    check("A2 ★★★ 提示词写明 Tier3 上限/不降档", "最多 1 场" in prompt and "不降档" in prompt, "")
    check("A3 ★★ 提示词要求输出 promotion", "promotion" in prompt, "")

    print(); print("B. LLM 正常返回 + 台账下限")
    client = _FakeClient({"scenes": [
        {"sceneId": "E2-S1", "tier": 1, "sceneKey": "地铁站台",
         "reason": "老场景", "promotion": False},
        {"sceneId": "E2-S2", "tier": 3, "sceneKey": "走廊",
         "reason": "本集题眼", "promotion": True},
    ], "seriesNote": "系列第二集"})
    out = SP.plan_episode(episode_id="E02", script=SCRIPT, shots=SHOTS,
                          scene_cards=CARDS, data=_bible(), client=client,
                          hero_max=2,
                          keys={"E2-S1": "地铁站台", "E2-S2": "走廊"})
    check("B1 ★★★ 来源记 llm-series", out["source"] == "llm-series", str(out["source"]))
    check("B2 ★★★ 已有档位是下限（LLM 给 1，台账 2 → 保持 2）",
          out["scenes"]["E2-S1"]["tier"] == 2, str(out["scenes"]["E2-S1"]))
    check("B3 ★★★ 新场景按 LLM 升到 Tier3", out["scenes"]["E2-S2"]["tier"] == 3, "")
    check("B4 ★★ promotion 标记被保留", out["scenes"]["E2-S2"].get("promotion") is True, "")
    check("B5 ★★★ sceneKey 进决策（跨集稳定）",
          out["scenes"]["E2-S1"]["sceneKey"] == "地铁站台", "")
    check("B6 ★★ 成本按档位算（0.2+0.45=0.65）",
          abs(out["costCny"] - 0.65) < 1e-6, str(out["costCny"]))

    print(); print("C. 上限 / 人工覆盖 / 兜底")
    c2 = _FakeClient({"scenes": [
        {"sceneId": "E2-S1", "tier": 3, "sceneKey": "地铁站台"},
        {"sceneId": "E2-S2", "tier": 3, "sceneKey": "走廊"}]})
    out2 = SP.plan_episode(episode_id="E03", script=SCRIPT, shots=SHOTS,
                           scene_cards=CARDS, data=_bible(), client=c2,
                           hero_max=1, keys={"E2-S1": "地铁站台", "E2-S2": "走廊"})
    t3 = [s for s in ("E2-S1", "E2-S2") if out2["scenes"][s]["tier"] == 3]
    check("C1 ★★★ Tier3 上限只留 1 场", len(t3) == 1, str(t3))
    out3 = SP.plan_episode(episode_id="E03", script=SCRIPT, shots=SHOTS,
                           scene_cards=CARDS, data=_bible(), client=c2,
                           hero_max=2, overrides={"走廊": 2},
                           keys={"E2-S1": "地铁站台", "E2-S2": "走廊"})
    check("C2 ★★★ 人工按 sceneKey 覆盖（走廊 → 2）",
          out3["scenes"]["E2-S2"]["tier"] == 2
          and out3["scenes"]["E2-S2"]["source"] == "manual", "")
    out4 = SP.plan_episode(episode_id="E04", script=SCRIPT, shots=SHOTS,
                           scene_cards=CARDS, data=_bible(),
                           client=_FakeClient(RuntimeError("boom")), hero_max=2,
                           keys={"E2-S1": "地铁站台", "E2-S2": "走廊"})
    check("C3 ★★★ LLM 挂了 -> 系列启发式", out4["source"] == "series-heuristic", str(out4["source"]))
    check("C4 ★★ 兜底也遵守台账下限（地铁站台 ≥2）",
          out4["scenes"]["E2-S1"]["tier"] >= 2, str(out4["scenes"]["E2-S1"]))
    check("C5 ★★ 错误被记录", "boom" in str(out4.get("error")), str(out4.get("error")))
    out5 = SP.plan_episode(episode_id="E05", script=SCRIPT, shots=SHOTS,
                           scene_cards=CARDS, data=_bible(), client=None,
                           hero_max=2, keys={"E2-S1": "地铁站台", "E2-S2": "走廊"})
    check("C6 ★★★ 无 client 也能跑（不需要 key）",
          out5["source"] == "series-heuristic" and len(out5["scenes"]) == 2, "")

    n = sum(1 for _, ok, _ in _r if ok)
    print()
    for name, ok, detail in _r:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 66)
    print(f"结果：{n}/{len(_r)} 通过")
    return 0 if n == len(_r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
