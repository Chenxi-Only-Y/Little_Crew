"""质检员自检（2026-11-12）：`qc_inspector` 职位。

锁住四件事：
1 **只判不渲染**（产物是 `qc.report`，含 actions 指令）
2 **0 镜可判必须留痕**（不能把"没看"说成"没问题"）
3 **词表与执行侧一致**（`RERENDER_KINDS` 两边相等 —— 漂移即失败）
4 走编排器时**产物进黑板、调用进账本**（这是它存在的理由）
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def _png(p: Path) -> str:
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    return str(p)


class _Client:
    """假视觉模型：可配置"崩"或"好"。"""

    model_id = "deepseek-flash"

    def __init__(self, faces: str = "broken", blank: bool = False):
        self.faces = faces
        self.blank = blank
        self.calls = 0

    def chat_image(self, *a, **kw):
        self.calls += 1
        return json.dumps({"blank": self.blank, "faces": self.faces,
                           "matches": 4, "problems": []})


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("质检员（qc_inspector）—— 自检")
    print("=" * 66)

    from little_crew.review.qc_inspector import QcInspector

    print(); print("A. 只判不渲染（产物是指令）")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        imgs = {sid: _png(d / f"{sid}.png") for sid in ("SH1", "SH2")}
        rep = QcInspector(client=_Client("broken")).inspect(imgs)
        check("A1 ★★★ 产出 kind=qc.report", rep.get("kind") == "qc.report", str(rep.get("kind")))
        check("A2 ★★★ 崩镜进 findings（severity=error）",
              any(f["severity"] == "error" for f in rep["findings"]),
              str([f["kind"] for f in rep["findings"]])[:60])
        check("A3 ★★★ 同时给出 actions（红字：redraw）",
              bool(rep["actions"]) and all(a["action"] == "redraw" for a in rep["actions"]),
              str(rep["actions"])[:80])
        check("A4 ★★★ 只列不重复（同镜只下一次指令）",
              len({a["shotId"] for a in rep["actions"]}) == len(rep["actions"]),
              str([a["shotId"] for a in rep["actions"]]))
        check("A5 ★★ 不写黑板、不渲染（函数是纯的）",
              not any(k.startswith("_") for k in rep) and "artifacts" not in rep, "")

    print(); print("B. 0 镜可判必须留痕")
    with tempfile.TemporaryDirectory() as td2:
        d2 = Path(td2)
        bad = {"SH9": str(d2 / "missing.png")}      # 文件不存在 -> 读图失败
        rep2 = QcInspector(client=_Client("ok")).inspect(bad)
        check("B1 ★★★ reviewedCount=0 时 notJudged=True（等于没审）",
              rep2["reviewedCount"] == 0 and rep2["notJudged"] is True, str(rep2)[:80])
        check("B2 ★★ 且列出没审到的镜（notReviewed）",
              "SH9" in (rep2.get("notReviewed") or []), str(rep2.get("notReviewed")))
        check("B3 ★★★ 没审到不得产出 redraw 指令",
              rep2["actions"] == [], str(rep2["actions"]))

    print(); print("C. 词表（对齐断言放在 runtime 侧）")
    from little_crew.review import qc_inspector as qc

    # ⚠️ 这里**不** import `runtime.vision_rework`：`review` 层不得依赖 `runtime` 层
    # （R1 反向依赖，架构守卫会拦 —— 实测抓到过）。
    # 两边词表是否一致，由 `runtime/vision_rework_check.py` 断言（它能向下依赖 review）。
    check("C1 ★★★ 质检员声明了重画词表（非空）",
          bool(tuple(qc.RERENDER_KINDS)), str(qc.RERENDER_KINDS))

    print(); print("D. 走编排器：进黑板 + 进账本")
    from little_crew.agent.orchestrator import CrewOrchestrator
    from little_crew.agent.protocol import Budget

    with tempfile.TemporaryDirectory() as td3:
        d3 = Path(td3)
        imgs3 = {"SH1": _png(d3 / "a.png")}
        o = CrewOrchestrator(budget=Budget(steps=100, wall_ms=60_000))
        o.bind("qc_inspector", QcInspector(client=_Client("ok")))
        out = o.run_stage("qc_inspector", "shot.qc",
                          {"shot.image": imgs3, "descriptions": {"SH1": "少女"}})
        check("D1 ★★★ 职位调用成功", out.ok, str(getattr(out, "reason", ""))[:60])
        check("D2 ★★★ qc.report 进了黑板",
              isinstance(o.blackboard.value("qc.report"), dict),
              str(type(o.blackboard.value("qc.report")).__name__))
        check("D3 ★★★ 这次调用**进了预算账本**（steps >= 1）",
              o.ledger.spent_steps >= 1, str(o.ledger.spent_steps))
        check("D4 ★★ 也进了调用树（span 里能看到 qc_inspector）",
              any(str(s.get("callee")) == "qc_inspector" for s in o.runtime.spans()),
              str([s.get("callee") for s in o.runtime.spans()][:3]))

    print(); print("E. 没有图 -> 明确失败（不假装通过）")
    from little_crew.agent.protocol import CallEnvelope

    inst = QcInspector(client=_Client("ok"))
    env = CallEnvelope.root("qc_inspector", "shot.qc", {}, None)
    res = inst(envelope=env, runtime=None)
    check("E1 ★★★ payload 没有图 -> F-NO-IMAGES",
          not res.ok and res.error_code == "F-NO-IMAGES", str(res.error_code))

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
