"""人工 Gate 自检（三态语义 + 不许把跳过当通过）。"""
from __future__ import annotations

import sys

from little_crew.runtime import human_gate as HG

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("人工 Gate —— 自检")
    print("=" * 66)

    print(); print("A. 三态语义")
    a = HG.as_record(HG.decide(auto="approve"), shots=10, seconds=60.0)
    check("A1 ★★★ approve -> reviewed & approved", a["reviewed"] and a["approved"], str(a))
    check("A2 ★★★ approve 不算 assisted（人没改东西）", a["assisted"] is False, str(a))
    r = HG.as_record(HG.decide(auto="reject"), shots=10, seconds=60.0)
    check("A3 ★★★ reject -> 不通过且 assisted", r["approved"] is False and r["assisted"], str(r))
    s = HG.as_record(HG.decide(auto="skip"), shots=10, seconds=60.0)
    check("A4 ★★★ skip -> reviewed=False（**不等于通过**）",
          s["reviewed"] is False and s["approved"] is False, str(s))
    check("A5 ★★ skip 也不标 assisted（人没动）", s["assisted"] is False, str(s))

    print(); print("B. 非法输入")
    bad = HG.decide(auto="随便")
    check("B1 ★★★ 非法预设 -> 不崩、记原因",
          bad["decision"] == "skip" and "非法" in bad["reason"], bad["reason"])
    check("B2 ★★ 非法预设标记来源", bad["by"] == "auto-invalid", bad["by"])

    print(); print("C. 缺席可辨")
    rec = HG.as_record({"decision": "skip", "by": "none",
                        "reason": "非交互环境", "interactive": False})
    check("C1 ★★★ 记录里写明「没问人」的理由", "非交互" in rec["reason"], rec["reason"])
    check("C2 ★★ 记录列出「没判什么」",
          any("能不能给人看" in x for x in rec["notJudged"]), str(rec["notJudged"])[:40])
    check("C3 ★★ shots/seconds 被带进记录",
          rec["shots"] == 0 and rec["seconds"] == 0.0, "")

    print(); print("C2. 改判规则（纯函数）")
    ok1, why1 = HG.apply(True, "", {"decision": "approve"})
    check("C2a ★★★ approve 不会把失败变成功（只对失败生效）",
          HG.apply(False, "渲染失败", {"decision": "approve"})[0] is False, "")
    check("C2b ★★★ approve 保持原判", ok1 is True and why1 == "", "")
    ok2, why2 = HG.apply(True, "", {"decision": "reject", "reason": "节奏太闷"})
    check("C2c ★★★ reject 把通过改成不通过，并写原因",
          ok2 is False and "节奏太闷" in why2, why2)
    ok3, why3 = HG.apply(True, "原原因", {"decision": "skip"})
    check("C2d ★★ skip 不改任何东西", ok3 is True and why3 == "原原因", why3)

    print(); print("D. 摘要")
    txt = HG.summarize(12, 88.5, pending=1, vision_errors=2, gate_ok=False)
    check("D1 ★★ 摘要含镜头/时长", "12" in txt and "88.5" in txt, txt[:60])
    check("D2 ★★ 摘要写明「机器判不了什么」", "机器判不了" in txt, "")
    check("D3 ★ 摘要含门禁未过", "未过" in txt, "")

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
