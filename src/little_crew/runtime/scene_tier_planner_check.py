"""`runtime/scene_tier_planner.py` 自检（2026-11-17）。

档位决策：**LLM 看整部剧 → 人工覆盖优先 → 失败退回频率启发式**；
Tier3 每集上限；决策来源必须写清（llm / heuristic / manual）。
"""
from __future__ import annotations

import sys

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def _script(n: int = 3) -> dict:
    return {"scenes": [
        {"id": f"SC{i}", "heading": f"地点{i}·夜",
         "text": f"这是第 {i} 场的剧本内容，" + "细节" * 60}
        for i in range(1, n + 1)]}


class _Shot:
    def __init__(self, sid: str, scene: str):
        self.shot_id = sid
        self.scene_id = scene


class _FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.prompts: list[str] = []

    def chat_json(self, prompt, *, system="", what=""):
        self.prompts.append(prompt)
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("场景档位决策（LLM + 人工覆盖 + 启发式兜底）—— 自检")
    print("=" * 66)

    from little_crew.runtime import scene_tier_planner as TP

    print(); print("A. 提示词：整部剧 + 档位成本 + 上限")
    prompt = TP.build_prompt(
        scenes=[{"sceneId": "SC1", "heading": "门口·夜", "text": "x" * 500},
                {"sceneId": "SC2", "heading": "客厅·日", "text": "y"}],
        shots_per_scene={"SC1": 4, "SC2": 1},
        scene_cards={"SC1": {"space": "旧公寓门口", "props": ["皮箱"]}},
        hero_max=2)
    check("A1 ★★★ 提示词列出全部场次（不能只看本场）",
          "SC1" in prompt and "SC2" in prompt and "全部场次" in prompt, "")
    check("A2 ★★★ 提示词写明 Tier3 每集上限", "最多 2 场" in prompt or "≤ 2" in prompt, "")
    check("A3 ★★ 提示词包含各档成本", "0.05" in prompt and "0.45" in prompt, "")
    check("A4 ★★ 场景卡摘要被带上", "旧公寓门口" in prompt and "皮箱" in prompt, "")
    check("A5 ★★★ 长文本截断（不会塞爆 prompt）", prompt.count("细节") <= 4, "")

    print(); print("B. LLM 正常返回")
    shots = [_Shot(f"{sc}-{i}", sc) for sc in ("SC1", "SC2", "SC3")
             for i in range(1, 3)]
    client = _FakeClient({"scenes": [
        {"sceneId": "SC1", "tier": 3, "reason": "全剧题眼"},
        {"sceneId": "SC2", "tier": 2, "reason": "普通主场景"},
        {"sceneId": "SC3", "tier": 1, "reason": "过场"},
    ], "seriesNote": "整部剧：门口是题眼"})
    out = TP.plan_tiers(script=_script(3), shots=shots, client=client, hero_max=2)
    check("B1 ★★★ 来源记为 llm", out["source"] == "llm", str(out["source"]))
    check("B2 ★★★ 三场档位按 LLM 结果", [out["scenes"][s]["tier"] for s in
                                          ("SC1", "SC2", "SC3")] == [3, 2, 1], "")
    check("B3 ★★ seriesNote 被记录", "题眼" in out.get("seriesNote", ""), "")
    check("B4 ★★★ 成本按档位算（0.45+0.20+0.05=0.70）",
          abs(out["costCny"] - 0.70) < 1e-6, str(out["costCny"]))
    check("B5 ★★ 每场带理由", all(out["scenes"][s].get("reason") for s in
                                 ("SC1", "SC2", "SC3")), "")

    print(); print("C. Tier3 上限（超出按分数/镜数降级）")
    c2 = _FakeClient({"scenes": [
        {"sceneId": "SC1", "tier": 3}, {"sceneId": "SC2", "tier": 3},
        {"sceneId": "SC3", "tier": 3}]})
    out2 = TP.plan_tiers(script=_script(3), shots=shots, client=c2, hero_max=1)
    t3 = [s for s in ("SC1", "SC2", "SC3") if out2["scenes"][s]["tier"] == 3]
    check("C1 ★★★ 只保留 1 场 Tier3", len(t3) == 1, str(t3))
    check("C2 ★★★ 被降级的场写明原因（上限）",
          any("上限" in str(out2["scenes"][s].get("reason") or "")
              for s in ("SC1", "SC2", "SC3")), "")

    print(); print("D. 人工覆盖（优先级最高）")
    out3 = TP.plan_tiers(script=_script(3), shots=shots, client=client,
                         hero_max=2, overrides={"SC3": 3, "SC1": 1})
    check("D1 ★★★ 人工把 SC3 提到 Tier3",
          out3["scenes"]["SC3"]["tier"] == 3
          and out3["scenes"]["SC3"]["source"] == "manual", str(out3["scenes"]["SC3"]))
    check("D2 ★★★ 人工把 SC1 降到 Tier1",
          out3["scenes"]["SC1"]["tier"] == 1, str(out3["scenes"]["SC1"]))
    check("D3 ★★ 非法覆盖值被忽略", TP.plan_tiers(
        script=_script(2), shots=shots[:2], client=client, hero_max=2,
        overrides={"SC1": 9})["scenes"]["SC1"]["source"] != "manual", "")

    print(); print("E. 失败兜底（LLM 挂/没 key/漏场）")
    out4 = TP.plan_tiers(script=_script(3), shots=shots,
                         client=_FakeClient(RuntimeError("boom")), hero_max=2)
    check("E1 ★★★ LLM 抛错 -> 退回启发式", out4["source"] == "heuristic", str(out4["source"]))
    check("E2 ★★ 错误原因被记录", "boom" in str(out4.get("error")), str(out4.get("error")))
    check("E3 ★★★ 没有 client -> 启发式（不需要 key）",
          TP.plan_tiers(script=_script(2), shots=shots[:2], client=None)["source"]
          == "heuristic", "")
    c3 = _FakeClient({"scenes": [{"sceneId": "SC1", "tier": 2}]})
    out5 = TP.plan_tiers(script=_script(3), shots=shots, client=c3, hero_max=2)
    check("E4 ★★★ LLM 漏场 -> 漏的场用启发式补，来源标清",
          out5["scenes"]["SC2"]["source"] == "heuristic"
          and out5["scenes"]["SC1"]["source"] == "llm", str(out5["scenes"]))
    c4 = _FakeClient({"scenes": [{"sceneId": "SC1", "tier": 99},
                                 {"sceneId": "NOPE", "tier": 3}]})
    out6 = TP.plan_tiers(script=_script(2), shots=shots[:2], client=c4, hero_max=2)
    check("E5 ★★★ 非法档位/未知场被剔除", out6["scenes"]["SC1"]["tier"] in (1, 2, 3)
          and "NOPE" not in out6["scenes"], str(out6["scenes"]))

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
