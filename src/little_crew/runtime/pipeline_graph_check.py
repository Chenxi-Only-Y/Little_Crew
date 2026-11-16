"""无限画布后端自检（2026-11-16）：把一次跑批转成可渲染的依赖图。

锁住五件事：
1 **节点**：19 个职位 + 通道节点（渲染/合成/证据）+ 外部来源（小说）
2 **边**：按名册 `produces/requires` 的产物连边（如场景设计师 -> 分镜师·scene.library）
3 **状态**：只按**证据文件是否存在**推断 done/pending；未启用 = skipped（不冒充完成）
4 **费用**：只汇总能对账的（image_calls / lipsync），不估算
5 **落盘**：`delivery/pipeline.graph.json` 可被前端直接读
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 68)
    print("无限画布后端（pipeline graph）—— 自检")
    print("=" * 68)

    from little_crew.agent.roster import DEFAULT_ROSTER
    from little_crew.runtime import crew_profile as CP
    from little_crew.runtime import pipeline_graph as PG

    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        d = wd / "delivery"
        d.mkdir()
        # 造证据：场景库/合规/图片台账/口型台账
        (d / "scene.library.json").write_text("{}", encoding="utf-8")
        (d / "compliance.json").write_text("{}", encoding="utf-8")
        (d / "image_calls.json").write_text(
            json.dumps({"cloud": {"estCostCny": 0.3}}), encoding="utf-8")
        (d / "lipsync.json").write_text(
            json.dumps({"costCny": 0.75}), encoding="utf-8")
        full = CP.resolve(profile="full")
        g = PG.build_graph(roster=DEFAULT_ROSTER, crew=full, workdir=wd,
                           episode_id="E1")
        nodes = {n["id"]: n for n in g["nodes"]}
        check("A1 ★★★ 包含全部 19 个职位节点",
              sum(1 for n in g["nodes"] if n["type"] == "position") == 19, "")
        check("A2 ★★★ 含通道节点（渲染/合成/证据链）",
              {"code:shot.image", "code:episode.master",
               "code:delivery.evidence"} <= set(nodes),
              str(sorted(k for k in nodes if k.startswith("code:"))))
        check("A3 ★★ 含外部来源（源小说）",
              any(s["artifact"] == "source.novel" for s in g["sources"]), "")

        check("B1 ★★★ 产物契约连边（场景设计师 -> 分镜师 scene.library）",
              any(e["from"] == "scene_designer" and e["to"] == "storyboard_artist"
                  and e["artifact"] == "scene.library" for e in g["edges"]),
              str(g["edges"][:2]))
        check("B2 ★★★ 渲染通道 -> 审片/质检（shot.image 是通道产物）",
              any(e["from"] == "code:shot.image"
                  and e["artifact"] == "shot.image" for e in g["edges"]), "")

        check("C1 ★★★ 有证据的场景设计师 = done",
              nodes["scene_designer"]["status"] == "done",
              nodes["scene_designer"]["status"])
        check("C2 ★★★ 无证据的职位 = pending（不当完成）",
              nodes["gaffer"]["status"] == "pending", nodes["gaffer"]["status"])
        check("C3 ★★★ 证据路径写进节点（前端可点开）",
              "delivery/scene.library.json"
              in nodes["scene_designer"]["evidence"], "")
        check("C4 ★★ 费用只汇总可对账项",
              g["costs"].get("imageCny") == 0.3
              and g["costs"].get("lipsyncCny") == 0.75, str(g["costs"]))

        mini = CP.resolve(profile="minimal")
        g2 = PG.build_graph(roster=DEFAULT_ROSTER, crew=mini, workdir=wd,
                            episode_id="E2")
        n2 = {n["id"]: n for n in g2["nodes"]}
        check("D1 ★★★ 未启用职位标 skipped（不冒充完成）",
              n2["gaffer"]["status"] == "skipped"
              and n2["gaffer"]["active"] is False, "")
        check("D2 ★★★ 未启用的边不画（图与真实执行一致）",
              not any(e["to"] == "gaffer" for e in g2["edges"]), "")
        check("D3 ★★ 图摘要带 active/done/skipped 计数",
              g["summary"]["active"] == 19 and g["summary"]["edges"] > 0,
              json.dumps(g["summary"], ensure_ascii=False))

        p = PG.write_graph(wd, g)
        ok_file = p.is_file()
        obj = json.loads(p.read_text(encoding="utf-8")) if ok_file else {}
        check("E1 ★★★ 落盘 delivery/pipeline.graph.json 且可解析",
              ok_file and obj.get("kind") == "pipeline.graph",
              str(p))
        check("E2 ★★★ 图自带编制口径（preset/hash/active）",
              (obj.get("crew") or {}).get("preset") == "full"
              and len(str((obj.get("crew") or {}).get("hash") or "")) == 12, "")

    n = sum(1 for _, ok, _ in _r if ok)
    print()
    for name, ok, detail in _r:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 68)
    print(f"结果：{n}/{len(_r)} 通过")
    return 0 if n == len(_r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
