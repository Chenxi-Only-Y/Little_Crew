"""质检员（`qc_inspector`）：逐镜抽检画面，判「崩没崩」，并**下返工指令**（2026-11-12）。

**为什么放在 `review/` 而不是 `agent/`**：它调用 `review.vision_review` 与
`agent.llm`。若放在 `agent/`，就成了 `agent -> review` 的**反向依赖**
（架构守卫 R1 会拦，实测抓过）。实现放在哪一层不影响名册 ——
职位是编排概念，实现是代码概念。

## 与"视觉闸"的关系

视觉闸（`review/vision_review.py`）是**看图的本事**；本职位是**把它变成职位**：
- 之前：`episode._stage_vision` **直接** `new LlmClient()` 调模型 →
  24 次调用**不进预算账本、不进调用树**、也没有跨族检查。
- 现在：走 `run_stage("qc_inspector", "shot.qc", …)` →
  **自动**进 `BudgetLedger`（`reserve/settle`）、进 `_spans`、走编排器的授权与自签收检查。

## 职责边界（重要）

| 做什么 | 不做什么 |
|---|---|
| 看图判 `blank` / `faces=broken` / 一致度 | **不重画** —— 重画由 `run()` 读 `actions` 后执行 |
| 产出 `qc.report`（findings + actions） | 不决定"整集能不能交付"（那是审片/门禁的事） |
| 0 镜可判时显式标 `reviewedCount=0` | 不把"没看"说成"没问题" |

**为什么"判"和"改"要分开**：真实 QC 也是**打回单**，不是自己上手重做；
渲染要碰 IR/缓存/通道，塞进职位只会把两件事搅在一起（踩过：视觉闸判在重渲之后）。

## 产物格式

```
qc.report = {
  "kind": "qc.report",
  "reviewedCount": 12, "total": 40, "errors": 0,
  "findings": [{"shotId": "...", "kind": "faces_broken", "severity": "error",
                "reason": "..."}],
  "actions":  [{"shotId": "...", "action": "redraw", "reason": "..."}],
  "sampled":  ["SH-..."],
  "skipped":  ["SH-..."],
}
```
`actions` 是**指令**，不是执行结果 —— `run()` 执行后另写 `vision_rework.json`。
"""

from __future__ import annotations

from typing import Any, Mapping

from little_crew.agent.protocol import CallResult

#: 默认抽检多少镜（成本可控；与 `vision_review.DEFAULT_MAX_SHOTS` 一致）
DEFAULT_MAX_SHOTS = 12

#: 判出这些 kind 就要重画。
#:
#: ⚠️ 这里**不 import** `runtime.vision_rework.RERENDER_KINDS`：
#: `agent` 层不得依赖 `runtime` 层（R1 反向依赖，会挂架构守卫）。
#: 代价是"两套词表"可能漂移 —— 因此有一条**对齐测试**
#: （`qc_inspector_check` 里断言两边相等），漂移即失败。
RERENDER_KINDS = ("vision.faces-broken", "vision.blank")


class QcInspector:
    """质检员实现：`impl(*, envelope, runtime) -> CallResult`。

    payload 约定：
      `{"shot.image": {shotId: 图片路径}, "descriptions": {shotId: 镜头描述}}`
    取不到图片的镜**不算审过**（记 skipped），绝不默认通过。
    """

    def __init__(self, *, client: Any = None, max_shots: int = DEFAULT_MAX_SHOTS) -> None:
        self.client = client
        self.max_shots = max(0, int(max_shots))

    # ---- 实现契约 ------------------------------------------------------

    def __call__(self, *, envelope: Any, runtime: Any) -> CallResult:
        payload = dict(getattr(envelope, "payload", {}) or {})
        images = dict(payload.get("shot.image") or {})
        descs = dict(payload.get("descriptions") or {})
        if not images:
            # 没有图不是"通过"，是"没法判" —— 显式失败
            return CallResult.fail("F-NO-IMAGES",
                                   "质检需要 shot.image，但 payload 里一张图都没有")
        try:
            report = self.inspect(images, descs)
        except Exception as exc:                              # noqa: BLE001
            return CallResult.fail("F-QC", f"{type(exc).__name__}: {exc}")
        return CallResult.success({"qc.report": report},
                                  evidence={"sampled": len(report.get("sampled") or ()),
                                            "reviewed": report.get("reviewedCount", 0)})

    # ---- 纯函数：判 + 出指令 -------------------------------------------

    def inspect(self, images: Mapping[str, str],
                descs: Mapping[str, str] | None = None) -> dict:
        """看图 -> `qc.report`。**不渲染、不写黑板**，便于单测。"""
        from little_crew.review import vision_review as vr

        descs = dict(descs or {})
        # `vision_review.review()` 要的是"镜头对象"，这里用最轻的替身：
        # 它只读 `shot_id` 与 `description`（见该模块 `_label`）。
        shots = [type("_S", (), {"shot_id": sid,
                                 "description": descs.get(sid, "")})()
                 for sid in images]
        client = self.client or _default_client()
        res = vr.review(shots, dict(images), client=client,
                        max_shots=self.max_shots)
        findings = list(res.get("findings") or ())
        actions = []
        seen = set()
        for f in findings:
            if str(f.get("severity")) != "error":
                continue
            if str(f.get("kind")) not in RERENDER_KINDS:
                continue
            sid = str(f.get("shotId") or "")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            actions.append({"shotId": sid, "action": "redraw",
                            "reason": str(f.get("reason") or "")[:120]})
        return {
            "kind": "qc.report",
            "reviewedCount": int(res.get("reviewedCount") or 0),
            "total": int(res.get("total") or len(images)),
            "errors": int(res.get("errors") or 0),
            "findings": findings,
            "actions": actions,
            # `vision_review.review()` 的 `skipped` 是**计数**（不是列表），
            # 真实"跳过了哪些镜"看 `rows` 里 `reviewed=False` 的那些。
            "skipped": int(res.get("skipped") or 0),
            "notReviewed": sorted(r.get("shotId", "") for r in (res.get("rows") or ())
                                  if not r.get("reviewed")),
            # 0 镜可判时**必须**留痕：等于没审
            "notJudged": int(res.get("reviewedCount") or 0) == 0,
        }


def _default_client() -> Any:
    """默认客户端：45 秒超时（EF29 实测：默认 120s 会让 12 镜拖 13 分钟）。"""
    from little_crew.agent.llm import LlmClient

    return LlmClient("deepseek-flash", timeout_s=45)
