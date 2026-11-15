"""场景设计师自检（2026-11-15）。

锁住六件事：
1 **名册对称**：场景设计师与角色设计师同级同组（美术组），
   美术指导不再产场景资产（`scene.library` 归场景设计师）
2 规则桩**确定性**：同一剧本两次跑出同样的场景卡（跨镜/跨集一致的前提）
3 规则桩**不编造**：只从原文取 `space`、按关键词推 `era`，不凭空写材质/道具
4 LLM 实现：JSON 解析 + `era` 封闭集校验；解析失败 → `ok=False`（不静默）
5 `normalize_scene_library`：缺场次号/缺 space 的卡被剔除
6 默认桩注册：`CORE_CREW_STUBS` 里真的有 `scene_designer`
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


class FakeClient:
    def __init__(self, text: str):
        self.text = text
        self.calls = 0

    def chat(self, prompt, system="", temperature=0.0):
        self.calls += 1
        return SimpleNamespace(text=self.text)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("场景设计师（scene_designer）—— 自检")
    print("=" * 66)

    from little_crew.agent.crew_stubs import CORE_CREW_STUBS
    from little_crew.agent.roster import DEFAULT_ROSTER
    from little_crew.agent.scene_designer_llm import (
        SceneDesignerLlm,
        SceneDesignerRules,
        normalize_scene_library,
    )

    print(); print("A. 名册对称（资产层）")
    by_id = {a.agent_id: a for a in DEFAULT_ROSTER}
    check("A1 ★★★ 场景设计师存在且属于美术组",
          "scene_designer" in by_id
          and by_id["scene_designer"].department.value == "art",
          str(by_id.get("scene_designer") and
              by_id["scene_designer"].department))
    check("A2 ★★★ 场景设计师产出 scene.library",
          "scene.library" in tuple(by_id["scene_designer"].produces), "")
    check("A3 ★★★ 美术指导不再产出 scene.library（资产与裁决分离）",
          "scene.library" not in tuple(by_id["production_designer"].produces), "")
    check("A4 ★★ 名册规模 20（19 + 场景设计师）",
          len(DEFAULT_ROSTER) == 20, str(len(DEFAULT_ROSTER)))
    check("A5 ★★ 默认桩里注册了场景设计师",
          "scene_designer" in CORE_CREW_STUBS, str(sorted(CORE_CREW_STUBS)))

    print(); print("B. 规则桩：确定性且不编造")
    draft = SimpleNamespace(scenes=[
        {"id": "S01", "text": "雨夜站台，列车进站，少女独自等待。"},
        {"id": "S02", "text": "未来城市的全息广告牌下，两人对峙。"},
    ])
    rules = SceneDesignerRules()
    env = SimpleNamespace(payload={"script.draft": draft})
    r1 = rules(envelope=env, runtime=None)
    r2 = rules(envelope=env, runtime=None)
    lib1 = (r1.output or {}).get("scene.library") or {}
    check("B1 ★★★ 规则桩产出场景库（ok=True 且两场）",
          bool(r1.ok) and len(lib1.get("scenes") or []) == 2, str(r1.ok))
    check("B2 ★★★ 两次跑结果完全一致（确定性）",
          (r1.output or {}) == (r2.output or {}), "")
    sc1 = (lib1.get("scenes") or [{}])[0]
    check("B3 ★★★ space 直接取剧本原文（逐字，不摘要）",
          sc1.get("space", "").startswith("雨夜站台"), sc1.get("space", "")[:40])
    check("B4 ★★★ 不凭空编材质/道具/色板",
          not any(k in sc1 for k in ("materials", "props", "palette")), str(sc1))
    sc2 = (lib1.get("scenes") or [{}, {}])[1]
    check("B5 ★★ 年代按关键词推断（未来 -> 未来）",
          sc2.get("era") == "未来", str(sc2.get("era")))
    check("B6 ★★ 现代题材兜底为当代",
          sc1.get("era") == "当代", str(sc1.get("era")))

    print(); print("C. 归一化与校验")
    lib = normalize_scene_library({"scenes": [
        {"sceneId": "S01", "space": "站台"},
        {"scene_id": "S02", "space": "车厢"},
        {"sceneId": "", "space": "没有场次号的卡"},
        {"sceneId": "S03", "space": ""},
    ], "throughLine": "雨"})
    check("C1 ★★★ 缺场次号/缺 space 的卡被剔除",
          [x["sceneId"] for x in lib["scenes"]] == ["S01", "S02"], str(lib["scenes"]))
    check("C2 ★★ throughLine 透传", lib.get("throughLine") == "雨", "")
    try:
        normalize_scene_library({"scenes": [
            {"sceneId": "S01", "space": "站台", "era": "赛博"}]})
        bad = False
    except ValueError:
        bad = True
    check("C3 ★★★ 非法 era -> ValueError（封闭集）", bad, "")

    print(); print("D. LLM 实现")
    good = ('{"scenes":[{"sceneId":"S01","space":"地下站台",'
            '"materials":["钢"],"era":"当代"}],"throughLine":"冷"}')
    llm = SceneDesignerLlm(client=FakeClient(good))
    r = llm(envelope=SimpleNamespace(payload={"script.draft": "…剧本…"}),
            runtime=None)
    out = (r.output or {}).get("scene.library") or {}
    check("D1 ★★★ LLM 输出被解析成场景库", bool(r.ok)
          and len(out.get("scenes") or []) == 1, str(r.ok))
    bad_llm = SceneDesignerLlm(client=FakeClient("这不是 JSON"))
    rb = bad_llm(envelope=SimpleNamespace(payload={"script.draft": "…"}),
                 runtime=None)
    check("D2 ★★★ 解析失败 -> ok=False 且带错误码",
          rb.ok is False and bool(rb.error_code), str(rb.error_message)[:60])

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
