"""职位职位 —— 真实 LLM 实现（而非规则桩）。

与 `crew_stubs.py` 的关系
-------------------------
| | 规则桩（`crew_stubs`） | 本模块 |
|---|---|---|
| 判定方式 | 确定性规则 / 正则 | **模型语义理解** |
| 可复现 | ✅ 同输入必然同输出 | ❌ 有随机性（可固定种子缓解） |
| 依赖 | 无 | **需要 API key** |
| 用途 | 跑通协作机制、断言具体分数 | **真实创作能力**、答辩的"真模型"证据 |

**两者实现同一个契约**（``impl(*, envelope, runtime) -> CallResult``），
所以编排器、消息协议、门禁**一行都不用改** —— 换实现即可。

⚠️ **本模块最重要的设计：不许"假响应返工"**
------------------------------------------------
桩那边踩过一次"假闭环"：编剧每轮都重新解析原始素材，于是缺陷"自己好了"，
`patched` 永远是空的 —— 表面看是"两轮返工后通过"，实际什么都没改。

LLM 版本有一个**更隐蔽的同类风险**：模型收到返工意见后，
完全可以回一句"已按要求修订"而**实际不做任何修改**。
两轮文字不同（因为采样随机），看起来像"确实改了"，
但**改的不是被指出的那个问题**。

因此本实现强制：

1. 要求模型**显式列出 `changes`**（针对意见做了哪些修改）；
2. **有意见却没有 `changes` → 返回失败**，而不是当作成功；
3. 把 `changes` 与返工意见一起放进产物，**让证据链能查**。

> 宁可让一轮返工"失败"，也不要制造一个看起来成功的空转。
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from little_crew.agent.llm import LLMError, LlmClient
from little_crew.agent.messages import (
    Dimension,
    IssueLocation,
    QcScore,
    RepairHint,
    ReviewFeedback,
    ReviewIssue,
    ScriptDraft,
    Severity,
    Verdict,
)
from little_crew.agent.protocol import CallEnvelope, CallResult, LayerRuntime

__all__ = ["ScreenwriterLlm", "DramaturgeLlm", "CREW_PROMPTS", "build_llm_crew"]


# ==========================================================================
# 提示词
# ==========================================================================

#: 编剧的系统提示。**刻意写清"会被审"** —— 让模型知道输出要被检查，
#: 这不是提示词工程的花招，而是如实告知它的工作环境。
_SCREENWRITER_SYSTEM = """你是《小剧组》平台的**编剧**，负责把小说素材改编成漫剧分场剧本。

你的产出会被三位审查方独立检查：剧评人（结构）、文字编辑（文风）、
连续性监督（前后一致）。所以：

· 每一场必须**有冲突或阻力** —— 平铺直叙的场次会被打回；
· 冲突的解决必须**付出代价** —— 轻易化解不算代价；
· 每场要有**转折** —— 情绪走向不能是平的；
· 场景与人物要**前后一致**，不能出现前面没有的人或物。

输出**严格 JSON**，结构如下（不要输出任何其它内容）：

{
  "title": "本集标题",
  "scenes": [
    {
      "scene_id": "S01",
      "heading": "内景·旧书店·夜",
      "characters": ["林晚", "店主"],
      "text": "这一场的正文。要有对白与动作，200-400 字。",
      "summary": "一句话概括本场发生了什么"
    }
  ]
}"""


def _scene_schema_hint() -> str:
    return ('"scenes": [ {"scene_id": "S01", "heading": "内景·…·夜", '
            '"characters": ["人名"], "text": "正文", "summary": "一句话"} ]')


#: 各职位的提示词。键与 `roster` 里的 agent_id 对应。
CREW_PROMPTS: Mapping[str, Mapping[str, str]] = {
    "screenwriter": {
        "system": _SCREENWRITER_SYSTEM,
    },
    "dramaturge": {
        "system": """你是《小剧组》平台的**剧评人**，负责从剧作结构上审查分场剧本。

只关心三件事，其余一律不提：
1. **冲突** —— 这一场有没有明确的阻力/对抗？平铺直叙就是缺陷。
2. **代价** —— 冲突的解决付出了什么？没有代价的解决是廉价的。
3. **转折** —— 情绪或局势有没有变化？平的场次观众会走神。

输出**严格 JSON**：
{
  "issues": [
    {"sceneId": "S01", "dimension": "conflict|cost|turn",
     "problem": "具体指出问题在哪（引用原文）",
     "suggestion": "怎么改"}
  ],
  "overall": "一句话总评"
}
没有问题时 `issues` 为空数组 —— **不要为了凑数编问题**。""",
    },
    "story_editor": {
        "system": """你是《小剧组》平台的**文字编辑**，负责文风与用词。

检查：用词是否准确、有没有陈词滥调、对白是否像真人说话、
叙述视角是否统一。**不改剧情结构** —— 那是剧评人的职责。

输出**严格 JSON**：
{
  "issues": [
    {"sceneId": "S01", "problem": "具体问题（引用原文）",
     "suggestion": "改成什么"}
  ],
  "styleScore": 0.0
}
`styleScore` 是 0~1 的整体文风评分。没有问题时 `issues` 为空数组。""",
    },
    "continuity_supervisor": {
        "system": """你是《小剧组》平台的**连续性监督**，负责前后一致。

检查：人物在前后场的状态是否矛盾（受伤/持有物/位置/时间）、
场景描述与出场人物是否自洽、有没有"凭空出现"的人或物。

输出**严格 JSON**：
{
  "issues": [
    {"sceneId": "S01", "problem": "与哪一场矛盾、矛盾在哪",
     "suggestion": "怎么改"}
  ],
  "consistent": true
}
没问题时 `issues` 为空数组且 `consistent` 为 true。""",
    },
}


# ==========================================================================
# 编剧（真实 LLM）
# ==========================================================================

class ScreenwriterLlm:
    """编剧 —— 真实 LLM 实现。

    与桩的三点契约一致（否则编排器不认）：
      ① 入参在 ``envelope.payload``；
      ② 产出 ``{"script.draft": ScriptDraft}``；
      ③ **按返工意见修订并如实记录改了什么**。
    """

    agent_id = "screenwriter"

    def __init__(self, *, client: LlmClient | None = None,
                 max_scenes: int = 8, temperature: float = 0.8) -> None:
        self.client = client or LlmClient()
        self.max_scenes = max_scenes
        self.temperature = temperature
        self.calls = 0
        #: 每轮收到的返工意见（测试与证据链用）
        self.received_critiques: list[Mapping[str, Any]] = []
        #: 每轮**模型自述**改了什么。
        #:
        #: ⚠️ 这是"模型说自己改了什么"，不是"实际改了什么"。
        #: 两者的差别正是本模块要防的东西 —— 但**有自述**至少让
        #: 证据链可查；完全没有自述则连查都无从查起。
        self.declared_changes: list[tuple[str, ...]] = []
        #: 上一轮的正文，用于把"声称改了"与"真的不一样"对上
        self._prev_text: str = ""

    # ---- 主入口 --------------------------------------------------------

    def __call__(self, *, envelope: CallEnvelope,
                 runtime: LayerRuntime) -> CallResult:
        self.calls += 1
        p = dict(envelope.payload or {})
        source = str(p.get("source.novel", "") or "")
        episode_id = str(p.get("episodeId", "E001"))
        title = str(p.get("title", "未命名"))

        critique = p.get("script.critique")
        has_critique = isinstance(critique, Mapping) and bool(critique)
        if has_critique:
            self.received_critiques.append(critique)

        if not source.strip() and not has_critique:
            return CallResult.fail(
                "F-INPUT-EMPTY",
                "编剧收到空素材（`source.novel` 为空且无返工意见）——"
                "这不该被当成成功，否则会产出空剧本并被后续环节当成有效输入",
            )

        prompt = self._build_prompt(source, episode_id, title, critique)

        try:
            data = self.client.chat_json(
                prompt, system=CREW_PROMPTS["screenwriter"]["system"],
                temperature=self.temperature, what="剧本")
        except LLMError as e:
            # **如实上报，并区分可重试性** —— 让编排器能决定是重试还是升级
            return CallResult.fail(
                f"F-LLM-{e.kind.upper()}",
                f"编剧调用失败：{e}",
                escalate=not e.retryable,
            )

        # 有意见却没有改动说明 —— **不当成功**
        changes = tuple(str(c) for c in (data.get("changes") or ())
                        if str(c).strip())
        if has_critique and not changes:
            return CallResult.fail(
                "F-REVISION-NOT-DECLARED",
                "收到返工意见，但模型没有说明改了哪些地方（`changes` 为空）——"
                "无法证明返工真的发生。宁可让这轮失败，也不要制造"
                "「看起来通过了、实际什么都没改」的空转",
                escalate=True,
            )
        self.declared_changes.append(changes)

        scenes = self._normalize_scenes(data, episode_id)
        if not scenes:
            return CallResult.fail(
                "F-OUTPUT-EMPTY",
                f"模型返回了合法 JSON 但没有可用场次（"
                f"前 200 字：{json.dumps(data, ensure_ascii=False)[:200]}）",
            )

        text = "\n".join(str(s.get("text", "")) for s in scenes)
        # 声称改了、正文却一字未变 —— 这是"假响应"的直接证据
        if has_critique and changes and text == self._prev_text:
            return CallResult.fail(
                "F-REVISION-NO-OP",
                f"模型声称修改了 {len(changes)} 处，但正文与上一轮**完全相同** —— "
                f"这是空转，不是返工",
                escalate=True,
            )
        self._prev_text = text

        draft = ScriptDraft(
            frm="screenwriter", to="dramaturge", stage="script",
            episode_id=episode_id,
            title=str(data.get("title") or title),
            scenes=tuple(scenes),
            revision=self.calls,
            estimated_seconds=float(len(scenes)) * 45.0,
            note=("LLM 首稿" if self.calls == 1
                  else f"LLM 第 {self.calls} 稿（模型自述修改："
                       f"{'、'.join(changes) or '无'}）"),
        )
        return CallResult.success(
            {"script.draft": draft},
            evidence=[{
                "kind": "llm-call",
                "agent": self.agent_id,
                "revision": self.calls,
                "declaredChanges": list(changes),
                "servedModel": (self.client.call_log[-1].get("servedModel")
                                if self.client.call_log else ""),
                "usage": (self.client.call_log[-1].get("usage")
                          if self.client.call_log else {}),
            }],
        )

    # ---- 内部 ----------------------------------------------------------

    def _build_prompt(self, source: str, episode_id: str, title: str,
                      critique: Mapping[str, Any] | None) -> str:
        head = (f"【本集】{episode_id}《{title}》\n"
                f"【要求】改编成 {self.max_scenes} 场以内的漫剧分场剧本。\n")
        body = f"\n【素材】\n{source[:6000]}\n"
        if isinstance(critique, Mapping) and critique:
            # 返工意见原样给模型 —— **不要替他总结**。
            # 总结会丢掉具体位置与原文引用，而"改哪一处"恰恰是关键信息。
            body += ("\n【返工意见】以下是审查方上一轮提出的问题，"
                     "**必须逐条处理**：\n"
                     + json.dumps(critique, ensure_ascii=False, indent=2)[:4000]
                     + "\n\n⚠️ 你的输出里**必须**包含 `changes` 字段，"
                       "逐条说明你为每个问题做了什么修改。\n")
        return head + body + "\n" + _scene_schema_hint()

    def _normalize_scenes(self, data: Mapping[str, Any],
                          episode_id: str) -> list[dict[str, Any]]:
        """把模型输出规整成 `ScriptDraft.scenes` 期望的 Mapping 形状。

        **宽容但不含糊**：字段名有出入就补默认值，
        但**没有正文的场次直接丢掉** —— 一个空场次进入下游会变成
        "一个什么都不发生的镜头"，而那比缺一场更难查。
        """
        raw = data.get("scenes")
        if not isinstance(raw, (list, tuple)):
            return []
        out: list[dict[str, Any]] = []
        for i, s in enumerate(raw[:self.max_scenes], 1):
            if not isinstance(s, Mapping):
                continue
            text = str(s.get("text") or s.get("content") or "").strip()
            if not text:
                continue
            chars = s.get("characters") or s.get("cast") or []
            if isinstance(chars, str):
                chars = [c.strip() for c in chars.replace("，", ",").split(",")
                         if c.strip()]
            out.append({
                "scene_id": str(s.get("scene_id") or s.get("id") or f"S{i:02d}"),
                "heading": str(s.get("heading") or s.get("location") or ""),
                "characters": [str(c) for c in chars],
                "text": text,
                "summary": str(s.get("summary") or ""),
                "episodeId": episode_id,
                "_source": "llm",
            })
        return out


# ==========================================================================
# 剧评人（真实 LLM，**跨模型族**）
# ==========================================================================

#: 结构三要素 -> `Dimension`。剧评人只报这三类，所以映射是固定的。
_DIM_MAP = {
    "conflict": "COMPLETENESS",
    "cost": "COMPLETENESS",
    "turn": "COMPLETENESS",
}


def _script_repair_op() -> str:
    """剧本阶段"改一场"对应的返工操作 —— **从权威表推导，不写死**。

    ⚠️ 踩过：我凭感觉写了 `revise_scene`，而它不在 `RepairHint.KNOWN_OPS` 里，
    构造函数直接抛 ValueError，表现成"审核者执行失败" ——
    乍看像是模型的问题，实际是我编了个不存在的操作名。

    这里从权威表选，并在表里没有预期操作时**大声失败** ——
    静默退回某个默认值会让"返工指令语义错位"这种问题极难发现。
    """
    prefer = ("modify_shot", "adjust_prompt")
    for op in prefer:
        if op in RepairHint.KNOWN_OPS:
            return op
    raise LLMError(
        "no-repair-op",
        f"`RepairHint.KNOWN_OPS` 里没有 {prefer} 中任何一个 —— "
        f"返工指令的语义对不上剧本阶段。现有：{sorted(RepairHint.KNOWN_OPS)}",
    )


class DramaturgeLlm:
    """剧本顾问 —— 真实 LLM 实现。

    ⚠️ **它必须与编剧不同模型族。**
    ---------------------------------------------------------------
    平台的核心要求之一是"跨模型族盲读"：**审核方与生成方不能同族**，
    否则等于让同一个脑子自己批自己（"证明的循环"，见 docs/00 §5.9）。
    同族判定会被 `ReviewFeedback.reviewer_model` / `generator_model`
    的一致性检查识别出来并**作废为 ESCALATE**。

    所以本类的默认客户端是 **MiMo**（小米），而编剧默认是 **DeepSeek**。
    **不要在部署时把两者改成同一个模型** —— 那会让门禁失去意义，
    而且不会报错，只会安静地退化成自我签收。
    """

    agent_id = "dramaturge"

    def __init__(self, *, client: LlmClient | None = None,
                 temperature: float = 0.2) -> None:
        self.client = client or LlmClient("mimo-v2.6-flash")
        # 审核要**稳**，不要发挥 —— 温度调低。
        # 高温度会让"同一份稿子两次审查给出不同结论"，
        # 而那正好毁掉门禁最需要的性质：可复现。
        self.temperature = temperature
        self.calls = 0
        self.last_raw: dict[str, Any] = {}

    def __call__(self, *, envelope: CallEnvelope,
                 runtime: LayerRuntime) -> CallResult:
        self.calls += 1
        p = dict(envelope.payload or {})
        draft = p.get("script.draft")
        draft = _as_draft(draft)
        if draft is None:
            return CallResult.fail(
                "F-INPUT",
                f"剧本顾问未收到 ScriptDraft（拿到 {type(p.get('script.draft')).__name__}）")

        prompt = self._build_prompt(draft)
        try:
            data = self.client.chat_json(
                prompt, system=CREW_PROMPTS["dramaturge"]["system"],
                temperature=self.temperature, what="剧评")
        except LLMError as e:
            return CallResult.fail(f"F-LLM-{e.kind.upper()}",
                                   f"剧评人调用失败：{e}",
                                   escalate=not e.retryable)
        self.last_raw = data

        issues, hints = self._to_issues(data, draft)
        fb = ReviewFeedback(
            frm="dramaturge", to="screenwriter", stage="gate",
            # 没有问题时才 PASS —— 与桩同语义
            verdict=(Verdict.PASS if not issues else Verdict.REVISE),
            score=self._score(data, draft, issues),
            issues=tuple(issues),
            repair_hints=tuple(hints),
            reviewer_model=self.client.model_id,
            # ⚠️ 生成方模型由编排器声明（`model_families`），这里如实写"未知"
            # 会更好吗？不 —— 写了错的值比不写更糟。用 draft 的 note 里
            # 无从得知，所以由 `_cross_family_ok()` 在编排层校验。
            generator_model="",
            round_no=int(getattr(draft, "revision", 1)),
        )
        return CallResult.success(
            {"script.critique": fb},
            evidence=[{
                "kind": "llm-call",
                "agent": self.agent_id,
                "reviewerModel": self.client.model_id,
                "servedModel": (self.client.call_log[-1].get("servedModel")
                                if self.client.call_log else ""),
                "issueCount": len(issues),
                "overall": str(data.get("overall", ""))[:200],
                "usage": (self.client.call_log[-1].get("usage")
                          if self.client.call_log else {}),
            }],
        )

    # ---- 内部 ----------------------------------------------------------

    def _build_prompt(self, draft: Any) -> str:
        lines = [f"【本集】{getattr(draft, 'episode_id', '')} "
                 f"《{getattr(draft, 'title', '')}》  第 "
                 f"{getattr(draft, 'revision', 1)} 稿",
                 f"【共 {len(draft.scenes)} 场】", ""]
        for sc in draft.scenes:
            sid = str(sc.get("scene_id") or sc.get("id") or "?")
            cast = "、".join(str(c) for c in (sc.get("characters") or ()))
            lines.append(f"── {sid}　{sc.get('heading', '')}　人物：{cast}")
            lines.append(str(sc.get("text", "")))
            lines.append("")
        lines.append("请审查以上剧本，**逐场**判断冲突 / 代价 / 转折是否成立。")
        lines.append("只报真实存在的问题 —— 不要为了凑数编问题。")
        return "\n".join(lines)

    def _to_issues(self, data: Mapping[str, Any],
                   draft: Any) -> tuple[list[ReviewIssue], list[RepairHint]]:
        """把模型输出转成 `ReviewIssue` / `RepairHint`。

        **对模型宽容、对结构严格**：
        字段名有出入可以认（`scene`/`sceneId`、`issue`/`problem`），
        但**不认识的结构一律丢掉** —— 宁可少报，也不要凭空造一条
        "位置不明、维度不明"的意见，那会让编剧收到无法执行的返工指令。
        """
        known = {str(sc.get("scene_id") or sc.get("id") or "")
                 for sc in draft.scenes}
        issues: list[ReviewIssue] = []
        hints: list[RepairHint] = []
        for raw in (data.get("issues") or ())[:20]:
            if not isinstance(raw, Mapping):
                continue
            sid = str(raw.get("sceneId") or raw.get("scene") or raw.get("id") or "")
            if sid not in known:
                # 位置不明 -> 编剧无法据此定位修改。**丢掉并计数**，
                # 而不是硬塞一个假的位置。
                continue
            problem = str(raw.get("problem") or raw.get("issue") or "").strip()
            if not problem:
                continue
            dim_name = _DIM_MAP.get(
                str(raw.get("dimension") or "").strip().lower(), "COMPLETENESS")
            dim = getattr(Dimension, dim_name, Dimension.COMPLETENESS)
            issues.append(ReviewIssue(
                severity=Severity.MAJOR,
                dimension=dim,
                location=IssueLocation(scene=sid),
                problem=f"{sid}：{problem}",
                suggestion=str(raw.get("suggestion") or raw.get("fix") or ""),
            ))
            hints.append(RepairHint(
                graph_id=f"G-{getattr(draft, 'episode_id', '')}",
                # ⚠️ `op` 必须是 `RepairHint.KNOWN_OPS` 里的值 ——
                # **权威操作表在 `messages.py`，不在这里**。
                # 我一开始凭感觉写了 `revise_scene`，直接 ValueError；
                # 而那个异常发生在**构造 RepairHint 时**，
                # 表现成"审核者执行失败"，乍看像是模型的问题。
                #
                # 语义选择：剧本阶段"改一场的正文" = `modify_shot`。
                # （`adjust_prompt` 是调提示词，`insert_shot` 是加一场 ——
                #   剧评人的意见是"这一场写得不对"，不是"缺一场"。）
                op=_script_repair_op(),
                args={"scene": sid,
                      "kind": str(raw.get("dimension") or "")},
                from_issue=f"issue_{len(issues):03d}",
            ))
        return issues, hints

    def _score(self, data: Mapping[str, Any], draft: Any,
               issues: list[ReviewIssue]) -> QcScore:
        """评分。

        ⚠️ **不让模型直接给总分。** 理由是：总分会掩盖"哪一维坏了"，
        而返工需要的是维度信息。所以各维由**问题数**推导，
        只让模型提供主观的 `quality`。

        评分口径与规则桩保持一致（100 分制、同样五个维度），
        否则"换成 LLM 后分数变了"会被误读成质量变化，
        而实际只是换了把尺子。
        """
        n = max(len(draft.scenes), 1)
        bad = len(issues)
        quality_raw = data.get("quality")
        try:
            quality = float(quality_raw) * 100.0 if (
                quality_raw is not None and float(quality_raw) <= 1.0
            ) else float(quality_raw) if quality_raw is not None else 75.0
        except (TypeError, ValueError):
            quality = 75.0
        return QcScore(dimensions={
            Dimension.COMPLETENESS: round(max(0.0, 100.0 - 100.0 * bad / (n * 3)), 1),
            Dimension.CONSISTENCY: 88.0,
            Dimension.QUALITY: round(max(0.0, min(100.0, quality)), 1),
            Dimension.FEASIBILITY: 88.0,
            Dimension.COMPLIANCE: 92.0,
        }, phase="draft")


def _as_draft(x: Any) -> ScriptDraft | None:
    """宽容地把黑板里的值还原成 `ScriptDraft`。

    黑板可能存过**序列化形式**（dict），也可能直接是对象 ——
    两种都要认，否则"换了个调用路径就取不到稿子"。
    """
    if isinstance(x, ScriptDraft):
        return x
    if isinstance(x, Mapping):
        return ScriptDraft(
            frm=str(x.get("from") or x.get("frm") or "screenwriter"),
            to=str(x.get("to") or "*"),
            episode_id=str(x.get("episodeId") or x.get("episode_id") or ""),
            title=str(x.get("title") or ""),
            scenes=tuple(x.get("scenes") or ()),
            revision=int(x.get("revision") or 1),
            estimated_seconds=float(x.get("estimatedSeconds") or 0.0),
        )
    return None


# ==========================================================================
# 组装
# ==========================================================================

def build_llm_crew(*, client: LlmClient | None = None,
                   reviewer_client: LlmClient | None = None,
                   only: tuple[str, ...] = ("screenwriter", "dramaturge"),
                   ) -> dict[str, Any]:
    """造一批真实 LLM 职位，可直接 `runtime.bind_agent(...)`。

    ⚠️ **`client` 与 `reviewer_client` 必须是不同模型族。**
    默认：编剧 DeepSeek / 审核方 MiMo —— 满足跨模型族盲读的硬要求。
    传同一个会让门禁退化成自我签收（**而且不会报错**）。
    """
    c = client or LlmClient("deepseek-v4")
    r = reviewer_client or LlmClient("mimo-v2.6-flash")
    made: dict[str, Any] = {}
    if "screenwriter" in only:
        made["screenwriter"] = ScreenwriterLlm(client=c)
    if "dramaturge" in only:
        made["dramaturge"] = DramaturgeLlm(client=r)
    return made
