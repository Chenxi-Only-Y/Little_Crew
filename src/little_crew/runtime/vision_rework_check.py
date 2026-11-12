"""视觉驱动重渲自检（纯逻辑 + 假渲染器，不烧 GPU）。"""
from __future__ import annotations

import sys

from little_crew.runtime import vision_rework as VRW

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def _ir(sid, att=0):
    return {"graphId": f"g-{sid}",
            "provenance": {"seedDerivation": {
                "projectSeed": 20261108, "episodeIdx": 0,
                "sceneIdx": 0, "shotIdx": 1, "attempt": att}}}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("视觉驱动重渲 —— 自检")
    print("=" * 66)

    print(); print("A. 挑目标")
    fs = [{"shotId": "S1", "kind": "vision.faces-broken", "reason": "脸崩"},
          {"shotId": "S2", "kind": "vision.low-match", "reason": "低一致"},
          {"shotId": "S1", "kind": "vision.blank", "reason": "重复"},
          {"shotId": "S3", "kind": "vision.blank", "reason": "空白"}]
    t = VRW.plan(fs)
    check("A1 ★★★ 只挑 error 级（warn 不动 GPU）",
          [x["shotId"] for x in t] == ["S1", "S3"], str([x["shotId"] for x in t]))
    check("A2 ★★ 同镜去重", sum(1 for x in t if x["shotId"] == "S1") == 1, "")
    t2 = VRW.plan(fs, max_shots=1)
    check("A3 ★★ 上限生效", len(t2) == 1, str(len(t2)))
    check("A4 空输入", VRW.plan([]) == [], "")

    print(); print("B. attempt 递增（换种子 = 缓存必然 miss）")
    ir = _ir("S1", 0)
    ir2, att = VRW.bump_attempt(ir)
    check("B1 ★★★ attempt 0 -> 1", att == 1, str(att))
    check("B2 ★★★ 原 IR 不被改（纯函数）",
          ir["provenance"]["seedDerivation"]["attempt"] == 0, str(ir))
    check("B3 ★★ 新 IR 独立", ir2 is not ir and ir2["provenance"] is not ir["provenance"], "")
    check("B4 ★★ 其它字段保留", ir2.get("graphId") == "g-S1", str(ir2.get("graphId")))
    ir3, att3 = VRW.bump_attempt({"graphId": "x"})
    check("B5 ★★ 没有 seedDerivation 也能补（不猜种子）",
          att3 == 1 and "seedDerivation" in ir3["provenance"], str(ir3))

    print(); print("B2. 真哈希：attempt 变了缓存才 miss")
    try:
        from little_crew.ir.canonical import content_hash

        _irx = {"irVersion": "1.0.0", "graphId": "G-E-S01-001",
               "scope": {"episodeId": "E", "sceneId": "S01", "shotId": "SH1"},
               "nodes": [{"id": "n1", "kind": "zimage.sampler",
                          "params": {"steps": 8}}],
               "edges": [],
               "provenance": {"seedDerivation": {
                   "projectSeed": 20261108, "episodeIdx": 0, "sceneIdx": 0,
                   "shotIdx": 0, "attempt": 0}}}
        _ir2, _ = VRW.bump_attempt(_irx)
        check("B6 ★★★ 改 attempt 会改内容哈希（否则重画=命中旧图）",
              content_hash(_irx) != content_hash(_ir2), "")
    except Exception as e:                                    # noqa: BLE001
        check("B6 ★★★ 改 attempt 会改内容哈希", False, f"{type(e).__name__}: {e}")

    print(); print("C. 重画流程")
    calls = []

    def fake_render(irs):
        calls.append(irs)
        return {"batch": [{"path": f"cache/{irs[0]['graphId']}_a{irs[0]['provenance']['seedDerivation']['attempt']}.png"}]}

    r = VRW.run([{"shotId": "S1", "kind": "vision.faces-broken", "reason": "脸崩"}],
                {"S1": _ir("S1")}, fake_render)
    check("C1 ★★★ 真调了渲染", len(calls) == 1, str(len(calls)))
    check("C2 ★★★ 产物被记下", r["count"] == 1 and r["rerendered"][0]["artifacts"], str(r["rerendered"]))
    check("C3 ★★ attempt 传进渲染的 IR", calls[0][0]["provenance"]["seedDerivation"]["attempt"] == 1, "")
    r2 = VRW.run([{"shotId": "NOPE", "kind": "vision.blank"}], {}, fake_render)
    check("C4 ★★ 没有 IR -> skipped（不静默）", len(r2["skipped"]) == 1, str(r2["skipped"]))
    r3 = VRW.run([{"shotId": "S1", "kind": "vision.blank"}], {"S1": _ir("S1")},
                 lambda irs: {"batch": []})
    check("C5 ★★★ 渲染没产物 -> failed", len(r3["failed"]) >= 1 and r3["count"] == 0, str(r3["failed"]))
    r4 = VRW.run([{"shotId": "S1", "kind": "vision.blank"}], {"S1": _ir("S1")},
                 lambda irs: (_ for _ in ()).throw(RuntimeError("GPU OOM")))
    check("C6 ★★ 渲染抛异常 -> failed（含原因）",
          "OOM" in str(r4["failed"][0].get("error", "")), str(r4["failed"])[:80])
    check("C7 ★★ 有上限（不无限重试）",
          len(VRW.run([{"shotId": "S1", "kind": "vision.blank"}], {"S1": _ir("S1")},
                      lambda irs: {"batch": []}, max_attempts=2)["failed"]) == 2, "")
    print(); print("F. 与质检员的词表对齐（2026-11-12）")
    from little_crew.review import qc_inspector as _qc

    check("F1 ★★★ 执行侧 RERENDER_KINDS == 质检员侧（漂移即失败）",
          tuple(VRW.RERENDER_KINDS) == tuple(_qc.RERENDER_KINDS),
          f"vr={VRW.RERENDER_KINDS} qc={_qc.RERENDER_KINDS}")


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
