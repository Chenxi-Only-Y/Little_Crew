"""合规官自检（2026-11-15）：授权归口 + 交付审计 + 内容 VLM 初审/人工复审。

锁住六件事：
1 **归口**：`LicenseGate` 是合规官名下的工具（`license.audit`），不是独立判定者
2 **硬规则才阻断**：只有 `DENY`（非商用/地区/门槛）令 `blocking=True`；
   `NEEDS_REVIEW`（未核实/无记录）转人工项，**不伪装成违规**
3 **交付审计**：AI 标识/署名/素材登记缺什么列什么
4 **内容初审**：没有视觉客户端就**如实标"未做"**（不是通过）
5 **不可自签收**：报告 `status` 永远不是 `signed`
6 **接线**：默认桩注册、编排器调用后 `compliance.report` 进黑板
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


class _Client:
    def __init__(self, text):
        self.text = text

    def chat_image(self, prompt, data, what="", max_tokens=0):
        return self.text


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 68)
    print("合规官（compliance_officer）—— 自检")
    print("=" * 68)

    from little_crew.agent.crew_stubs import CORE_CREW_STUBS
    from little_crew.agent.roster import DEFAULT_ROSTER
    from little_crew.agent.compliance_audit import ComplianceAudit, audit

    print(); print("A. 归口（名册）")
    spec = {a.agent_id: a for a in DEFAULT_ROSTER}["compliance_officer"]
    check("A1 ★★★ 合规官存在且为 VLM（内容初审要看图）",
          spec.execution.value == "vlm", spec.execution.value)
    check("A2 ★★★ `license.audit` 是其名下工具（LicenseGate 归口）",
          "license.audit" in tuple(spec.tools), str(spec.tools))
    check("A3 ★★★ 产物是 compliance.report",
          "compliance.report" in tuple(spec.produces), "")
    check("A4 ★★ 默认桩注册了合规官（每次交付必跑）",
          "compliance_officer" in CORE_CREW_STUBS, "")

    print(); print("B. 授权腿（LicenseGate）")
    r_deny = audit(models_used=["flux1-dev"], commercial=True)
    check("B1 ★★★ 非商用模型 + 商用场景 -> DENY/blocking",
          r_deny["blocking"] and r_deny["licenseAudit"]["denied"], "")
    check("B2 ★★★ 未上目录的模型 -> NEEDS_REVIEW（不阻断，转人工）",
          (lambda r: (not r["blocking"])
           and any(x["modelId"] == "ghost-model-xyz"
                   for x in r["licenseAudit"]["needsReview"]))(
              audit(models_used=["ghost-model-xyz"], commercial=True)),
          "")
    r_attr = audit(models_used=["minimax-h3"], commercial=True)
    check("B3 ★★★ 要求署名的模型 -> 署名文本被收集",
          "本作品由 MiniMax H3 生成" in r_attr["licenseAudit"]["attributions"],
          str(r_attr["licenseAudit"]["attributions"]))

    print(); print("C. 交付腿（标识/署名/素材）")
    r_empty = audit(models_used=[], delivery={})
    miss = {m["item"] for m in r_empty["deliveryAudit"]["missing"]}
    check("C1 ★★★ 空交付 -> 显式/隐式标识与素材登记都列为缺失",
          {"aiLabel.explicit", "aiLabel.implicit",
           "sourceRegistry"} <= miss, str(sorted(miss)))
    check("C2 ★★★ 缺失项全部进 humanReview（不是静默）",
          any(x.get("item") == "aiLabel.explicit"
              for x in r_empty["humanReview"]), "")
    r_ok = audit(models_used=["minimax-h3"], commercial=True, delivery={
        "aiLabels": {"explicit": True, "implicit": True},
        "credits": "本作品由 MiniMax H3 生成",
        "sourceRegistryOk": True,
    })
    check("C3 ★★★ 标识/署名/素材齐 -> 交付腿 ok",
          r_ok["deliveryAudit"]["ok"], str(r_ok["deliveryAudit"]["missing"]))
    check("C4 ★★★ 署名没写进交付物 -> 列为缺失",
          any("attribution" in m["item"]
              for m in audit(models_used=["minimax-h3"], commercial=True,
                             delivery={"aiLabels": {"explicit": True,
                                                    "implicit": True},
                                       "sourceRegistryOk": True}
                             )["deliveryAudit"]["missing"]), "")

    print(); print("D. 内容腿（VLM 初审 + 人工复审）")
    r_novlm = audit(models_used=[], delivery={})
    check("D1 ★★★ 没有视觉客户端 -> 初审如实标「未做」（不是通过）",
          r_novlm["contentFirstPass"]["performed"] is False
          and any(x.get("item") == "contentFirstPass"
                  for x in r_novlm["humanReview"]), "")
    fake_high = _Client('{"risk":"high","items":[{"kind":"trademark",'
                        '"severity":"high","reason":"出现真实品牌"}]}')
    r_high = audit(images={"SH1": __file__}, models_used=[],
                   delivery={}, client=fake_high)
    check("D2 ★★★ VLM 判高风险 -> 进 humanReview（人工复审）",
          r_high["contentFirstPass"]["highRisk"] == 1
          and any(x.get("type") == "content"
                  and x.get("kind") == "trademark"
                  for x in r_high["humanReview"]), "")
    fake_clean = _Client('{"risk":"none","items":[]}')
    r_clean = audit(images={"SH1": __file__}, models_used=[],
                    delivery={}, client=fake_clean)
    check("D3 ★★★ VLM 判无风险 -> 仍然 needs_human_review（初审≠终审）",
          r_clean["status"] == "needs_human_review", r_clean["status"])
    fake_bad = _Client("这不是 JSON")
    r_bad = audit(images={"SH1": __file__}, models_used=[], delivery={},
                  client=fake_bad)
    check("D4 ★★ 初审解析失败 -> 记为未完成（不假装看过）",
          r_bad["contentFirstPass"]["performed"] is False
          and r_bad["contentFirstPass"]["errors"], "")

    print(); print("E. 不可自签收")
    check("E1 ★★★ 任何情况下 status 都不是 signed",
          all(r["status"] != "signed"
              for r in (r_empty, r_deny, r_ok, r_high, r_clean, r_bad)), "")
    check("E2 ★★★ 人工签字字段为空（signedBy=None）",
          r_ok.get("signedBy") is None, "")
    check("E3 ★★ 报告带人类可读说明（不宣称已合规）",
          "不宣称已合规" in str(r_ok.get("note")), "")

    print(); print("F. 接线（编排器）")
    from little_crew.agent.orchestrator import CrewOrchestrator

    o = CrewOrchestrator(model_families={})
    o.bind("compliance_officer", ComplianceAudit())
    out = o.run_stage("compliance_officer", "compliance.check",
                      {"shot.image": {}, "descriptions": {},
                       "modelsUsed": [], "delivery": {}})
    rep = o.blackboard.value("compliance.report")
    check("F1 ★★★ 职位调用成功且 compliance.report 进黑板",
          bool(out.ok) and isinstance(rep, dict), str(getattr(out, "reason", ""))[:60])
    check("F2 ★★ 调用进了调用树",
          any(str(s.get("callee")) == "compliance_officer"
              for s in o.runtime.spans()), "")

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
