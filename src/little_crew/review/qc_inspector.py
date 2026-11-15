"""场记的**质检实现**（2026-11-12 建立，2026-11-15 并入「场记」职位）。

**为什么放在 `review/` 而不是 `agent/`**：它调用 `review.vision_review` 与
`agent.llm`。若放在 `agent/`，就成了 `agent -> review` 的**反向依赖**
（架构守卫 R1 会拦，实测抓过）。实现放在哪一层不影响名册 ——
职位是编排概念，实现是代码概念。

## 与"视觉闸"的关系

视觉闸（`review/vision_review.py`）是**看图的本事**；本职位是**把它变成职位**：
- 之前：`episode._stage_vision` **直接** `new LlmClient()` 调模型 →
  24 次调用**不进预算账本、不进调用树**、也没有跨族检查。
- 现在：走 `run_stage("continuity_supervisor", "shot.qc", …)` →
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

#: 跨镜审计最多比对几对相邻镜头（成本可控；默认 3 对）
CROSS_PAIR_MAX = 3

#: 跨镜一致性检查项（只报警，**不返工** —— 返工权在单镜质检）
_CROSS_PROMPT = """你是场记，正在做**跨镜一致性审计**。
左图与右图是同一部短片里**相邻的两个镜头**（顺序：左 -> 右）。
只判断**跨镜一致性**，不要评价画面好坏：
1. 同一角色：脸型/发型/服装/配饰是否一致
2. 关键道具：位置、外观、数量是否连贯
3. 场景空间：同一场戏的空间结构、家具、光源方向是否一致
4. 视线/轴线：人物视线方向、机位轴线是否跳轴
镜头描述（左 / 右）：{desc}
严格输出 JSON（不要解释）：
{{"consistent": true|false,
 "issues": [{{"kind": "character|prop|space|axis|other",
             "severity": "low|high", "reason": "一句话"}}],
 "confidence": 0.0-1.0}}
若图像不可读或无法判断，输出 {{"consistent": null, "issues": []}}。"""




def _json_of(text: str):
    """从模型回复里抠 JSON（容忍 ```json 包裹与前后废话）。"""
    s = str(text or "").strip()
    if s.startswith("```"):
        parts = s.split("```")
        s = parts[1] if len(parts) > 1 else s[3:]
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    import json as _j

    try:
        return _j.loads(s[i:j + 1])
    except Exception:                                         # noqa: BLE001
        return None


def side_by_side(path_a: str, path_b: str, *, height: int = 420):
    """把两张图拼成左右并排版（一次视觉调用判一对）。没有 Pillow 就返回 None。"""
    try:
        from io import BytesIO

        from PIL import Image
    except Exception:                                         # noqa: BLE001
        return None
    try:
        a = Image.open(str(path_a)).convert("RGB")
        b = Image.open(str(path_b)).convert("RGB")

        def _fit(im):
            w = max(1, int(im.width * (height / max(1, im.height))))
            return im.resize((w, height))

        a, b = _fit(a), _fit(b)
        gap = 12
        canvas = Image.new("RGB", (a.width + gap + b.width, height), (24, 24, 24))
        canvas.paste(a, (0, 0))
        canvas.paste(b, (a.width + gap, 0))
        buf = BytesIO()
        canvas.save(buf, format="JPEG", quality=86)
        import base64

        return base64.b64encode(buf.getvalue()).decode()
    except Exception:                                         # noqa: BLE001
        return None


def cross_shot_audit(shots, images, *, client, max_pairs: int = CROSS_PAIR_MAX,
                     descs=None) -> dict:
    """跨镜一致性审计（**只报警，不返工**）。纯函数，便于单测。

    Args:
        shots: 镜头序列（要有 `shot_id`），相邻两镜为一对。
        images: `{shotId: 图片路径}`。
        client: 视觉客户端（`chat_image`）；None = 如实标"未做"。
        max_pairs: 最多比对几对（成本上限）。
    """
    descs = dict(descs or {})
    order = []
    for s in (shots or ()):
        sid = str(getattr(s, "shot_id", "") or "")
        if sid:
            order.append(sid)
    pairs: list[dict] = []
    issues: list[dict] = []
    skipped: list[dict] = []
    judged = 0
    limit = max(0, int(max_pairs))
    for a, b in zip(order, order[1:]):
        if len(pairs) + len(skipped) >= limit:
            break
        pa, pb = str((images or {}).get(a) or ""), str((images or {}).get(b) or "")
        if not pa or not pb:
            skipped.append({"pair": [a, b], "why": "缺图"})
            continue
        if client is None:
            skipped.append({"pair": [a, b], "why": "未绑定视觉模型"})
            continue
        data = side_by_side(pa, pb)
        if not data:
            skipped.append({"pair": [a, b], "why": "无法拼图（缺 Pillow）"})
            continue
        desc = f"{descs.get(a, '')[:60]} / {descs.get(b, '')[:60]}"
        try:
            reply = client.chat_image(_CROSS_PROMPT.replace("{desc}", desc),
                                     data, what="跨镜一致性审计",
                                     max_tokens=400)
            text = reply if isinstance(reply, str) else getattr(reply, "text", "")
            obj = _json_of(text)
        except Exception as e:                                # noqa: BLE001
            skipped.append({"pair": [a, b], "why": f"{type(e).__name__}: {str(e)[:40]}"})
            continue
        if not isinstance(obj, dict) or obj.get("consistent") is None:
            skipped.append({"pair": [a, b], "why": "模型没给可判结论"})
            continue
        judged += 1
        pair_issues = []
        for it in (obj.get("issues") or ()):
            if not isinstance(it, dict):
                continue
            kind = str(it.get("kind") or "other")
            if kind not in ("character", "prop", "space", "axis"):
                kind = "other"
            rec = {"pair": [a, b], "kind": kind,
                   "severity": str(it.get("severity") or "low"),
                   "reason": str(it.get("reason") or "")[:120]}
            pair_issues.append(rec)
            issues.append(rec)
        pairs.append({"pair": [a, b], "consistent": bool(obj.get("consistent")),
                      "confidence": obj.get("confidence"), "issues": pair_issues})
    return {
        "kind": "continuity.report",
        "performed": judged > 0,
        # ⚠️ **advisoryOnly**：本报告只报警，不产生返工动作
        "advisoryOnly": True,
        "reviewedPairs": judged,
        "requestedPairs": limit,
        "pairs": pairs,
        "issues": issues,
        "skipped": skipped,
        # 一对都没判 = 等于没审（必须留痕，不能当通过）
        "notJudged": judged == 0,
    }

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
        # ---- 跨镜审计（2026-11-15）：capability 区分，产物是 continuity.report ----
        if str(getattr(envelope, "capability", "") or "") == "continuity.check":
            rep = cross_shot_audit(
                payload.get("shots") or (), images, client=self.client,
                max_pairs=int(payload.get("maxPairs") or self.max_shots
                              or CROSS_PAIR_MAX),
                descs=descs)
            return CallResult.success({"continuity.report": rep},
                                      evidence={"reviewedPairs": rep["reviewedPairs"],
                                                "issues": len(rep["issues"])})
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
