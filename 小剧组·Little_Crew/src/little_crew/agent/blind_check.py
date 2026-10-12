"""L3 跨模型族盲读 —— 自检。

要证明的核心是**三种情况的区分**（这是门禁三态铁律在 L3 上的体现）：

| 情况 | 含义 | 期望结局 |
|---|---|---|
| 审核方与生成方**同族** | 判定**不算数** | `ESCALATE`（升级人工） |
| 审核方**没产出意见** | 判定**缺失** | 约束**跳过**（如实记录） |
| 模型说画面**有问题** | 产物**真不合格** | `RETRY`（返工） |

**把三者混在一起是最危险的**：拿一份作废的判定去生成返工指令，
等于**基于不可信的观察指挥返工** —— 比不返工更糟。

`BlindReader` 的 `review_fn` 可注入 —— 所以本自检**不需要任何模型或 key**，
且三条路径都能穷尽测试。
"""

from __future__ import annotations

import sys

__all__ = ["main"]

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


# ---------------------------------------------------------------------------
# 夹具：造 ReviewFeedback（不依赖任何模型）
# ---------------------------------------------------------------------------

def _feedback(*, verdict: str = "PASS", reviewer: str = "qwen-vl",
              generator: str = "z-image", blocking: int = 0,
              claimed: float = 80.0, dims: dict | None = None):
    """造一份可用的 `ReviewFeedback`。

    ⚠️ **夹具的构造签名必须与真实 dataclass 一致。**
    写错时夹具在构造处抛 `TypeError`，而那个异常会被 `BlindReader`
    的 `except Exception` 吞掉 —— 那**是它的正确行为**
    （盲读挂了不该让整集跑不动）。于是症状是
    `available=False` 且 `values` 为空，看起来像被测代码有问题，
    实际是夹具自己没构造出来。

    本文件踩过一次：`Severity.BLOCKING` 实为 `BLOCKER`，
    且 `ReviewIssue` 用 `problem`/`suggestion` 而非 `message`。
    """
    from little_crew.agent.messages import (Dimension, IssueLocation,
                                            QcScore, RepairHint,
                                            ReviewFeedback, ReviewIssue,
                                            Severity, Verdict)

    score = QcScore(dimensions=dims or {Dimension.CONSISTENCY: claimed,
                                        Dimension.COMPLETENESS: claimed,
                                        Dimension.QUALITY: claimed},
                    claimed_score=claimed)
    issues = tuple(
        ReviewIssue(severity=Severity.BLOCKER, dimension=Dimension.QUALITY,
                    location=IssueLocation(shot="SH-1"),
                    problem=f"问题 {i}", suggestion="重画")
        for i in range(blocking))
    hints = ((RepairHint(graph_id="G-1", op="regenerate_asset", args={}),)
             if blocking else ())
    return ReviewFeedback(frm="review_board", to="orchestrator",
                          verdict=Verdict(verdict), score=score,
                          issues=issues, repair_hints=hints,
                          reviewer_model=reviewer, generator_model=generator)


# ==========================================================================
# A. 算子本身（纯函数）
# ==========================================================================

# ==========================================================================
# B. 适配器：三种情况的**映射**
# ==========================================================================

def _b_adapter_mapping() -> None:
    from little_crew.agent.blind import BlindReader

    # ---- ① 正常：模型说通过 ----
    r = BlindReader(review_fn=lambda *a, **k: ((_feedback(),), ""))
    out = r.read("SH-1")
    check("B1 正常审查 -> available",
          out.available, str(out.values))
    check("B2 裁定被转成观测值",
          out.values.get("blindVerdict") == "PASS", str(out.values))
    check("B3 带上两个模型 id（门禁可复核跨族）",
          bool(out.values.get("blindReviewerModel"))
          and bool(out.values.get("blindGeneratorModel")), str(out.values))

    # ---- ② ★★ 作废：available 但带 invalidReason ----
    r = BlindReader(review_fn=lambda *a, **k: ((), "与生成方同族，判定无效"))
    out = r.read("SH-1")
    check("B4 ★★ 判定作废 -> **available=True**（它是一次判定，只是不算数）",
          out.available, str(out.values))
    check("B5 ★★ 且带上作废原因（门禁据此走 ESCALATE 而非 RETRY）",
          "同族" in str(out.values.get("blindInvalidReason")),
          str(out.values))
    check("B6 `invalidated` 属性为真", out.invalidated, str(out.values))

    # ---- ③ ★★ 没产出意见：available=False（跳过，不是作废） ----
    r = BlindReader(review_fn=lambda *a, **k: ((), ""))
    out = r.read("SH-1")
    check("B7 ★★ 审核者没产出意见 -> **available=False**（约束会跳过）",
          not out.available, str(out.values))
    check("B8 且说明原因（可回答「为什么这条约束没执行」）",
          bool(out.unavailable_reason), out.unavailable_reason[:70])

    # ---- ④ ★★ 审核者炸了：也是 available=False，但不该让整集挂掉 ----
    def _boom(*a, **k):
        raise TimeoutError("VLM 超时")

    r = BlindReader(review_fn=_boom)
    out = r.read("SH-1")
    check("B9 ★★ 审核者超时 -> available=False（**不让整集跑不动**）",
          not out.available, str(out.values))
    check("B10 ★ 且留下具体原因（不是静默失效）",
          "TimeoutError" in out.unavailable_reason,
          out.unavailable_reason[:70])
    check("B11 没有编排器也没注入 fn -> 如实说不可用",
          not BlindReader().read("SH-1").available)

    # ---- ⑤ 多份意见取**最严** ----
    r = BlindReader(review_fn=lambda *a, **k: (
        (_feedback(verdict="PASS"), _feedback(verdict="REVISE")), ""))
    out = r.read("SH-1")
    check("B12 ★ 多份意见取**最严**的（一份说有问题就不该被盖过去）",
          out.values.get("blindVerdict") == "REVISE", str(out.values))

    # ---- ⑥ 阻断问题条数进证据 ----
    r = BlindReader(review_fn=lambda *a, **k: (
        (_feedback(verdict="REVISE", blocking=3),), ""))
    out = r.read("SH-1")
    check("B13 阻断级问题条数进证据",
          out.values.get("blindBlocking") == 3, str(out.values))

    # ---- ⑦ 无效判定时不该采信它的 verdict ----
    r = BlindReader(review_fn=lambda *a, **k: (
        (_feedback(verdict="PASS", reviewer="qwen", generator="qwen"),), ""))
    out = r.read("SH-1")
    check("B14 ★ 同族审核时 `effective_verdict` 归 ESCALATE"
          "（不采信它的 PASS）",
          out.values.get("blindVerdict") == "ESCALATE", str(out.values))


# ==========================================================================
# C. 端到端：门禁真的会被盲读拦住
# ==========================================================================

# ==========================================================================
# D. 与 IR 的对接
# ==========================================================================

def _d_ir_bridge() -> None:
    from little_crew.agent.ir_bridge import shot_to_ir
    from little_crew.agent.messages import Shot

    s = Shot(frm="a", to="b", shot_id="SH-D-1", scene_id="SC-D",
             description="少女走在山路上", shot_size="full",
             camera_movement="static", action="walk",
             characters=("少女",), duration_sec=5.0)
    cons = shot_to_ir(s)["constraints"]

    blind = [c for c in cons if c["assertion"]["op"] == "blind_read_verdict"]
    check("D1 ★ `shot_to_ir` 生成盲读约束", bool(blind), f"{len(blind)} 条")
    check("D2 ★★ 标 **hard**（盲读说有问题就该拦）",
          blind and blind[0]["enforcement"] == "hard",
          str([c["enforcement"] for c in blind]))
    check("D3 `allow` 默认只允许 PASS",
          blind and blind[0]["assertion"].get("allow") == ["PASS"],
          str(blind[0]["assertion"]) if blind else "-")
    check("D4 描述里说明「无 VLM 时如实跳过」",
          blind and "跳过" in blind[0].get("description", ""),
          (blind[0].get("description") if blind else "")[:80])

    # 级别对比：一致性是 audit，盲读是 hard —— 这个区别有依据
    ident = [c for c in cons if c["assertion"]["op"] == "identity_similarity"]
    check("D5 ★ 与一致性约束的级别**刻意不同**"
          "（一个能力不足、一个是真判定）",
          (not ident) or ident[0]["enforcement"] == "audit",
          f"identity={[c['enforcement'] for c in ident]} / "
          f"blind={[c['enforcement'] for c in blind]}")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:                                   # noqa: BLE001
        pass

    print("=" * 78)
    print("L3 跨模型族盲读 —— 自检")
    print("=" * 78)

    _b_adapter_mapping()
    _d_ir_bridge()

    print()
    npass = sum(1 for _, ok, _ in _results if ok)
    for name, ok, detail in _results:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 78)
    print(f"结果：{npass}/{len(_results)} 通过")
    return 0 if npass == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
