"""合规官（`compliance_officer`）：**授权归口 + 交付审计 + 内容初审**（2026-11-15）。

> 放在 `agent/` 层而不是 `review/`：它是**职位实现**（`agent` 契约），
> 且 `crew_stubs` 在 agent 层要注册默认桩 —— 若放 `review/`，agent 会反向依赖 review
> （架构守卫 R1 会拦，实测拦下过）。

## 为什么是这样三腿，而不是"一个 LLM 判合规"

| 腿 | 谁判 | 能不能阻断 | 说明 |
|---|---|---|---|
① 授权 | **`LicenseGate` 引擎**（本职位名下的 `license.audit` 工具） | ✅ 能 | 非商用/排除地区/营收门槛/未核实 → 拒绝路由 |
② 交付 | **本模块代码**（`delivery.audit`） | ✅ 能 | AI 标识、署名、素材登记是否写进交付物 |
③ 内容 | **VLM 初审 + 人工复审** | ❌ **初审不能** | 初审只产出"待人工复核清单"，终审签字是人工/法务 |

## 最重要的一条

**`status` 永远不可能是 `signed`。** 只有三种值：

- `blocked` —— 硬规则没过（授权拒绝 / 缺署名或缺标识），交付被拦
- `needs_human_review` —— 内容初审完成（或未做），**等人工/法务签字**
- （`signed` 不由本模块产生 —— 人工签署后由外部流程写入）

> 这是"**不可自签收**"在合规环节的落点：模型可以提示风险，但不能宣布合规通过。

## 数据结构

见 `audit()` 返回的 `compliance.report`：
`licenseAudit` / `deliveryAudit` / `contentFirstPass` / `humanReview` / `status` / `blocking`。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

#: 内容初审最多看几张（成本可控；其余抽检由人工复审补）
DEFAULT_MAX_IMAGES = 4

#: 内容风险类别（封闭集：便于统计与人工复核）
CONTENT_RISK_KINDS: tuple[str, ...] = (
    "minor",          # 未成年人不当内容
    "violence",       # 暴力/血腥过度
    "real_person",    # 真实人物/名人
    "trademark",      # 真实商标/品牌
    "discrimination", # 地域/民族/性别歧视
    "sexual",         # 色情/擦边
    "other",
)

_CONTENT_PROMPT = """你是内容合规初审员。请只判断**画面内容**是否存在法规/平台风险。
逐项检查：未成年人不当内容、暴力血腥过度、真实人物/名人、真实商标/品牌、
歧视性符号或表述、色情擦边。
镜头描述：{desc}
严格输出 JSON（不要解释）：
{{"risk": "none|low|high",
 "items": [{{"kind": "minor|violence|real_person|trademark|discrimination|sexual|other",
             "severity": "low|high", "reason": "一句话"}}]}}
若无法判断或图像不可读，输出 {{"risk": "unknown", "items": []}}。"""


def _json_of(text: str) -> Any:
    s = str(text or "").strip()
    if s.startswith("```"):
        s = s.split("```")[1] if "```" in s[3:] else s[3:]
        s = s.split("```")[0]
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(s[i:j + 1])
    except Exception:                                         # noqa: BLE001
        return None


def _delivery_audit(delivery: Mapping[str, Any] | None,
                    attributions: Sequence[str]) -> dict[str, Any]:
    """交付审计：标识 / 署名 / 素材登记。**缺什么列什么**，不猜。"""
    d = dict(delivery or {})
    missing: list[dict[str, str]] = []

    labels = d.get("aiLabels")
    # 显式标识（画面/音频提示）+ 隐式标识（元数据）是两件事
    if not isinstance(labels, Mapping) or not labels.get("explicit"):
        missing.append({"item": "aiLabel.explicit", "why": "成片缺少显式 AI 生成标识"})
    if not isinstance(labels, Mapping) or not labels.get("implicit"):
        missing.append({"item": "aiLabel.implicit", "why": "缺少隐式标识（元数据）"})

    credits = str(d.get("credits") or "")
    for text in attributions:
        if text and text not in credits:
            missing.append({"item": f"attribution:{text}",
                            "why": "授权要求的署名未写入交付物"})

    if d.get("sourceRegistryOk") is not True:
        missing.append({"item": "sourceRegistry",
                        "why": "素材来源未登记（BGM/字体/参考图）"})

    return {
        "ok": not missing,
        "labels": {"explicit": bool(isinstance(labels, Mapping)
                                    and labels.get("explicit")),
                   "implicit": bool(isinstance(labels, Mapping)
                                    and labels.get("implicit"))},
        "credits": credits[:200],
        "missing": missing,
    }


def _content_first_pass(client: Any, images: Mapping[str, str],
                        descs: Mapping[str, str],
                        max_images: int) -> dict[str, Any]:
    """VLM 内容初审。没有客户端就**如实记为未做**（不是通过）。"""
    if client is None:
        return {"performed": False, "reason": "未绑定视觉模型（初审未做）",
                "reviewed": 0, "items": [], "highRisk": 0}
    items: list[dict[str, Any]] = []
    reviewed = 0
    errors: list[str] = []
    picked = list(images.items())[:max(0, int(max_images))]
    for sid, path in picked:
        try:
            data = base64.b64encode(Path(str(path)).read_bytes()).decode()
        except Exception as e:                                # noqa: BLE001
            errors.append(f"{sid}:读图失败:{type(e).__name__}")
            continue
        desc = str((descs or {}).get(sid) or "")
        try:
            reply = client.chat_image(_CONTENT_PROMPT.replace("{desc}", desc[:300]),
                                      data, what="内容合规初审",
                                      max_tokens=400)
            text = reply if isinstance(reply, str) else getattr(reply, "text", "")
            obj = _json_of(text)
        except Exception as e:                                # noqa: BLE001
            errors.append(f"{sid}:{type(e).__name__}:{str(e)[:40]}")
            continue
        if not isinstance(obj, Mapping):
            errors.append(f"{sid}:模型没给 JSON")
            continue
        reviewed += 1
        risk = str(obj.get("risk") or "unknown")
        for it in (obj.get("items") or ()):
            if not isinstance(it, Mapping):
                continue
            kind = str(it.get("kind") or "other")
            if kind not in CONTENT_RISK_KINDS:
                kind = "other"
            items.append({"shotId": sid, "kind": kind,
                          "severity": str(it.get("severity") or "low"),
                          "reason": str(it.get("reason") or "")[:120]})
        if risk == "high" and not obj.get("items"):
            items.append({"shotId": sid, "kind": "other", "severity": "high",
                          "reason": "初审判高风险但未给出类别"})
    high = sum(1 for x in items if str(x.get("severity")) == "high")
    return {"performed": reviewed > 0, "reviewed": reviewed,
            "items": items, "highRisk": high, "errors": errors[:6]}


def audit(*, images: Mapping[str, str] | None = None,
          descriptions: Mapping[str, str] | None = None,
          models_used: Sequence[str] = (),
          delivery: Mapping[str, Any] | None = None,
          client: Any = None,
          max_images: int = DEFAULT_MAX_IMAGES,
          region: str = "CN", commercial: bool = False,
          annual_revenue_usd: int = 0) -> dict[str, Any]:
    """三腿合并 → `compliance.report`。**纯函数**（可单测，不写黑板）。"""
    from little_crew.governance.license import (
        DeploymentContext,
        LicenseGate,
        Verdict,
    )

    gate = LicenseGate()
    ctx = DeploymentContext(region=str(region or "CN"),
                            commercial=bool(commercial),
                            annual_revenue_usd=int(annual_revenue_usd))
    ids = [str(m) for m in (models_used or ()) if str(m).strip()]
    verdicts = [gate.check(m, ctx) for m in ids]
    # ⚠️ 只有 **DENY**（硬性禁止）才阻断交付；
    # **NEEDS_REVIEW**（未核实/信源冲突）不构成违法，转人工确认项 —— 
    # 否则"未核实"会被伪装成"违规"，反过来"接受风险"又会被伪装成"合规"。
    hard_denied = [v for v in verdicts
                   if getattr(v, "verdict", None) is Verdict.DENY]
    needs_review = [v for v in verdicts
                    if not v.allowed
                    and getattr(v, "verdict", None) is not Verdict.DENY]
    attributions = gate.collect_attributions(ids)

    license_audit = {
        "models": ids,
        "checked": len(verdicts),
        "denied": [{"modelId": v.model_id, "reason": v.reason,
                    "rule": v.triggered_rule}
                   for v in hard_denied],
        "needsReview": [{"modelId": v.model_id, "reason": v.reason,
                         "rule": v.triggered_rule}
                        for v in needs_review],
        "verdicts": [v.as_dict() if hasattr(v, "as_dict") else {
            "modelId": getattr(v, "model_id", ""),
            "allowed": bool(getattr(v, "allowed", False)),
            "reason": getattr(v, "reason", ""),
            "rule": getattr(v, "triggered_rule", "")} for v in verdicts],
        "attributions": list(attributions),
        "blocked": bool(hard_denied),
    }

    delivery_audit = _delivery_audit(delivery, attributions)
    content = _content_first_pass(client, dict(images or {}),
                                 dict(descriptions or {}), max_images)

    human: list[dict[str, Any]] = []
    for m in delivery_audit["missing"]:
        human.append({"type": "delivery", "owner": "交付负责人",
                      "item": m["item"], "why": m["why"]})
    for m in license_audit["needsReview"]:
        human.append({"type": "license", "owner": "人工/法务确认",
                      "modelId": m["modelId"], "why": m["reason"]})
    for it in content.get("items") or ():
        human.append({"type": "content", "owner": "人工/法务复审",
                      "shotId": it.get("shotId"), "kind": it.get("kind"),
                      "severity": it.get("severity"),
                      "why": it.get("reason")})
    if not content.get("performed"):
        human.append({"type": "content", "owner": "人工/法务复审",
                      "item": "contentFirstPass",
                      "why": content.get("reason") or "内容初审未做"})

    blocking = bool(license_audit["blocked"])
    report = {
        "kind": "compliance.report",
        "licenseAudit": license_audit,
        "deliveryAudit": delivery_audit,
        "contentFirstPass": content,
        "humanReview": human,
        "blocking": blocking,
        # ⚠️ **不产生 signed**：终审签字只能来自人工/法务流程
        "status": "blocked" if blocking else "needs_human_review",
        "signedBy": None,
        "note": ("硬规则（授权/标识/署名）由代码判定；内容合规只做 VLM 初审，"
                 "终审由人工/法务签字 —— 本报告不宣称已合规。"),
    }
    return report


class ComplianceAudit:
    """合规官实现：`impl(*, envelope, runtime) -> CallResult`（ASSEMBLY）。"""

    agent_id = "compliance_officer"
    capability = "compliance.check"
    produces_key = "compliance.report"

    def __init__(self, *, client: Any = None,
                 max_images: int = DEFAULT_MAX_IMAGES,
                 region: str = "CN", commercial: bool = False,
                 annual_revenue_usd: int = 0) -> None:
        self.client = client
        self.max_images = max_images
        self.region = region
        self.commercial = commercial
        self.annual_revenue_usd = annual_revenue_usd

    def __call__(self, *, envelope: Any, runtime: Any = None) -> Any:
        from little_crew.agent.protocol import CallResult

        p = dict(getattr(envelope, "payload", {}) or {})
        try:
            report = audit(
                images=p.get("shot.image") or {},
                descriptions=p.get("descriptions") or {},
                models_used=p.get("modelsUsed") or (),
                delivery=p.get("delivery") or {},
                client=self.client,
                max_images=self.max_images,
                region=self.region,
                commercial=self.commercial,
                annual_revenue_usd=self.annual_revenue_usd,
            )
        except Exception as e:                                # noqa: BLE001
            return CallResult.fail("F-COMPLIANCE",
                                   f"合规审计失败：{type(e).__name__}: {e}")
        return CallResult.success({self.produces_key: report})
