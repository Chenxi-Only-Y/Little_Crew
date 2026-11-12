"""阶段断点 + 重放自检（不联网、不渲染）。"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from little_crew.runtime import stage_checkpoint as SC
from little_crew.runtime.replay_agent import ReplayAgent, _restore

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("阶段断点续跑 —— 自检")
    print("=" * 66)

    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        print(); print("A. 存取")
        check("A1 ★★ 空目录 -> 无阶段", SC.done_stages(wd) == [], "")
        SC.save(wd, "script", {"draft": {"title": "T", "scenes": []}},
                meta={"episodeId": "E1"})
        check("A2 ★★ 写一个阶段", SC.done_stages(wd) == ["script"], str(SC.done_stages(wd)))
        SC.save(wd, "storyboard", {"shotList": {"shots": []}},
                meta={"episodeId": "E1"})
        check("A3 ★★★ 合并写（不覆盖别的阶段）",
              SC.done_stages(wd) == ["script", "storyboard"], str(SC.done_stages(wd)))
        check("A4 ★★ 能取回字段",
              SC.payload_of(wd, "script", "draft")["title"] == "T", "")
        try:
            SC.save(wd, "nope", {})
            ok = False
        except ValueError:
            ok = True
        check("A5 ★★★ 未知阶段直接报错", ok, "")

        print(); print("B. 能否续跑（没把握就不许跑）")
        r = SC.check(wd, "render", inputs={"episodeId": "E1"})
        check("B1 ★★★ 前置齐全 -> 可续跑", r["ok"], r["reason"])
        check("B2 ★★ 说清楚复用哪些/重算哪些",
              r["reuse"] == ["script", "storyboard"] and "render" in r["recompute"],
              str(r["reuse"]) + " / " + str(r["recompute"]))
        r2 = SC.check(wd, "render", inputs={"episodeId": "E9"})
        check("B3 ★★★ 集号不一致 -> **拒绝**（重放别人的产物就是错）",
              r2["ok"] is False and "不一致" in r2["reason"], r2["reason"])
        r3 = SC.check(wd, "storyboard", inputs={"episodeId": "E1"})
        check("B4 ★★ 从 storyboard 起只需 script",
              r3["ok"] and r3["reuse"] == ["script"], str(r3["reuse"]))
        r4 = SC.check(Path(td) / "empty", "render")
        check("B5 ★★★ 没 checkpoint -> 拒绝并说明", r4["ok"] is False
              and "没有 checkpoint" in r4["reason"], r4["reason"])

    print(); print("C. 重放代理")
    class _Inner:
        agent_id = "screenwriter"

        def __init__(self):
            self.calls = 0

        def __call__(self, *, envelope, runtime=None):
            self.calls += 1
            raise AssertionError("不该调到真实现")

    rec = {"output": {"kind": "script.draft", "title": "旧稿", "episodeId": "E1",
                      "scenes": [{"scene_id": "S01", "heading": "内景",
                                  "characters": ["少女"], "text": "她走。",
                                  "dialogue": []}]},
           "evidence": [{"round": 1}]}
    inner = _Inner()
    ra = ReplayAgent(inner, [rec], agent_id="screenwriter")
    from little_crew.agent.protocol import CallEnvelope, Budget

    out = ra(envelope=CallEnvelope.root("screenwriter", "script.draft", {},
                                       Budget.unlimited()), runtime=None)
    check("C1 ★★★ 命中 -> 返回旧产物且**不调真实现**",
          bool(out.ok) and inner.calls == 0, str(inner.calls))
    check("C2 ★★ 还原成真对象（有 scenes）",
          bool(getattr(out.output.get("script.draft") or out.output.get("script.final"),
                       "scenes", ())), str(type(out.output)))
    check("C3 ★★ 记录里写明命中", ra.as_dict()["replayed"] == 1, str(ra.as_dict()))
    out2 = None
    inner2 = _Inner()
    ra2 = ReplayAgent(inner2, [], agent_id="screenwriter")
    try:
        ra2(envelope=CallEnvelope.root("screenwriter", "script.draft", {},
                                      Budget.unlimited()), runtime=None)
    except AssertionError:
        out2 = "fell-through"
    check("C4 ★★★ 没记录 -> 退回真实现（宁可慢，不造假）",
          out2 == "fell-through" and inner2.calls == 1, str(inner2.calls))
    check("C5 ★★ 坏记录不塞半成品", _restore({"kind": "x"}, "script.draft") is None, "")
    check("C6 ★★★ 分镜能还原成 ShotList",
          _restore({"kind": "shot.list", "episodeId": "E1",
                    "shots": [{"shot_id": "SH1", "scene_id": "S01",
                               "shot_size": "full", "camera_movement": "static",
                               "duration_sec": 5.0, "characters": ["少女"],
                               "description": "她走。"}]},
                   "shot.list") is not None, "")

    print(); print("E. previs 是可选阶段（2026-11-12）")
    with tempfile.TemporaryDirectory() as td3:
        wd3 = Path(td3)
        SC.save(wd3, "script", {"draft": {"scenes": []}}, meta={"episodeId": "EP9"})
        SC.save(wd3, "storyboard", {"shotList": []}, meta={"episodeId": "EP9"})
        # 故意**不写** previs：模拟老工作目录
        r = SC.check(wd3, "render", inputs={"episodeId": "EP9"})
        check("E1 ★★★ 没有 previs 的旧 checkpoint 仍可续跑（不被无故拒绝）",
              r["ok"], r.get("reason", ""))
        SC.save(wd3, "previs", {"firsts": {}}, meta={"episodeId": "EP9", "shots": 0})
        r2 = SC.check(wd3, "render", inputs={"episodeId": "EP9"})
        check("E2 ★★ 有 previs 时计入复用清单",
              r2["ok"] and "previs" in r2["reuse"], str(r2["reuse"]))

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
