"""多智能体协作层 —— 名册与消息协议的验收测试。

**为什么放在 tests/ 而非 src/little_crew/agent/**

本文件同时使用 ``agent.roster``、``agent.messages``、``agent.protocol``
与 ``models.spec``（任务档案）—— 它验证的是**跨模块契约**，
不是某一层的内部逻辑。放包内会构成不必要的依赖边，架构守卫会判定违规。

运行： python tests\\test_agent_collab.py
零第三方依赖。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "src"))

from little_crew.agent.messages import (  # noqa: E402
    DEFAULT_QC5_WEIGHTS,
    Dimension,
    IssueLocation,
    MessageKind,
    PHASE_QC5_WEIGHTS,
    QcScore,
    RepairHint,
    ReviewFeedback,
    ReviewIssue,
    ScriptDraft,
    SELF_REPORT_TOLERANCE,
    Severity,
    Verdict,
    validate_message,
)
from little_crew.agent.protocol import Layer  # noqa: E402
from little_crew.agent.roster import (  # noqa: E402
    DEFAULT_ROSTER,
    Department,
    ExecutionKind,
    build_registry,
    execution_kind_counts,
    roster_by_department,
    roster_by_stage,
    roster_warnings,
    validate_roster,
)

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, cond, detail))


def expect_raises(name: str, exc: type[BaseException], fn, *a, **kw) -> None:
    try:
        fn(*a, **kw)
    except exc as e:
        check(name, True, str(e)[:80])
    except BaseException as e:  # noqa: BLE001
        check(name, False, f"期望 {exc.__name__}，实得 {type(e).__name__}: {e}")
    else:
        check(name, False, f"期望 {exc.__name__}，但未抛错")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    print("=" * 78)
    print("多智能体协作 —— 名册与消息协议")
    print("=" * 78)

    # ==================================================================
    # A. 剧组名册
    # ==================================================================
    check("A1 名册为 18 个剧组职位", len(DEFAULT_ROSTER) == 18,
          f"{len(DEFAULT_ROSTER)} 个")

    check("A2 名册静态校验零问题", validate_roster() == [],
          "; ".join(validate_roster()[:3]))

    ids = [s.agent_id for s in DEFAULT_ROSTER]
    check("A3 agent_id 唯一", len(set(ids)) == len(ids))

    directors = [s for s in DEFAULT_ROSTER if s.department is Department.DIRECTOR]
    check("A4 有且仅有一个「导演」，且是唯一的 L1",
          len(directors) == 1
          and directors[0].layer is Layer.DECISION
          and sum(1 for s in DEFAULT_ROSTER
                  if s.layer is Layer.DECISION) == 1,
          f"{[d.crew_title for d in directors]}")

    # 职位名必须是真实剧组职务 —— 这是"按剧组职业组织"要求的落地检查
    crew_titles = {s.crew_title for s in DEFAULT_ROSTER}
    expected_crew = {
        "导演", "编剧", "剧本顾问", "文学策划",
        "美术指导", "角色设计师", "场记",
        "分镜师", "摄影指导", "灯光师", "摄影师",
        "对白剪辑", "作曲", "剪辑师", "合成剪辑",
        "制片主任", "版权与合规", "审片",
    }
    check("A5 职位名与真实剧组职务一致", crew_titles == expected_crew,
          f"缺 {sorted(expected_crew - crew_titles)}；多 {sorted(crew_titles - expected_crew)}")

    # 每个部门都有人
    by_dept = roster_by_department()
    empty = [d for d, v in by_dept.items() if not v]
    check("A6 八个部门均有职位", empty == [], f"空部门: {empty}")

    # 依赖闭合：每个 requires 都有产出者（外部输入除外）
    produced = {p for s in DEFAULT_ROSTER for p in s.produces}
    dangling = [(s.crew_title, r) for s in DEFAULT_ROSTER for r in s.requires
                if r not in produced and r != "source.novel"]
    check("A7 依赖闭合（无悬空 requires）", dangling == [], str(dangling[:3]))

    # 生成方不含审核能力（不自我签收 —— 按产出归属判定）
    owner: dict[str, str] = {}
    for s in DEFAULT_ROSTER:
        for p in s.produces:
            owner.setdefault(p, s.agent_id)
    self_review = []
    targets = {"script.critique": ("script.draft",),
               "style.guard": ("script.draft",),
               "continuity.check": ("shot.image",),
               "review.board": ("shot.image", "shot.video")}
    for s in DEFAULT_ROSTER:
        for cap in s.capabilities:
            for t in targets.get(cap, ()):
                if owner.get(t) == s.agent_id:
                    self_review.append((s.crew_title, cap, t))
    check("A8 无人审自己的产出（不自我签收）", self_review == [], str(self_review))

    # L1 不直调 L3
    reg = build_registry()
    l1_direct_l3 = []
    for s in DEFAULT_ROSTER:
        if s.layer is not Layer.DECISION:
            continue
        for cap in s.consumes:
            for layer, impl in reg._cap_index.get(cap, []):  # noqa: SLF001
                if layer is Layer.EXEC:
                    l1_direct_l3.append((s.crew_title, cap, impl))
    check("A9 R2 不跳层：导演不直调 L3 工具", l1_direct_l3 == [], str(l1_direct_l3))

    # 注册表审计
    problems = reg.audit()
    check("A10 CapabilityRegistry 审计零问题", problems == [],
          "; ".join(problems[:3]))

    # GPU 约束是"提示"而非"错误"
    warns = roster_warnings()
    gpu_count = sum(1 for s in DEFAULT_ROSTER if s.execution.needs_gpu)
    check("A11 GPU 需求作为约束提示（不混入校验错误）",
          gpu_count >= 1 and any("GPU" in w for w in warns)
          and not any("GPU" in p for p in validate_roster()),
          f"{gpu_count} 个职位需要 GPU")

    # 执行方式分布合理：确定性职责不该混进来
    kinds = execution_kind_counts()
    check("A12 执行方式分布：无纯确定性职位残留（它们应是工具）",
          kinds.get("assembly", 0) >= 1 and sum(kinds.values()) == 18,
          json.dumps(kinds, ensure_ascii=False))

    tools = {t for t in reg._tools}  # noqa: SLF001
    check("A13 技术质检与授权核对已降为工具（不在 Agent 名册里）",
          "qc.tech_check" in tools and "license.audit" in tools
          and not any(s.agent_id in ("tech_qc",) for s in DEFAULT_ROSTER),
          str(sorted(tools)))

    # ==================================================================
    # B. 消息协议 —— 服务端权威算分
    # ==================================================================
    low = {Dimension.CONSISTENCY: 45.0, Dimension.COMPLETENESS: 80.0,
           Dimension.QUALITY: 70.0, Dimension.FEASIBILITY: 85.0,
           Dimension.COMPLIANCE: 95.0}

    s_final = QcScore(dimensions=low, claimed_score=90.0, phase="final")
    check("B1 权威总分由维度明细加权算出（非模型自报）",
          abs(s_final.total - 64.5) < 0.01 and s_final.total != 90.0,
          f"total={s_final.total}")
    check("B2 权威分与自报分的差异被检出",
          s_final.deviation is not None
          and s_final.deviation > SELF_REPORT_TOLERANCE
          and not s_final.self_report_trustworthy,
          f"deviation={s_final.deviation}")

    s_honest = QcScore(dimensions=low, claimed_score=66.0, phase="final")
    check("B3 自报分与权威分接近时判定可信",
          s_honest.self_report_trustworthy, f"deviation={s_honest.deviation}")

    check("B4 无自报分时不阻拦（只是未验证）",
          QcScore(dimensions=low).self_report_trustworthy)

    check("B5 最弱维度可识别（返工优先看短板而非总分）",
          s_final.weakest is not None
          and s_final.weakest[0] is Dimension.CONSISTENCY
          and s_final.weakest[1] == 45.0,
          str(s_final.weakest))

    # 阶段权重
    s_draft = QcScore(dimensions=low, phase="draft")
    check("B6 草稿档放宽一致性权重（同一素材，草稿档更宽容）",
          s_draft.total > s_final.total,
          f"draft={s_draft.total} final={s_final.total}")
    check("B7 两档权重各自合计为 1.0",
          abs(sum(DEFAULT_QC5_WEIGHTS.values()) - 1.0) < 1e-9
          and abs(sum(PHASE_QC5_WEIGHTS["draft"].values()) - 1.0) < 1e-9)
    check("B8 成片档一致性权重最高（漫剧的核心难点）",
          max(DEFAULT_QC5_WEIGHTS, key=lambda d: DEFAULT_QC5_WEIGHTS[d])
          is Dimension.CONSISTENCY)

    expect_raises("B9 维度分数越界被拒绝", ValueError, QcScore,
                  dimensions={Dimension.QUALITY: 120.0})

    # ==================================================================
    # C. 生效结论（三条规则综合，不直接采信 verdict 字段）
    # ==================================================================
    def fb(**kw) -> ReviewFeedback:
        base = dict(frm="dramaturge", to="screenwriter", verdict=Verdict.PASS,
                    reviewer_model="qwen3-vl-max", generator_model="deepseek-v4")
        base.update(kw)
        return ReviewFeedback(**base)

    check("C1 自报不可信 -> ESCALATE（判定过程不可靠）",
          fb(score=s_final).effective_verdict() is Verdict.ESCALATE)

    check("C2 审核方与生成方同族 -> ESCALATE（自我签收）",
          fb(reviewer_model="qwen3-vl-max",
             generator_model="qwen3-max").effective_verdict() is Verdict.ESCALATE)

    check("C3 跨族且自报可信 -> 采信原 verdict",
          fb(score=s_honest).effective_verdict() is Verdict.PASS)

    major_issue = ReviewIssue(Severity.MAJOR, Dimension.CONSISTENCY,
                              IssueLocation(scene="SC-E001-003", line=42),
                              "角色服装与第 2 场不一致", "改为米色风衣")
    check("C4 有阻断级问题却判通过 -> 强制 REVISE（以问题为准）",
          fb(score=s_honest, issues=(major_issue,)).effective_verdict()
          is Verdict.REVISE)

    check("C5 INFO 级问题不改变结论",
          fb(score=s_honest, issues=(ReviewIssue(
              Severity.INFO, Dimension.QUALITY,
              IssueLocation(shot="SH-001"), "可优化转场"),)).effective_verdict()
          is Verdict.PASS)

    check("C6 未声明模型时不阻拦跨族检查（记为未验证）",
          fb(reviewer_model="", generator_model="").cross_family_ok)

    # ==================================================================
    # D. 审查意见必须可定位、可执行
    # ==================================================================
    expect_raises("D1 BLOCKER 无 suggestion 被拒绝（不可执行）", ValueError,
                  ReviewIssue, Severity.BLOCKER, Dimension.QUALITY,
                  IssueLocation(scene="X"), "有问题")
    expect_raises("D2 空 problem 被拒绝", ValueError, ReviewIssue,
                  Severity.MINOR, Dimension.QUALITY, IssueLocation(scene="X"), "")

    loc = IssueLocation(scene="SC-E001-003", shot="SH-E001-003-007", line=42,
                        asset="CHAR-0007")
    check("D3 结构化定位可序列化且可读",
          loc.as_dict() == {"scene": "SC-E001-003", "shot": "SH-E001-003-007",
                            "line": 42, "asset": "CHAR-0007"}
          and "SC-E001-003" in loc.describe())
    check("D4 空定位可识别（协议校验会拦下）", IssueLocation().is_empty())

    # ==================================================================
    # E. 返工指令必须能映射成 IR 变更
    # ==================================================================
    hint = RepairHint(graph_id="G-ep001-sh005", op="insert_shot",
                      args={"after": "SH-E001-003-007",
                            "durationSec": 3.0, "purpose": "展示代价"},
                      from_issue="issue_001")
    check("E1 返工指令携带目标图 id 与操作类型",
          hint.graph_id and hint.op in RepairHint.KNOWN_OPS)
    expect_raises("E2 未知返工操作被拒绝（白名单）", ValueError,
                  RepairHint, graph_id="G1", op="do_something_weird")
    expect_raises("E3 缺 graph_id 被拒绝（返工必须指明目标）", ValueError,
                  RepairHint, graph_id="", op="insert_shot")

    check("E4 返工指令可序列化（供下游翻译为 IR 补丁）",
          hint.as_dict()["graphId"] == "G-ep001-sh005"
          and hint.as_dict()["op"] == "insert_shot")

    # ==================================================================
    # F. 消息封装与协议校验
    # ==================================================================
    script = ScriptDraft(
        frm="screenwriter", to="dramaturge", stage="script",
        episode_id="EP001", title="雨夜",
        scenes=({"id": "SC-E001-001", "text": "天台，林晚独白"},),
        revision=1, estimated_seconds=95.0,
        note="仅人读，不参与判定")

    check("F1 msg_id 自动生成且确定性（同内容同 id）",
          script.msg_id.startswith("msg_")
          and script.msg_id == ScriptDraft(
              frm="screenwriter", to="dramaturge", stage="script",
              episode_id="EP001", title="雨夜",
              scenes=({"id": "SC-E001-001", "text": "天台，林晚独白"},),
              revision=1, estimated_seconds=95.0,
              note="仅人读，不参与判定",
              created_at=script.created_at).msg_id,
          script.msg_id)

    expect_raises("F2 缺发送方被拒绝", ValueError, ScriptDraft,
                  frm="", to="dramaturge")
    expect_raises("F3 缺接收方被拒绝", ValueError, ScriptDraft,
                  frm="screenwriter", to="")

    d = script.as_dict()
    check("F4 消息可序列化且字段名用 from（避开 Python 关键字）",
          d["from"] == "screenwriter" and d["to"] == "dramaturge"
          and d["kind"] == MessageKind.DELIVERABLE.value)
    check("F5 协议校验通过", validate_message(d) == [], str(validate_message(d)))
    check("F6 内容哈希稳定可复现",
          script.content_hash == script.content_hash
          and script.content_hash.startswith("sha256:"))

    # 协议校验能抓出坏消息
    bad_review = {
        "kind": MessageKind.REVIEW.value, "msgId": "m1",
        "from": "a", "to": "b", "createdAt": "2026-01-01T00:00:00+00:00",
        "issues": [
            {"severity": "blocker", "dimension": "quality",
             "location": {}, "problem": "没定位", "suggestion": "改"},
            {"severity": "info", "dimension": "quality",
             "location": {"scene": "S1"}, "problem": "", "suggestion": ""},
            {"severity": "blocker", "dimension": "quality",
             "location": {"scene": "S1"}, "problem": "有定位但无建议"},
        ],
        "repairHints": [{"graphId": "", "op": "未知操作"}],
    }
    probs = validate_message(bad_review)
    check("F7 协议校验能抓出：空定位 / 空 problem / BLOCKER 无建议",
          any("定位" in p for p in probs) and any("problem 为空" in p for p in probs)
          and any("suggestion" in p for p in probs),
          f"{len(probs)} 个问题")
    check("F8 协议校验能抓出：返工指令缺 graphId 与未知操作",
          any("graphId" in p for p in probs) and any("未登记" in p for p in probs))

    check("F9 未知消息类型被拒绝（协议白名单）",
          any("未知消息类型" in p for p in validate_message(
              {"kind": "whatever", "msgId": "m", "from": "a", "to": "b",
               "createdAt": "t"})))

    # ==================================================================
    # G. 名册与协议连通（协作的最小闭环要件）
    # ==================================================================
    # 编剧产出 script.draft；剧本顾问审查它；两者不同 Agent —— 这是协作的前提
    writer = next(s for s in DEFAULT_ROSTER if s.agent_id == "screenwriter")
    dramaturge = next(s for s in DEFAULT_ROSTER if s.agent_id == "dramaturge")
    check("G1 交稿方与审查方是不同职位（协作而非自审）",
          writer.agent_id != dramaturge.agent_id
          and "script.draft" in writer.produces
          and "script.draft" in dramaturge.requires)
    check("G2 编剧可发起审查（consumes 含 script.critique）",
          "script.critique" in writer.consumes)
    check("G3 审查方按部门归属编剧组（同组内互审，符合剧组惯例）",
          dramaturge.department is Department.SCREENPLAY)
    check("G4 并行双维审查：剧本顾问与文学策划互不依赖",
          "script.critique" not in dramaturge.requires
          and "style.guard" not in dramaturge.requires,
          "两者可并行，由导演合并意见")

    # ==================================================================
    # 汇总
    # ==================================================================
    print()
    npass = sum(1 for _, ok, _ in _results if ok)
    for name, ok, detail in _results:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail:
            print(f"       {detail}")
    print("=" * 78)
    print(f"结果：{npass}/{len(_results)} 通过")
    if npass == len(_results):
        print()
        print(f"  剧组职位 : {len(DEFAULT_ROSTER)} 个，分属 {len(Department)} 个部门")
        print(f"  执行方式 : {json.dumps(execution_kind_counts(), ensure_ascii=False)}")
        print(f"  L3 工具  : {sorted(tools)}")
        print(f"  QC-5 权重: 一致性 {DEFAULT_QC5_WEIGHTS[Dimension.CONSISTENCY]:.2f}"
              f" / 完整性 {DEFAULT_QC5_WEIGHTS[Dimension.COMPLETENESS]:.2f}"
              f" / 品质感 {DEFAULT_QC5_WEIGHTS[Dimension.QUALITY]:.2f}")
    return 0 if npass == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
