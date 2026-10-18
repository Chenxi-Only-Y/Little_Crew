"""**通用批评家**：让 A 类职位（审查/评审）用一个实现覆盖。

## 为什么需要"通用"而不是每个职位写一遍

名册里 A 类有 5 个：`continuity_supervisor` / `compliance_officer` /
`pacing_editor` / `review_board` / `story_editor`。

它们的**输出契约完全相同** —— 都是 `ReviewFeedback`（意见 + 严重度 + 返工指令）。
差异只在"**盯什么**"：

| 职位 | 盯什么 |
|---|---|
| `story_editor` | 文风、用词、句式是否统一（`style.guard`） |
| `continuity_supervisor` | 跨场人物/物件/时间是否连贯（`continuity.report`） |
| `compliance_officer` | 授权、敏感内容、标注（`compliance.report`） |
| `pacing_editor` | 节奏、信息密度（`pacing.report`） |
| `review_board` | 成片整体是否达标（`review.verdict`） |

所以：**一个类 + 一份"盯什么"的清单**，比复制五遍可靠得多 ——
复制五遍会漂，而漂了不会报错。

## 刻意的边界

**只做 A 类。** B 类（`director` / `production_designer` / `gaffer` /
`line_producer`）的产物是结构化计划（`episode.plan` 等），
需要各自的规整逻辑；C 类（`cinematographer` 等）根本不是 LLM 职位。

把三类硬塞进一个"通用职位"，是最容易写出"什么都管、什么都管不好"的地方。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from little_crew.agent.llm import LLMError, LlmClient, extract_json
from little_crew.agent.messages import (
    Dimension,
    IssueLocation,
    QcScore,
    RepairHint,
    ReviewFeedback,
    ReviewIssue,
    Severity,
    Verdict,
)
from little_crew.agent.protocol import Budget, CallEnvelope, CallResult

__all__ = [
    "CRITIC_CHECKS",
    "CriticLlm",
    "build_critic_crew",
]

#: 每个 A 类职位"盯什么"。**这是它们唯一真正的差异。**
#:
#: 写法上刻意具体到"能判断"的程度 ——
#: 「节奏是否合适」这种话模型只会回一句"还行"，
#: 而「一场戏超过 12 秒且没有新的信息」是可以判定的。
CRITIC_CHECKS: dict[str, dict[str, Any]] = {
    "story_editor": {
        "capability": "style.guard",
        "artifact": "script.draft",
        "focus": "文风一致性",
        "checks": (
            "同一集里的叙述人称是否统一（不能一段第一人称、一段全知）",
            "用词层级是否一致（书面语与口语不能混用到出戏）",
            "句式是否过度重复（连续三句同样结构会显得机械）",
        ),
    },
    "continuity_supervisor": {
        "capability": "continuity.check",
        "artifact": "script.draft",
        "focus": "连贯性",
        "checks": (
            "同一人物在不同场次的外貌/年龄/身份描述是否矛盾",
            "时间线是否自洽（夜→晨的过渡有没有交代）",
            "关键物件（信、照片、竹篮）的出现与消失有没有交代",
        ),
    },
    "compliance_officer": {
        "capability": "compliance.check",
        "artifact": "script.draft",
        "focus": "合规",
        "checks": (
            "是否含未成年人不当内容、暴力细节过度描写",
            "是否使用了真实人物/真实商标的名字",
            "是否含地域/民族/性别歧视性表述",
        ),
    },
    "pacing_editor": {
        "capability": "pacing.plan",
        "artifact": "script.draft",
        "focus": "节奏",
        "checks": (
            "是否有连续的场次都在交代信息而没有推进（说明性冗余）",
            "高潮前是否有足够的铺垫（转折不能凭空出现）",
            "结尾是否收得住（不能戛然而止或拖尾）",
        ),
    },
    "review_board": {
        "capability": "review.board",
        "artifact": "shot.list",
        "focus": "成片整体",
        "checks": (
            "分镜是否覆盖了剧本的全部关键场次",
            "镜头时长分布是否合理（不能全是 8 秒或全是 3 秒）",
            "是否有明显的偷懒（一场只给一个镜头）",
        ),
    },
}

_SYSTEM = (
    "你是一位严格的{position_cn}，只负责审查「{focus}」。"
    "你的意见会直接驱动返工，所以必须具体、可执行、可核对。"
    "不要客套，不要总结优点，只指出问题。"
)


class CriticLlm:
    """把一个 A 类职位实现为真实 LLM 批评家。

    `impl(*, envelope, runtime) -> CallResult` —— 与其它职位同一契约。
    """

    def __init__(self, agent_id: str, *, client: LlmClient | None = None,
                 temperature: float = 0.2) -> None:
        if agent_id not in CRITIC_CHECKS:
            raise ValueError(
                f"{agent_id!r} 不是 A 类审查职位；"
                f"可选：{sorted(CRITIC_CHECKS)}")
        self.agent_id = agent_id
        self.spec = CRITIC_CHECKS[agent_id]
        self.capability: str = self.spec["capability"]
        self.artifact_key: str = self.spec["artifact"]
        self.client = client or LlmClient("deepseek-v4")
        self.temperature = temperature

    # ---- 提示词 --------------------------------------------------------

    def _render(self, payload: Mapping[str, Any]) -> tuple[str, str]:
        art = payload.get(self.artifact_key)
        body = art if isinstance(art, str) else _as_text(art)
        checks = "\n".join(f"  {i}. {c}"
                           for i, c in enumerate(self.spec["checks"], 1))
        system = _SYSTEM.format(position_cn=self.agent_id,
                                focus=self.spec["focus"])
        prompt = (
            f"## 待审内容\n{body or '（空）'}\n\n"
            f"## 你只负责检查这 {len(self.spec['checks'])} 条\n{checks}\n\n"
            "## 输出要求\n"
            "**逐项**给出结论 —— 每一条检查项都要有一句判断，"
            "不能跳项、不能合并。\n"
            "严格输出 JSON：\n"
            '{"observations": [\n'
            '   {"check": 1, "ok": true, "finding": "", "where": ""},\n'
            '   {"check": 2, "ok": true, "finding": "", "where": ""},\n'
            '   {"check": 3, "ok": true, "finding": "", "where": ""}\n'
            ' ],\n'
            ' "overall": "一句话总评"}\n\n'
            "规则：\n"
            "  · `ok`：**该项没问题就 true**；有问题就 false\n"
            "  · `finding`：`ok=false` 时写具体问题（含位置）；"
            "`ok=true` 时**留空字符串**\n"
            "  · ⚠️ **不要把「未见问题」写进 `finding`** —— "
            "没问题就用 `ok=true` 表达\n"
            "  · `where`：问题在哪一场/哪一镜；无问题留空\n"
            "  · **三项都必须出现** —— 漏项等于这一项没检查，"
            "而那会被当作「未检查」而不是「已通过」\n"
            "  · 不要评论这三条以外的东西（那不是你的职责）"
        )
        return prompt, system

    # ---- 调用 ----------------------------------------------------------

    def __call__(self, *, envelope: CallEnvelope,
                 runtime: Any = None) -> CallResult:
        payload = dict(envelope.payload or {})
        if not any(k in payload for k in (self.artifact_key,)):
            return CallResult.fail(
                "F-INPUT",
                f"{self.agent_id} 需要 payload[{self.artifact_key!r}]")
        try:
            prompt, system = self._render(payload)
        except Exception as e:                                # noqa: BLE001
            return CallResult.fail("F-RENDER", f"提示词构造失败: {e}")

        try:
            data = self.client.chat_json(prompt, system=system,
                                         what=f"{self.agent_id} 的审查意见")
        except LLMError as e:
            return CallResult.fail(
                f"F-LLM-{e.kind}", f"{self.agent_id} 调用失败: {e}",
                escalate=not e.retryable)
        except Exception as e:                                # noqa: BLE001
            return CallResult.fail("F-LLM", f"{self.agent_id} 解析失败: {e}")

        issues, hints, score = self._normalize(data, payload)
        verdict = (Verdict.REVISE if issues else Verdict.PASS)
        fb = ReviewFeedback(
            frm=self.agent_id, to="screenwriter", stage="gate",
            verdict=verdict, score=score, issues=tuple(issues),
            repair_hints=tuple(hints),
            # 审核方与生成方分属不同族（跨族审核是硬要求）
            reviewer_model=self.client.model_id,
            generator_model=str(payload.get("generatorModel") or ""))
        return CallResult.success({self.capability: fb})

    # ---- 规整 ----------------------------------------------------------

    def _normalize(self, data: Mapping[str, Any],
                   payload: Mapping[str, Any]
                   ) -> tuple[list[ReviewIssue], list[RepairHint], QcScore]:
        raw = data.get("issues") or []
        issues: list[ReviewIssue] = []
        hints: list[RepairHint] = []

        # ---- 逐项表态解析（2026-10-18 改）----
        #
        # ⚠️ **为什么不再只看 `issues`**：实测提示词里写"没有问题就返回空数组"
        # 时，模型对一份明显有人称混用/场景跳跃的三场戏给出了"0 个问题"。
        # 一个永远说 PASS 的审核者**比没有更糟** —— 它制造"检查过了"的假信心。
        #
        # 改成强制逐项表态后：
        #   · 每项必须有一句判断（`finding` 空 = 该项没问题）
        #   · **漏项**被记为一条 `minor` 问题（`未覆盖检查项 N`）——
        #     因为**未检查不等于已通过**（项目的铁律①）
        obs = data.get("observations")
        if isinstance(obs, list) and obs:
            want = list(range(1, len(self.spec["checks"]) + 1))
            seen_n: set[int] = set()
            for ob in obs:
                if not isinstance(ob, Mapping):
                    continue
                try:
                    n = int(ob.get("check"))
                except (TypeError, ValueError):
                    continue
                seen_n.add(n)
                finding = str(ob.get("finding") or "").strip()
                # ⚠️ **按 `ok` 布尔判定，不靠文本猜**（2026-10-18 实测教训）：
                # 第一版"finding 非空即问题"，而模型会把「未见问题」
                # 写进 finding —— 于是**把"没问题"报成了问题**。
                # 文本猜测不可靠，必须让它显式表态。
                ok_flag = ob.get("ok")
                if ok_flag is True:
                    continue
                if ok_flag is None:
                    # 没给布尔 -> 视为**未表态**，不作为已检查
                    seen_n.discard(n)
                    continue
                if not finding:
                    continue
                issues.append(ReviewIssue(
                    severity=Severity.MAJOR, dimension=Dimension.QUALITY,
                    problem=f"检查项 {n}（{self.spec['checks'][n-1]}）：{finding}",
                    suggestion="按问题描述修订该处",
                    location=IssueLocation(
                        scene=str(ob.get("where") or ""),
                        asset=self.artifact_key)))
                raw_hint = {"note": "按问题描述修订"}
                hints.append(RepairHint(
                    graph_id=f"G-{payload.get('episodeId', '') or 'E000'}",
                    op=_repair_op(), args=raw_hint,
                    from_issue=f"obs_{n:03d}"))
            missing = [n for n in want if n not in seen_n]
            if missing:
                issues.append(ReviewIssue(
                    severity=Severity.MINOR, dimension=Dimension.QUALITY,
                    problem=(f"本次审查**未覆盖**检查项 {missing} —— "
                             f"未检查不等于已通过"),
                    suggestion="补齐这几项的判断后再裁定",
                    location=IssueLocation(asset=self.artifact_key)))
        for i, it in enumerate(raw if isinstance(raw, list) else ()):
            if not isinstance(it, Mapping):
                continue
            problem = str(it.get("problem") or "").strip()
            if not problem:
                continue
            sev = str(it.get("severity") or "").strip().lower()
            severity = {"blocker": Severity.BLOCKER,
                        "major": Severity.MAJOR,
                        "minor": Severity.MINOR}.get(sev, Severity.MINOR)
            sugg = str(it.get("suggestion") or "").strip()
            # ⚠️ `BLOCKER` 必须带 suggestion —— 否则编排器判"阻塞问题无返工指令"
            # 而把**整轮作废**（不是"这条被忽略"）。这是实测踩过的坑。
            if severity is Severity.BLOCKER and not sugg:
                severity = Severity.MAJOR
            issues.append(ReviewIssue(
                severity=severity, dimension=Dimension.QUALITY,
                problem=problem,
                suggestion=sugg,
                location=IssueLocation(
                    scene=str(it.get("where") or ""),
                    asset=self.artifact_key)))
            if sugg:
                hints.append(RepairHint(
                    graph_id=f"G-{payload.get('episodeId', '') or 'E000'}",
                    op=_repair_op(), args={"note": sugg},
                    from_issue=f"issue_{i:03d}"))

        # 分数**由代码算**，不取模型自报 —— 模型对自己有多严厉没有稳定标定
        penalty = {"blocker": 40.0, "major": 15.0, "minor": 5.0}
        lost = sum(penalty[i.severity.value] for i in issues)
        dim = max(0.0, 100.0 - lost)
        score = QcScore(dimensions={
            Dimension.CONSISTENCY: dim,
            Dimension.COMPLETENESS: dim,
            Dimension.QUALITY: dim,
            Dimension.FEASIBILITY: dim,
            Dimension.COMPLIANCE: 100.0 if self.agent_id != "compliance_officer"
            else dim,
        }, phase="draft")
        return issues, hints, score


def _repair_op() -> str:
    """从**权威表**取一个合法返工操作名。

    ⚠️ 不要凭记忆写操作名 —— `RepairHint` 会校验，写错就抛 `ValueError`，
    表现成"审核者执行失败"（`alpha.36` 踩过）。
    """
    known: Sequence[str] = tuple(getattr(RepairHint, "KNOWN_OPS", ()) or ())
    for cand in ("modify_shot", "adjust_prompt", "regenerate_asset",
                 "update_binding"):
        if cand in known:
            return cand
    return str(known[0]) if known else "modify_shot"


def _as_text(art: Any) -> str:
    for attr in ("as_text", "to_text", "text"):
        v = getattr(art, attr, None)
        if isinstance(v, str) and v.strip():
            return v
        if callable(v):
            try:
                got = v()
                if isinstance(got, str):
                    return got
            except Exception:                                 # noqa: BLE001
                pass
    if isinstance(art, Mapping):
        import json
        return json.dumps(art, ensure_ascii=False)[:8000]
    return str(art or "")


def build_critic_crew(*, client: LlmClient | None = None,
                      only: Sequence[str] | None = None) -> dict[str, Any]:
    """构造 A 类职位实现。`only` 不给 = 全部 5 个。"""
    ids = list(only) if only is not None else list(CRITIC_CHECKS)
    return {rid: CriticLlm(rid, client=client) for rid in ids}
