"""模型授权门禁测试。

核心验证：授权风险**真的被代码拦住**，而不是文档里的一句话。
零第三方依赖： python -m little_crew.governance.test_license
"""

from __future__ import annotations

import sys

from little_crew.governance.license import (
    APACHE_2_0,
    LICENSE_CATALOG,
    DeploymentContext,
    LicenseGate,
    LicenseInfo,
    Verdict,
)

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, cond, detail))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    print("=" * 78)
    print("模型授权门禁 —— 测试")
    print("=" * 78)

    # 本机实际情形：中国大陆、非商用、零营收
    local = DeploymentContext(region="CN", commercial=False, annual_revenue_usd=0)
    # 商用情形：中国、商用、年营收 50 万美元
    smb = DeploymentContext(region="CN", commercial=True, annual_revenue_usd=500_000)
    # 规模化商用：年营收 3000 万美元
    scale = DeploymentContext(region="CN", commercial=True, annual_revenue_usd=30_000_000)
    # 欧美部署
    us = DeploymentContext(region="US", commercial=True, annual_revenue_usd=500_000)
    eu = DeploymentContext(region="EU", commercial=True, annual_revenue_usd=500_000)

    gate = LicenseGate()

    # ---------- L1 Apache 2.0 无条件通过 ----------
    for mid in ("qwen-image-2512", "wan2.2-14b", "ernie-image", "ace-step-1.5"):
        v = gate.check(mid, scale)
        check(f"L1 {mid} 在最高约束场景下仍允许（Apache-2.0）",
              v.allowed and v.triggered_rule == "OK", f"{v.verdict} {v.reason}")

    # ---------- L2 同名模型授权相反（最高风险项） ----------
    v4b = gate.check("flux2-klein-4b", smb)
    v9b = gate.check("flux2-klein-9b", smb)
    check("L2 FLUX.2 Klein 4B 商用允许（Apache-2.0）", v4b.allowed, v4b.reason)
    check("L2 FLUX.2 Klein 9B 商用被拒绝（非商用授权）",
          v9b.verdict is Verdict.DENY
          and v9b.triggered_rule == "NONCOMMERCIAL_IN_COMMERCIAL_CTX",
          f"{v9b.verdict} {v9b.reason}")
    check("L2 同一模型在非商用场景下不拦（授权只约束商用）",
          gate.check("flux2-klein-9b", local).allowed,
          "非商用场景下 9B 可用作研究与对照")

    # ---------- L3 地区排除 ----------
    v_cn = gate.check("minimax-h3", smb)
    v_us = gate.check("minimax-h3", us)
    v_eu = gate.check("minimax-h3", eu)
    check("L3 MiniMax H3 在中国大陆允许", v_cn.allowed, v_cn.reason)
    check("L3 MiniMax H3 在美国被拒绝（地区排除）",
          v_us.verdict is Verdict.DENY and v_us.triggered_rule == "REGION_EXCLUDED",
          v_us.reason)
    check("L3 MiniMax H3 在欧盟被拒绝", v_eu.verdict is Verdict.DENY, v_eu.reason)
    check("L3 地区判定大小写不敏感",
          gate.check("minimax-h3", DeploymentContext(region="us")).verdict
          is Verdict.DENY, "")
    check("L3 HunyuanVideo 1.5 在欧盟被拒绝",
          gate.check("hunyuanvideo-1.5", eu).verdict is Verdict.DENY,
          gate.check("hunyuanvideo-1.5", eu).reason)

    # ---------- L4 营收门槛 ----------
    v_ltx_small = gate.check("ltx-video-2b", smb)
    v_ltx_big = gate.check("ltx-video-2b", scale)
    check("L4 LTX 在年营收 50 万美元下允许（门槛 1000 万）",
          v_ltx_small.allowed, v_ltx_small.reason)
    check("L4 LTX 在年营收 3000 万美元下被拒绝",
          v_ltx_big.verdict is Verdict.DENY
          and v_ltx_big.triggered_rule == "REVENUE_THRESHOLD_EXCEEDED",
          v_ltx_big.reason)
    v_sa = gate.check("stable-audio-3-medium",
                      DeploymentContext(region="CN", commercial=True,
                                        annual_revenue_usd=2_000_000))
    check("L4 Stable Audio 3 门槛更低（100 万美元）被正确拦截",
          v_sa.verdict is Verdict.DENY, v_sa.reason)

    # ---------- L5 未核实 / 信源冲突一律拒绝 ----------
    v_music = gate.check("minimax-music-3", local)
    check("L5 信源冲突的模型被拒绝自动启用",
          v_music.verdict is Verdict.NEEDS_REVIEW
          and v_music.triggered_rule == "CONFLICTING_SOURCES",
          v_music.reason)
    v_lens = gate.check("lens", local)
    check("L5 未核实的模型被拒绝自动启用",
          v_lens.verdict is Verdict.NEEDS_REVIEW
          and v_lens.triggered_rule == "UNVERIFIED",
          v_lens.reason)
    v_unknown = gate.check("some-brand-new-model", local)
    check("L5 目录中无记录的模型被拒绝（未知不等于允许）",
          v_unknown.verdict is Verdict.NEEDS_REVIEW
          and v_unknown.triggered_rule == "NO_RECORD",
          v_unknown.reason)
    check("L5 NEEDS_REVIEW 与 DENY 语义区分开",
          Verdict.NEEDS_REVIEW is not Verdict.DENY
          and not Verdict.NEEDS_REVIEW.allowed,
          "两者都不放行，但理由不同：一个要人看，一个是确定不行")

    # ---------- A) 未核实即拒绝（核心强制行为） ----------
    gate2 = LicenseGate()
    gate2.register("my-custom-model", LicenseInfo(
        APACHE_2_0, commercial_use=True, verified=False,
        note="自建模型，授权待确认"))
    check("A 未核实标记覆盖了 Apache-2.0 的宽松性（verified=False 优先）",
          gate2.check("my-custom-model", local).verdict is Verdict.NEEDS_REVIEW,
          "即便写 Apache-2.0，未核实也不放行")
    gate2.register("my-custom-model", LicenseInfo(
        APACHE_2_0, commercial_use=True, verified=True, note="已核实"))
    check("A 核实后立即放行（登记即生效，无需改代码）",
          gate2.check("my-custom-model", local).allowed, "")

    # ---------- B) 降级链每一跳都要过授权 ----------
    # 场景：H3 在欧美被排除 -> 降级目标里混入了一个禁商用模型
    chain = ["minimax-h3", "wan2.2-14b", "flux2-klein-9b", "yue2-3b", "ltx-video-2b"]
    allowed_us, verdicts = gate.check_degradation_chain(chain, us)
    check("B 降级链被整体过滤（欧美场景）",
          allowed_us == ["wan2.2-14b", "ltx-video-2b"],
          f"allowed={allowed_us}")
    denied = {v.model_id: v.triggered_rule for v in verdicts if not v.allowed}
    check("B 降级链中被剔除的模型理由正确",
          denied.get("minimax-h3") == "REGION_EXCLUDED"
          and denied.get("flux2-klein-9b") == "NONCOMMERCIAL_IN_COMMERCIAL_CTX"
          and denied.get("yue2-3b") == "NONCOMMERCIAL_IN_COMMERCIAL_CTX",
          str(denied))
    check("B 相同降级链在中国场景下保留 H3",
          "minimax-h3" in gate.check_degradation_chain(chain, smb)[0],
          str(gate.check_degradation_chain(chain, smb)[0]))

    # ---------- C) 归属标注自动注入 ----------
    attrs = gate.collect_attributions(["minimax-h3", "qwen-image-2512", "wan2.2-14b"])
    check("C 只收集要求标注的模型，去重",
          attrs == ["本作品由 MiniMax H3 生成"], str(attrs))
    check("C 无标注要求的模型不产生空条目",
          all(a.strip() for a in attrs) and len(attrs) == 1, str(attrs))
    v = gate.check("minimax-h3", smb)
    check("C 判定结果直接携带归属文本，供上层注入",
          v.allowed and v.attribution_text == "本作品由 MiniMax H3 生成",
          v.attribution_text)

    # ---------- D) AUP 条款 ----------
    no_aup = DeploymentContext(region="CN", commercial=True, accepts_aup=False)
    check("D 未接受 AUP 的部署方无法使用带 AUP 的模型",
          gate.check("sdxl-base-1.0", no_aup).triggered_rule == "AUP_NOT_ACCEPTED",
          gate.check("sdxl-base-1.0", no_aup).reason)
    check("D 接受 AUP 时正常放行",
          gate.check("sdxl-base-1.0", smb).allowed, "")

    # ---------- 路由集成 ----------
    candidates = ["minimax-h3", "wan2.2-14b", "flux2-klein-9b", "lens",
                  "ltx-video-2b", "qwen-image-2512"]
    allowed, _ = gate.filter_allowed(candidates, smb)
    check("路由集成：被拒模型根本不进入打分环节",
          allowed == ["minimax-h3", "wan2.2-14b", "ltx-video-2b", "qwen-image-2512"],
          str(allowed))

    # ---------- 审计报告 ----------
    report = gate.audit()

    # ⚠️ 这里**不写死计数**。早先版本写的是 `NEEDS_REVIEW == 2`，
    # 结果一往目录里加模型（哪怕是正确的补充登记）测试就变红 ——
    # 那是脆弱断言，会训练人"改测试让它变绿"，而不是去查真问题。
    # 正确做法：期望值从目录**推导**出来。
    expect_review = {
        mid for mid, info in LICENSE_CATALOG.items()
        if info.conflicting_sources or not info.verified
    }
    check("审计报告含判定计数与归属清单",
          set(report["counts"]) == {"ALLOW", "DENY", "NEEDS_REVIEW"}
          and report["counts"]["NEEDS_REVIEW"] == len(expect_review)
          and report["attributionsRequired"] == ["本作品由 MiniMax H3 生成"],
          f"counts={report['counts']} 期望 NEEDS_REVIEW={len(expect_review)} "
          f"attrs={report['attributionsRequired']}")
    check("审计报告覆盖全部登记模型",
          len(report["models"]) == len(LICENSE_CATALOG),
          f"{len(report['models'])} vs {len(LICENSE_CATALOG)}")

    # ---------- 判定结果可展示 ----------
    check("判定理由非空且可读（能直接展示给用户）",
          all(gate.check(m, local).reason for m in LICENSE_CATALOG),
          "")

    # ---------- 汇总 ----------
    print()
    npass = sum(1 for _, ok, _ in _results if ok)
    for name, ok, detail in _results:
        print(f" [{'v' if ok else 'X'}] {name}")
        if not ok or detail:
            print(f"       {detail}")
    print("=" * 78)
    print(f"结果：{npass}/{len(_results)} 通过")

    if npass == len(_results):
        print()
        print("--- 授权审计报告（本机情形：中国大陆 / 非商用）---")
        rep = LicenseGate(context=local).audit()
        print(f"   ALLOW={rep['counts']['ALLOW']}  "
              f"DENY={rep['counts']['DENY']}  "
              f"NEEDS_REVIEW={rep['counts']['NEEDS_REVIEW']}")
        print("   注：标记 [NC] 者为非商用授权 —— 在当前非商用情形下判定放行，")
        print("       但转为商用即被拒。单独标记以免被误读为「可商用」。")
        print()
        for row in rep["models"]:
            lic = row["license"] or {}
            mark = {"ALLOW": "OK ", "DENY": "NO ", "NEEDS_REVIEW": "?? "}[row["verdict"]]
            tag = "  [NC]" if not lic.get("commercialUse", True) else ""
            print(f"   [{mark}] {row['modelId']:26} {lic.get('licenseId','?'):22} "
                  f"{row['reason'][:40]}{tag}")
    return 0 if npass == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
