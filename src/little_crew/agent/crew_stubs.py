"""5 个核心职位的**规则桩**实现 —— 把协作链路跑通。

为什么先做桩，不直接接 LLM
-------------------------
| 理由 | 说明 |
|---|---|
| **可测试** | 桩是确定性的，测试可断言具体分数与裁定；LLM 输出不可复现 |
| **可复现** | 同一输入必然同一输出 —— 与平台的哈希/缓存体系同构 |
| **不被 API 阻塞** | 协作机制的验证不应依赖 API key 与配额 |
| **契约先行** | 先把 `impl(*, envelope, runtime)` 的契约跑实，接真模型时只换实现 |

**桩不是玩具**：它实现的是**真实的规则**（时长上限、镜头切分、
结构完整性、跨场一致性），只是判定逻辑用确定性规则而非语义理解。
因此它能真实地暴露"协作机制是否工作"。

⚠️ **桩的产出与 LLM 的产出走同一条消息通道** ——
接口一致，所以 R11 接真模型时**编排器一行都不用改**。

实现契约
--------
由 ``LayerRuntime.call_agent`` 决定::

    def impl(*, envelope: CallEnvelope, runtime: LayerRuntime) -> CallResult

入参在 ``envelope.payload``，**不是** ``payload=`` 关键字。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping, Sequence

from little_crew.agent.messages import (
    MAX_SHOT_SECONDS,
    Dimension,
    IssueLocation,
    QcScore,
    RepairHint,
    ReviewFeedback,
    ReviewIssue,
    ScriptDraft,
    Severity,
    Shot,
    ShotList,
    Verdict,
)
from little_crew.agent.protocol import CallEnvelope, CallResult, LayerRuntime

__all__ = [
    "ScreenwriterRules",
    "DramaturgeRules",
    "StoryEditorRules",
    "StoryboardArtistRules",
    "ReviewBoardRules",
    "CORE_CREW_STUBS",
    "default_crew_stubs",
]


D = Dimension


# ==========================================================================
# 共用工具
# ==========================================================================

#: 场景切分的标记（桩用结构化输入；真模型会做语义切分）
_SCENE_MARK = re.compile(r"^\s*(?:#{1,3}\s*)?\[?(?:场|SC)[-\s]?(\d+)\]?[:：]?\s*(.*)$")

#: 各结构元素的检出模式。
#:
#: **隐性表达也要认**：早期版本只匹配"但/却/然而"，把
#: 「她甩开了」「没有回头」「他拒绝」这类**动作层面的冲突**全漏了 ——
#: 而戏剧冲突恰恰大多写在动作里，不在连词里。
#: 规则太窄的后果是"审查意见与文本实际质量不符"，比不检查更糟。
_STRUCTURE_PATTERNS: Mapping[str, str] = {
    "conflict": (
        r"但|却|然而|阻止|拒绝|争|冲突|抵抗"
        r"|甩开|推开|挣开|挣脱"
        r"|没有(?:回头|停|接|答|理|让)|不肯|不愿|不肯|偏不"
        r"|抓住|拽住|拦住|挡住"
        r"|沉默|僵|对峙|别过脸"
    ),
    "cost": (
        r"代价|失去|牺牲|付出|放弃|毁了"
        r"|再也|永远不|回不去|来不及"
        r"|消失了|没了|碎了|断了|化开"
        r"|很难看|苦笑|咬(?:牙|唇)"
    ),
    "turn": (
        r"转身|忽然|突然|没想到|原来|竟然|反而|终于|这时|那一刻"
    ),
}

#: 结构必需元素：(键, 中文名, 缺失时的说明)
_STRUCTURE_RULES: tuple[tuple[str, str, str], ...] = (
    ("conflict", "冲突", "本场缺少明确的冲突或阻力"),
    ("cost", "代价", "本场冲突解决没有付出代价"),
    ("turn", "转折", "本场没有转折，情绪是平的"),
)


def _digest(*parts: Any) -> str:
    """确定性的短 id。**不含时间戳** —— 同输入必然同 id，便于比对与缓存。"""
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:10]


def _parse_scenes(source: str, episode_id: str) -> tuple[dict[str, Any], ...]:
    """把结构化剧本文本切成场次。

    桩的切分规则是确定性的：匹配 ``[场N]`` / ``SC-N`` 或 ``# N`` 标题行。
    段落正文归入最近一个场次。**没有标记时整段视为一场** ——
    这样即便输入是散文，也能产出可审查的结构。
    """
    scenes: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    body: list[str] = []

    def flush() -> None:
        if current is not None:
            current["text"] = "\n".join(body).strip()
            scenes.append(current)

    for raw in source.splitlines():
        m = _SCENE_MARK.match(raw)
        if m:
            flush()
            idx = int(m.group(1))
            current = {
                "id": f"SC-{episode_id}-{idx:03d}",
                "heading": (m.group(2) or "").strip(),
                "index": idx,
            }
            body = []
        else:
            body.append(raw)
    flush()

    if not scenes:
        scenes.append({"id": f"SC-{episode_id}-001", "heading": "开场",
                       "index": 1, "text": source.strip()})
    return tuple(scenes)


def _flaws_of(scene: Mapping[str, Any]) -> list[str]:
    """检出一场戏缺哪些结构元素。**纯规则，确定性。**"""
    blob = f"{scene.get('heading', '')}\n{scene.get('text', '')}"
    return [key for key, _, _ in _STRUCTURE_RULES
            if not re.search(_STRUCTURE_PATTERNS[key], blob)]


def _characters_of(scene: Mapping[str, Any]) -> tuple[str, ...]:
    """从场次文本里抽出角色名（``@名字`` 标记或已知资产 id）。"""
    text = str(scene.get("text", "")) + str(scene.get("heading", ""))
    found = re.findall(r"@([\w\u4e00-\u9fff]{1,12})", text)
    found += re.findall(r"\b(CHAR-\d{4})\b", text)
    seen: dict[str, None] = {}
    for f in found:
        seen.setdefault(f, None)
    return tuple(seen)


# ==========================================================================
# 1) 编剧
# ==========================================================================

#: 修订时插入的"补丁句"—— 让规则可检出的结构元素真正出现在文本里。
#:
#: ⚠️ 桩**必须真的改文本**。早期版本每轮重新生成同样的内容，
#: 于是三轮结果完全一致（68.1 分、同样 2 条意见），
#: 表面上"跑了三轮返工"，实际**返工闭环是假的**。
#: 这正是桩最容易骗过自己的地方。
_PATCH_SENTENCE: Mapping[str, str] = {
    "conflict": "她甩开他的手，别过脸去，没有回答。",
    "cost": "代价是她再也回不到从前那种笃定里了。",
    "turn": "忽然，她转身走进雨里，反而笑了一下。",
}


class ScreenwriterRules:
    """编剧（规则桩）。

    **两个关键行为**（缺一不可）：

    1. **首稿故意留结构缺陷** —— 否则验证的是"流程能跑"，
       而不是"协作机制有效"
    2. **按返工指令针对性修订** —— 否则三轮结果完全相同，
       "返工闭环"只是走过场

    第 2 条尤其重要：一个不真正响应反馈的实现，会让整套协作机制
    看起来正常运转，却什么都没改变。**这是桩最会骗人的地方。**
    """

    def __init__(self, *, introduce_flaw: bool = True) -> None:
        self.introduce_flaw = introduce_flaw
        self.calls = 0
        #: 记录每轮收到的返工指令（测试与演示用）
        self.received_critiques: list[Mapping[str, Any]] = []
        #: 记录每轮实际修补了哪些结构元素
        self.patched: list[tuple[str, ...]] = []

    def __call__(self, *, envelope: CallEnvelope, runtime: LayerRuntime) -> CallResult:
        self.calls += 1
        p = dict(envelope.payload or {})
        source = str(p.get("source.novel", "") or "")
        episode_id = str(p.get("episodeId", "E001"))

        critique = p.get("script.critique")
        if isinstance(critique, Mapping):
            self.received_critiques.append(critique)

        scenes = [dict(s) for s in _parse_scenes(source, episode_id)]

        # 先按返工指令修订 —— 修补要作用在**未注入缺陷**的文本上，
        # 否则"修好的"和"注入坏的"会互相覆盖，结果不可预测。
        done = self._apply_critique(scenes, critique)
        self.patched.append(done)

        # 缺陷**持续到被修补为止**。
        #
        # ⚠️ 这里曾有一个导致"假闭环"的设计错误：早期版本只在**首轮**注入缺陷，
        # 次轮直接重新解析原始素材 —— 于是缺陷"自己好了"，
        # 编剧根本没用到审查意见，`patched` 永远是空的。
        # 表面上看是"两轮返工后通过"，实际什么都没改。
        #
        # 正确做法：只要该元素**还没被修补**，就持续缺失 ——
        # 这样"修订后通过"才有因果意义。
        if self.introduce_flaw and "turn" not in done and scenes:
            last = scenes[-1]
            if "turn" not in _flaws_of(last):
                last["text"] = _inject_flaw(str(last.get("text", "")))
                last["_injectedFlaw"] = "missing_turn"

        draft = ScriptDraft(
            frm="screenwriter", to="dramaturge", stage="script",
            episode_id=episode_id,
            title=str(p.get("title", "未命名")),
            scenes=tuple(scenes),
            revision=self.calls,
            estimated_seconds=float(len(scenes)) * 45.0,
            note=("首稿" if self.calls == 1
                  else f"第 {self.calls} 稿（修补：{'、'.join(done) or '无'}）"),
        )
        return CallResult.success({"script.draft": draft})

    # ---- 修订 ----------------------------------------------------------

    def _apply_critique(self, scenes: list[dict[str, Any]],
                        critique: Mapping[str, Any] | None) -> tuple[str, ...]:
        """把审查意见翻译成实际的文本修补。返回本轮修补的结构元素。

        **修补依据是「审查意见指向哪个场次」，不是「当前文本缺什么」。**

        ⚠️ 这里曾有一个导致"假闭环"的判据错误：早期版本遍历场次、
        只修补**当前缺该元素**的场次。但次轮拿到的是**重新解析的干净素材**
        （缺陷是上轮注入的，素材本身没有），于是匹配不到任何场次，
        `applied` 恒为空 —— 表现为"返工指令收到了、但什么都没改"。

        正确的做法是用审查意见自带的**结构化定位**（``location.scene``）
        找到目标场次，把意见里的问题当作"这里需要补什么"来处理。
        """
        if not isinstance(critique, Mapping):
            return ()

        #: 场次 id -> 该场次需要补的结构元素
        need: dict[str, set[str]] = {}
        #: 无定位时的兜底目标（最后一场）
        fallback: set[str] = set()

        def _label_to_key(text: str) -> str | None:
            for key, label, _ in _STRUCTURE_RULES:
                if f"「{label}」" in text or label in text:
                    return key
            return None

        for iss in critique.get("issues", ()) or ():
            if not isinstance(iss, Mapping):
                continue
            key = _label_to_key(str(iss.get("problem", "")))
            if key is None:
                continue
            sid = str((iss.get("location") or {}).get("scene", ""))
            if sid:
                need.setdefault(sid, set()).add(key)
            else:
                fallback.add(key)

        for h in critique.get("repairHints", ()) or ():
            if not isinstance(h, Mapping):
                continue
            purpose = str((h.get("args") or {}).get("purpose", ""))
            key = _label_to_key(purpose)
            if key is None:
                continue
            after = str((h.get("args") or {}).get("after", ""))
            if after:
                need.setdefault(after, set()).add(key)
            else:
                fallback.add(key)

        if not need and not fallback:
            return ()

        # 无定位的意见应用到**最后一场**（结构性缺陷通常收束在结尾）
        if fallback and scenes:
            need.setdefault(str(scenes[-1].get("id", "")), set()).update(fallback)

        applied: set[str] = set()
        for sc in scenes:
            sid = str(sc.get("id", ""))
            want_here = need.get(sid)
            if not want_here:
                continue
            body = str(sc.get("text", ""))
            for key in sorted(want_here):
                sentence = _PATCH_SENTENCE[key]
                if sentence in body:          # 幂等：已补过就不重复
                    continue
                body = body + "\n" + sentence
                applied.add(key)
            sc["text"] = body
            if applied:
                sc.setdefault("_patched", [])       # type: ignore[union-attr]
        return tuple(sorted(applied))


def _inject_flaw(text: str) -> str:
    """把一个**规则可检出**的缺陷注入文本：抹掉所有"转折"信号。

    只删转折，保留冲突与代价 —— 这样修复是"补一句转折"，
    而不是"重写整场"，能更清楚地展示闭环的每一步。
    """
    for pat in ("转身", "忽然", "突然", "没想到", "原来", "竟然", "反而",
                "终于", "这时", "那一刻"):
        text = text.replace(pat, "")
    return text


# ==========================================================================
# 2) 剧本顾问（看戏剧结构）
# ==========================================================================

class DramaturgeRules:
    """剧本顾问（规则桩）。**只审别人的稿**，关注戏剧结构。"""

    def __call__(self, *, envelope: CallEnvelope, runtime: LayerRuntime) -> CallResult:
        p = dict(envelope.payload or {})
        draft = p.get("script.draft")
        if isinstance(draft, Mapping):
            draft = _draft_from(draft)
        if not isinstance(draft, ScriptDraft):
            return CallResult.fail("F-INPUT", "剧本顾问未收到 ScriptDraft")

        issues: list[ReviewIssue] = []
        hints: list[RepairHint] = []
        missing_total = 0

        for sc in draft.scenes:
            sid = str(sc.get("id", ""))
            flaws = _flaws_of(sc)
            missing_total += len(flaws)
            for key in flaws:
                label = dict((k, l) for k, l, _ in _STRUCTURE_RULES)[key]
                msg = dict((k, m) for k, _, m in _STRUCTURE_RULES)[key]
                issues.append(ReviewIssue(
                    # ⚠️ **`BLOCKER`，不是 `MAJOR`**（2026-10-16 口径）。
                    #
                    # 只有 `BLOCKER` 阻断流程（见
                    # `orchestrator.BLOCKING_SEVERITY`）。而"缺冲突/代价/转折"
                    # 是**该修的缺陷** —— 编剧首稿故意留它，剧评人抓出来，
                    # 返工修好，那是**设计的闭环**。
                    # 定成不阻断会让第 1 轮就放行，闭环形同虚设。
                    #
                    # 判据是**"该修 vs 可以更好"**，不是"确定性 vs 语义"。
                    severity=Severity.BLOCKER,
                    dimension=D.COMPLETENESS,
                    location=IssueLocation(scene=sid),
                    problem=f"{sid} 缺少「{label}」：{msg}",
                    suggestion=f"在 {sid} 补一个体现「{label}」的镜头或段落",
                ))
                hints.append(RepairHint(
                    graph_id=f"G-{draft.episode_id}",
                    op="insert_shot",
                    args={"after": sid, "purpose": label},
                    from_issue=f"issue_{len(issues):03d}",
                ))

        # 评分：结构缺陷越多，完整性越低
        n = max(len(draft.scenes), 1)
        completeness = max(0.0, 100.0 - 100.0 * missing_total / (n * len(_STRUCTURE_RULES)))
        # 一致性：跨场角色是否连贯（此处按"每场是否交代了人物"粗判）
        no_char = sum(1 for sc in draft.scenes if not _characters_of(sc))
        consistency = max(0.0, 100.0 - 40.0 * no_char / n)

        verdict = (Verdict.PASS if not issues
                   else Verdict.REVISE)
        fb = ReviewFeedback(
            frm="dramaturge", to="screenwriter", stage="gate",
            verdict=verdict,
            score=QcScore(dimensions={
                D.COMPLETENESS: round(completeness, 1),
                D.CONSISTENCY: round(consistency, 1),
                D.QUALITY: round(min(95.0, 70.0 + 20.0 * (1 - missing_total / (n * 3))), 1),
                D.FEASIBILITY: 88.0,
                D.COMPLIANCE: 92.0,
            }, phase="draft"),
            issues=tuple(issues),
            repair_hints=tuple(hints),
            reviewer_model="qwen3-max",       # 与编剧的 deepseek 跨族
            generator_model="deepseek-v4",
            round_no=int(draft.revision),
        )
        return CallResult.success({"script.critique": fb})


def _draft_from(d: Mapping[str, Any]) -> ScriptDraft:
    """从字典还原 ScriptDraft（黑板可能存过序列化形式）。"""
    return ScriptDraft(
        frm=str(d.get("from", "screenwriter")), to=str(d.get("to", "*")),
        episode_id=str(d.get("episodeId", "")),
        title=str(d.get("title", "")),
        scenes=tuple(d.get("scenes", ())),
        revision=int(d.get("revision", 1)),
        estimated_seconds=float(d.get("estimatedSeconds", 0.0)),
    )


# ==========================================================================
# 3) 文学策划（看风格与一致性）
# ==========================================================================

class StoryEditorRules:
    """文学策划（规则桩）。**只审别人的稿**，关注风格与用词纪律。"""

    #: 风格纪律：避免的俗套表达
    CLICHES = ("不禁", "缓缓地", "深深地", "默默地", "微微一")

    def __call__(self, *, envelope: CallEnvelope, runtime: LayerRuntime) -> CallResult:
        p = dict(envelope.payload or {})
        draft = p.get("script.draft")
        if isinstance(draft, Mapping):
            draft = _draft_from(draft)
        if not isinstance(draft, ScriptDraft):
            return CallResult.fail("F-INPUT", "文学策划未收到 ScriptDraft")

        issues: list[ReviewIssue] = []
        cliche_hits = 0
        for sc in draft.scenes:
            sid = str(sc.get("id", ""))
            text = str(sc.get("text", ""))
            for c in self.CLICHES:
                if c in text:
                    cliche_hits += 1
                    issues.append(ReviewIssue(
                        severity=Severity.MINOR,
                        dimension=D.QUALITY,
                        location=IssueLocation(scene=sid),
                        problem=f"{sid} 用了俗套表达「{c}」",
                        suggestion=f"把「{c}」换成具体的动作或细节",
                    ))

        n = max(len(draft.scenes), 1)
        quality = max(0.0, 100.0 - 12.0 * cliche_hits)
        # 一致性：场次 id 是否规范
        bad_ids = sum(1 for sc in draft.scenes
                      if not re.match(r"^SC-[\w]+-\d{3}$", str(sc.get("id", ""))))
        consistency = max(0.0, 100.0 - 50.0 * bad_ids / n)

        fb = ReviewFeedback(
            frm="story_editor", to="screenwriter", stage="gate",
            verdict=(Verdict.PASS if not issues else Verdict.REVISE),
            score=QcScore(dimensions={
                D.QUALITY: round(quality, 1),
                D.CONSISTENCY: round(consistency, 1),
                D.COMPLETENESS: 86.0,
                D.FEASIBILITY: 90.0,
                D.COMPLIANCE: 94.0,
            }, phase="draft"),
            issues=tuple(issues),
            repair_hints=tuple(
                RepairHint(graph_id=f"G-{draft.episode_id}", op="adjust_prompt",
                           args={"scene": str(i.location.scene),
                                 "avoid": i.problem.split("「")[-1].rstrip("」")},
                           from_issue=f"issue_{k:03d}")
                for k, i in enumerate(issues, 1)),
            reviewer_model="qwen3-max",
            generator_model="deepseek-v4",
            round_no=int(draft.revision),
        )
        return CallResult.success({"style.guard": fb})


# ==========================================================================
# 4) 分镜师
# ==========================================================================

class StoryboardArtistRules:
    """分镜师（规则桩）。把定稿剧本切成镜头表。

    **规则来自平台硬约束**，不是随手定的：

    | 规则 | 依据 |
    |---|---|
    | 单镜 ≤ 10 秒 | 本机 H3 实测 12 秒必 OOM，10 秒是安全上限 |
    | 一个场次至少 2 个镜头 | 单镜一场无法形成节奏 |
    | 景别在对话场次轮换 | 避免同景别连排导致的视觉疲劳 |
    """

    #: 依据场次长度选景别序列（确定性轮换）
    SIZE_CYCLE = ("full", "medium", "medium_close", "close", "medium_full")
    #: 相机运动模板（与白模渲染的模板名一致）
    MOVE_CYCLE = ("static", "dolly_in", "truck_left", "dolly_out", "static")

    def __call__(self, *, envelope: CallEnvelope, runtime: LayerRuntime) -> CallResult:
        p = dict(envelope.payload or {})
        draft = p.get("script.final") or p.get("script.draft")
        if isinstance(draft, Mapping):
            draft = _draft_from(draft)
        if not isinstance(draft, ScriptDraft):
            return CallResult.fail("F-INPUT", "分镜师未收到定稿剧本")

        shots: list[Shot] = []
        for sc in draft.scenes:
            sid = str(sc.get("id", "SC-000-000"))
            text = str(sc.get("text", "")).strip()
            chars = _characters_of(sc)
            # 按句号/问号/叹号切句 → 每句一镜；至少 2 镜
            sentences = [s for s in re.split(r"[。！？!?]\s*", text) if s.strip()]
            k = max(2, min(len(sentences), 4))
            per = min(MAX_SHOT_SECONDS, 6.0)

            for j in range(k):
                idx = len(shots) + 1
                desc = (sentences[j] if j < len(sentences) else text[:40]) or text[:40]
                shots.append(Shot(
                    frm="storyboard_artist", to="cinematographer", stage="storyboard",
                    shot_id=f"SH-{draft.episode_id}-{sid[-3:]}-{j + 1:03d}",
                    scene_id=sid,
                    shot_size=self.SIZE_CYCLE[(idx - 1) % len(self.SIZE_CYCLE)],
                    camera_movement=self.MOVE_CYCLE[(idx - 1) % len(self.MOVE_CYCLE)],
                    action="walk" if (chars and j == 0) else "stand",
                    duration_sec=per,
                    characters=chars,
                    description=desc.strip()[:120],
                ))

        total = round(sum(s.duration_sec for s in shots), 2)
        out = ShotList(
            frm="storyboard_artist", to="*", stage="storyboard",
            episode_id=draft.episode_id,
            shots=tuple(shots),
            total_seconds=total,
        )
        breakdown = {
            "shotCount": out.shot_count,
            "totalSeconds": total,
            "bySize": {s: sum(1 for x in shots if x.shot_size == s)
                       for s in {x.shot_size for x in shots}},
            "byMove": {m: sum(1 for x in shots if x.camera_movement == m)
                       for m in {x.camera_movement for x in shots}},
            "digest": _digest(out.episode_id, out.shot_count, total),
        }
        return CallResult.success({"shot.list": out, "shot.breakdown": breakdown})


# ==========================================================================
# 5) 审片
# ==========================================================================

class ReviewBoardRules:
    """审片（规则桩）。**跨族盲读**，按 QC-5 五维裁定。"""

    #: 可执行的单镜时长上限
    def __call__(self, *, envelope: CallEnvelope, runtime: LayerRuntime) -> CallResult:
        p = dict(envelope.payload or {})
        sl = p.get("shot.list")
        if isinstance(sl, Mapping):
            sl = ShotList(
                frm="storyboard_artist", to="*",
                episode_id=str(sl.get("episodeId", "")),
                shots=tuple(
                    Shot(frm="storyboard_artist", to="*",
                         shot_id=str(s.get("shotId", "")),
                         scene_id=str(s.get("sceneId", "")),
                         shot_size=str(s.get("shotSize", "medium")),
                         camera_movement=str(s.get("cameraMovement", "static")),
                         action=str(s.get("action", "stand")),
                         duration_sec=float(s.get("durationSec", 5.0)),
                         characters=tuple(s.get("characters", ())),
                         description=str(s.get("description", "")))
                    for s in sl.get("shots", ())),
                total_seconds=float(sl.get("totalSeconds", 0.0)),
            )
        if not isinstance(sl, ShotList):
            return CallResult.fail("F-INPUT", "审片未收到 ShotList")

        issues: list[ReviewIssue] = []
        hints: list[RepairHint] = []
        oversize = [s for s in sl.shots if s.duration_sec > MAX_SHOT_SECONDS]
        for s in oversize:
            issues.append(ReviewIssue(
                severity=Severity.BLOCKER,
                dimension=D.FEASIBILITY,
                location=IssueLocation(scene=s.scene_id, shot=s.shot_id),
                problem=f"{s.shot_id} 时长 {s.duration_sec:.1f}s 超过硬上限 "
                        f"{MAX_SHOT_SECONDS:.0f}s（本机 H3 实测会 OOM）",
                suggestion=f"把 {s.shot_id} 拆成两个镜头",
            ))
            hints.append(RepairHint(
                graph_id=f"G-{sl.episode_id}", op="modify_shot",
                args={"shot": s.shot_id, "durationSec": MAX_SHOT_SECONDS},
                from_issue=f"issue_{len(issues):03d}"))

        empty = [s for s in sl.shots if not s.characters]
        for s in empty[:5]:
            issues.append(ReviewIssue(
                severity=Severity.MAJOR,
                dimension=D.CONSISTENCY,
                location=IssueLocation(scene=s.scene_id, shot=s.shot_id),
                problem=f"{s.shot_id} 未声明出场角色，无法做跨镜一致性核对",
                suggestion=f"为 {s.shot_id} 补 characters 字段",
            ))
            hints.append(RepairHint(
                graph_id=f"G-{sl.episode_id}", op="update_binding",
                args={"shot": s.shot_id, "field": "characters"},
                from_issue=f"issue_{len(issues):03d}"))

        # 同景别连排超过 4 个 → 节奏单调
        run, worst = 1, 1
        for a, b in zip(sl.shots, sl.shots[1:]):
            run = run + 1 if a.shot_size == b.shot_size else 1
            worst = max(worst, run)
        if worst >= 4:
            issues.append(ReviewIssue(
                severity=Severity.MINOR,
                dimension=D.QUALITY,
                location=IssueLocation(scene=sl.shots[0].scene_id),
                problem=f"有 {worst} 个连续同景别镜头，视觉节奏单调",
                suggestion="在连排处插入反打或特写",
            ))

        n = max(sl.shot_count, 1)
        feasibility = max(0.0, 100.0 - 60.0 * len(oversize) / n)
        consistency = max(0.0, 100.0 - 30.0 * len(empty) / n)
        quality = max(0.0, 100.0 - (8.0 if worst >= 4 else 0.0))
        completeness = 95.0 if sl.shot_count >= 2 * max(len({s.scene_id for s in sl.shots}), 1) \
            else 80.0

        has_blocker = any(i.severity is Severity.BLOCKER for i in issues)
        verdict = (Verdict.REVISE if has_blocker or issues else Verdict.PASS)

        fb = ReviewFeedback(
            frm="review_board", to="storyboard_artist", stage="gate",
            verdict=verdict,
            score=QcScore(dimensions={
                D.CONSISTENCY: round(consistency, 1),
                D.COMPLETENESS: round(completeness, 1),
                D.QUALITY: round(quality, 1),
                D.FEASIBILITY: round(feasibility, 1),
                D.COMPLIANCE: 95.0,
            }, phase="final"),
            issues=tuple(issues),
            repair_hints=tuple(hints),
            # **跨族盲读**：审片用 qwen3-vl-max，分镜师用 deepseek-v4
            reviewer_model="qwen3-vl-max",
            generator_model="deepseek-v4",
            round_no=1,
        )
        return CallResult.success({"review.verdict": fb})


# ==========================================================================
# 装配
# ==========================================================================

#: 5 个核心职位的规则桩（agent_id -> 类）
CORE_CREW_STUBS: Mapping[str, type] = {
    "screenwriter": ScreenwriterRules,
    "dramaturge": DramaturgeRules,
    "story_editor": StoryEditorRules,
    "storyboard_artist": StoryboardArtistRules,
    "review_board": ReviewBoardRules,
}


def default_crew_stubs() -> dict[str, Any]:
    """构造一套默认桩实例，供编排器绑定。"""
    return {k: cls() for k, cls in CORE_CREW_STUBS.items()}
