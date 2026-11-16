"""报告页自检（2026-11-16）：新产物 + 静态流水线图（B1）。

锁住六件事：
1 报告页能读**新产物**（编制/合规/交付标识/口型/相机/场景卡/造型/后期/费用）
2 **静态流水线图**真的画出来（内联 SVG、节点、边、状态、图例）—— 无 JS/无 CDN
3 缺失产物时**不崩**（旧 run 也能出报告，只显示"无"）
4 逐镜图兜底：cache 解析不到时读 `assets/shots/<sid>/*.png`
5 后期统筹**不打印原始嵌套 dict**（会把整页撑爆）
6 口径：编制未验证项、合规 needs_human_review 如实展示，不写"通过"
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def _w(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _fake_png(path: Path) -> None:
    # 1x1 透明 PNG（报告只引用不解析）
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000a49444154789c63000100000500010d0a2db40000"
        "000049454e44ae426082"))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 68)
    print("报告页（B1：新产物 + 静态流水线图）—— 自检")
    print("=" * 68)

    from little_crew.runtime.report_html import build_report

    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        d = wd / "delivery"
        d.mkdir()
        _w(d / "run.report.json", {
            "episodeId": "E1", "ok": True, "scriptRounds": 2, "scriptVerdict": "PASS",
            "totalSeconds": 24.0, "wallSeconds": 88, "coverage": "1.0",
            "rendered": 4, "cacheHits": 0,
            "crew": {"preset": "standard", "hash": "abc123def456",
                     "active": ["screenwriter", "storyboard_artist"],
                     "skipped": ["compliance_officer"],
                     "autoAdded": ["scene_designer"],
                     "verification": {"reviewed": True, "qualityChecked": True,
                                      "complianceAudited": False},
                     "unverified": ["compliance_officer"], "level": "reduced"},
        })
        _w(d / "episode.master.json", {
            "title": "测试集", "episodeId": "E1", "shotCount": 1,
            "totalSeconds": 24.0, "createdAt": "2026-11-16",
            "shots": [{"shotId": "S1", "shotSize": "close", "cameraMovement": "static",
                       "durationSec": 6, "characters": ["少女"], "ready": True,
                       "artifacts": ["missing-name.png"],
                       "prompt": "雨夜站台"}],
        })
        _w(d / "pipeline.graph.json", {
            "kind": "pipeline.graph",
            "crew": {"preset": "standard", "hash": "abc123def456"},
            "nodes": [
                {"id": "scene_designer", "type": "position", "title": "场景设计师",
                 "stage": "asset", "status": "done", "active": True},
                {"id": "storyboard_artist", "type": "position", "title": "分镜师",
                 "stage": "storyboard", "status": "done", "active": True},
                {"id": "gaffer", "type": "position", "title": "灯光师",
                 "stage": "storyboard", "status": "skipped", "active": False},
                {"id": "code:shot.image", "type": "channel", "title": "渲染通道（出图）",
                 "stage": "render", "status": "done", "active": True},
            ],
            "edges": [
                {"from": "scene_designer", "to": "storyboard_artist",
                 "artifact": "scene.library"},
                {"from": "code:shot.image", "to": "storyboard_artist",
                 "artifact": "shot.image"},
            ],
            "sources": [{"id": "source:source.novel", "title": "源素材/小说",
                         "artifact": "source.novel"}],
            "summary": {"positions": 19, "active": 13, "done": 10,
                        "pending": 6, "skipped": 6, "edges": 17},
        })
        _w(d / "compliance.json", {
            "status": "needs_human_review", "blocking": False,
            "licenseAudit": {"denied": [], "needsReview": [{"modelId": "m"}]},
            "deliveryAudit": {"ok": True},
            "contentFirstPass": {"performed": True, "reviewed": 2},
            "humanReview": [{"type": "content", "why": "待法人复审商标"}],
        })
        _w(d / "ai_labels.json", {
            "aiLabels": {"explicit": True, "implicit": True,
                         "where": ["delivery/ai_label_endcard.png"]},
            "credits": "本作品由 MiniMax H3 生成",
            "sources": [{"kind": "model"}], "sourceRegistryOk": False,
        })
        _w(d / "lipsync.json", {
            "enabled": True, "okCount": 1, "costCny": 0.75,
            "targets": ["S1"],
            "rows": [{"shotId": "S1", "shotSeconds": 6.0, "clipSeconds": 6.0,
                      "fit": {"method": "pad_freeze"}}],
        })
        _w(d / "camera_plan.json", {"default": {"focal": "85mm"},
                                    "perShot": {"S1": {"focal": "24mm"}}})
        _w(d / "scene_cards.json", {"cards": {"S01": {}}, "perShot": {"S1": {}}})
        _w(d / "outfits_used.json", {"perShot": {"S1": {"variant": "rain"}}})
        _w(d / "post.plan.json", {"audio": {"music": "generated"},
                                  "subtitle": {"style": "burned"},
                                  "loudness": {"targetLufs": -16.0},
                                  "transitions": [{"type": "cut"}]})
        _w(d / "image_calls.json", {"cloud": {"estCostCny": 0.2}})
        _w(d / "token_usage.json", {"total": {"promptTokens": 1000,
                                              "completionTokens": 500, "calls": 3}})
        _fake_png(wd / "assets" / "shots" / "S1" / "still.png")

        page = build_report(wd, title="B1 测试")
        html = page.read_text(encoding="utf-8")
        check("A1 ★★★ 报告页生成成功", page.is_file() and len(html) > 2000, str(len(html)))
        check("A2 ★★★ 新章节：剧组编制与流水线", "剧组编制与流水线" in html, "")
        check("A3 ★★★ 新章节：合规与交付标识", "合规与交付标识" in html, "")
        check("A4 ★★★ 新章节：增强能力", "增强能力" in html, "")

        _svg = html.split("<svg")[1].split("</svg>")[0] if "<svg" in html else ""
        check("B1 ★★★ 静态流水线图是内联 SVG（无 JS/无外部资源）",
              bool(_svg) and "<script" not in _svg and "src=" not in _svg
              and "href=" not in _svg, _svg[:60])
        check("B2 ★★★ 图画出了节点与边（rect/path）",
              html.count("<rect") >= 5 and html.count("<path") >= 3,
              f"rect={html.count('<rect')} path={html.count('<path')}")
        check("B3 ★★★ 图例与状态口径存在",
              "done（有证据）" in html and "skipped（编制未启用）" in html, "")
        check("B4 ★★ 边带产物名（悬停可看）", "<title>scene.library</title>" in html, "")

        check("C1 ★★★ 编制口径：模板/指纹/自动补齐/验证级别",
              "standard" in html and "abc123def456" in html
              and "scene_designer" in html and "reduced" in html, "")
        check("C2 ★★★ 合规状态如实展示（needs_human_review，不写通过）",
              "needs_human_review" in html and "人工/法务复审" in html, "")
        check("C3 ★★★ 交付标识：显式/隐式 + 署名 + 素材登记缺项",
              "显式标识" in html and "隐式标识" in html
              and "MiniMax H3" in html and "缺项" in html, "")
        check("C4 ★★★ 口型：费用与时长对齐方式",
              "¥0.75" in html and "pad_freeze" in html, "")
        check("C5 ★★ 相机/场景卡/造型 摘要", "85mm" in html
              and "场景卡数" in html and "rain" in html, "")
        check("C6 ★★★ 后期统筹不打印原始嵌套 dict（避免撑爆页面）",
              "{'style'" not in html and "burned" in html, "")
        check("C7 ★★ 逐镜图兜底到 assets/shots（cache 找不到时）",
              "assets/shots/S1/still.png" in html, "")

    # 缺失产物：旧目录也必须能出报告
    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        (wd / "delivery").mkdir()
        _w(wd / "delivery" / "run.report.json", {"episodeId": "OLD", "ok": False})
        page = build_report(wd)
        html = page.read_text(encoding="utf-8")
        check("D1 ★★★ 没有任何新产物时仍生成报告（旧 run 兼容）",
              page.is_file() and "没有 pipeline.graph.json" in html, "")
        check("D2 ★★ 旧 run 也明确标「未记录编制」", "没有记录剧组编制" in html, "")

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
