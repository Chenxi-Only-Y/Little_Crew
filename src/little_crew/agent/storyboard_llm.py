"""分镜师 —— 真实 LLM 实现。

为什么这个职位特别适合 LLM
--------------------------
"把一场戏切成镜头"本来就是**创作判断**：哪句该给特写、哪里该换机位、
节奏怎么走。规则桩只能按标点切句 + 轮换景别，出来的镜头表**语法对、
语意平**。

⚠️ 但 LLM **不能信任它守住硬约束**
------------------------------------
单镜 ≤ 10 秒、一场至少 2 镜这类限制来自**平台能力**（本机 H3 实测 12 秒必 OOM），
不是创作偏好。模型完全可能排出 15 秒的镜头 —— 它不知道我们的显存有多大。

所以本实现的分工是：

| 谁 | 负责什么 |
|---|---|
| **LLM** | 镜头怎么切、景别怎么选、画面描述写什么（**创作**） |
| **代码** | 时长上限、场次下限、取值合法性（**约束**） |

**校验失败时如实失败并说明原因，不静默夹紧。** 静默把 15 秒改成 10 秒
会让"提示词没守约束"这件事永远看不见 —— 而那正是要修的东西。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from little_crew.agent.llm import LLMError, LlmClient
from little_crew.agent.messages import (
    MAX_SHOT_SECONDS,
    Shot,
    ShotList,
)

__all__ = ["StoryboardArtistLlm", "STORYBOARD_SYSTEM", "LEGAL_VOCAB"]


def _legal_vocab() -> dict[str, tuple[str, ...]]:
    """合法取值 —— **从数据契约推导，不手写**。

    ⚠️ 手写会有第二份真相。三个词表的来源都在**本层或更低层**：
    景别与机位来自分镜师的轮换表，动作来自 `messages.PREVIS_ACTIONS`
    （`Shot.action` 的合法取值定义）。

    ⚠️ 踩过：`PREVIS_ACTIONS` 原本只在 `runtime/episode.py` 里，
    而 `agent`(层 6) 不能 import `runtime`(层 8) —— 守卫报反向依赖。
    已把权威表**下移到 `messages.py`**（它定义的就是 `Shot.action` 的取值），
    `episode.py` 改为从那里引用。
    """
    from little_crew.agent.crew_stubs import StoryboardArtistRules
    from little_crew.agent.messages import PREVIS_ACTIONS

    # 用 dict.fromkeys 去重并**保序**：轮换表里有重复项
    # （`MOVE_CYCLE` 的 `static` 出现两次），直接喂给提示词会让
    # 模型看到 `['static', ..., 'static']` —— 显得像我们搞错了。
    def uniq(seq: Any) -> tuple[str, ...]:
        return tuple(dict.fromkeys(str(x) for x in seq))

    return {
        "shotSize": uniq(StoryboardArtistRules.SIZE_CYCLE),
        "cameraMovement": uniq(StoryboardArtistRules.MOVE_CYCLE),
        "action": uniq(PREVIS_ACTIONS),
    }


LEGAL_VOCAB = _legal_vocab()


def _system_prompt() -> str:
    v = LEGAL_VOCAB
    return f"""你是《小剧组》平台的**分镜师**，负责把定稿剧本切成镜头表。

## 硬约束（来自平台能力，**不是偏好**，违反会被驳回）

· **每个镜头时长 ≤ {MAX_SHOT_SECONDS:.0f} 秒** —— 本机视频模型超过它就显存溢出。
· **每个场次至少 2 个镜头** —— 单镜一场形不成节奏。
· `shotSize` 只能取：{", ".join(v["shotSize"])}
· `cameraMovement` 只能取：{", ".join(v["cameraMovement"])}
· `action` 只能取：{", ".join(v["action"])}
· 同一场次内**不要连排相同景别** —— 视觉会疲劳。

## 你的创作判断

· 对白密集处用 `medium` / `medium_close`，情绪高点用 `close`。
· 交代环境用 `full` / `medium_full`。
· 关键动作给 `dolly_in` 或 `truck_left`；静场用 `static`。
· `description` 是**给文生图模型的画面描述**：写清景别、光线、人物动作、
  环境细节。**不要写镜头术语**（那些已经由字段表达了），要写**看得见的东西**。
· 人物名必须与剧本里出现的一致。

## 输出（严格 JSON，不要任何其它内容）

{{
  "shots": [
    {{
      "sceneId": "S01",
      "shotSize": "medium",
      "cameraMovement": "static",
      "action": "stand",
      "durationSec": 5.0,
      "characters": ["林晚"],
      "description": "中景。旧书店内，暖黄灯光从柜台后的小灯照出……"
    }}
  ]
}}"""


STORYBOARD_SYSTEM = _system_prompt()


class StoryboardArtistLlm:
    """分镜师 —— 真实 LLM 实现。

    与规则桩同一契约：收 `script.final` / `script.draft`，
    产出 `{"shot.list": ShotList, "shot.breakdown": {...}}`。
    """

    agent_id = "storyboard_artist"

    def __init__(self, *, client: LlmClient | None = None,
                 temperature: float = 0.6, max_shots: int = 40,
                 plan: Mapping[str, Any] | None = None,
                 lighting: Mapping[str, Any] | None = None) -> None:
        self.client = client or LlmClient("deepseek-v4")
        self.temperature = temperature
        self.max_shots = max_shots
        #: **导演方案**（`episode.plan`）。给了就按它的 `shotBudget` 分配镜头。
        #:
        #: 为什么不给也算正常：分镜师可以独立工作。但**给了就要遵守** ——
        #: 否则"导演"这个职位只是产了一份没人看的文档。
        self.plan = dict(plan or {})
        #: **灯光方案**（`lighting.plan`）。给了就把每场的光写进提示词。
        #:
        #: 为什么光能进提示词而"氛围"不能：见 `gaffer_llm` 的模块说明 ——
        #: 「窗在左侧，硬光斜切」是可执行的，「压抑感」不是。
        self.lighting = dict(lighting or {})
        #: 上一轮的预算偏差（测试与证据用）
        self.budget_deviations: list[str] = []
        self.calls = 0
        #: 上一轮的驳回原因（测试与证据用）
        self.rejections: list[str] = []

    # ---- 主入口 --------------------------------------------------------

    def __call__(self, *, envelope: Any, runtime: Any) -> Any:
        from little_crew.agent.protocol import CallResult

        self.calls += 1
        p = dict(envelope.payload or {})
        draft = _as_draft(p.get("script.final") or p.get("script.draft"))
        if draft is None:
            return CallResult.fail("F-INPUT", "分镜师未收到定稿剧本")

        prompt = self._build_prompt(draft)
        try:
            data = self.client.chat_json(
                prompt, system=STORYBOARD_SYSTEM,
                temperature=self.temperature, what="分镜表")
        except LLMError as e:
            return CallResult.fail(f"F-LLM-{e.kind.upper()}",
                                   f"分镜师调用失败：{e}",
                                   escalate=not e.retryable)

        shots, problems = self._to_shots(data, draft)
        if problems:
            # ⚠️ **不静默夹紧。** 把违规明说出来 —— 静默修正会让
            # "提示词没守约束"永远看不见，而那正是要修的东西。
            self.rejections.append("；".join(problems[:6]))
            return CallResult.fail(
                "F-CONSTRAINT-VIOLATED",
                "分镜违反平台硬约束：" + "；".join(problems[:6]),
            )
        if not shots:
            return CallResult.fail(
                "F-OUTPUT-EMPTY",
                f"模型返回了合法 JSON 但没有可用镜头"
                f"（前 200 字：{str(data)[:200]}）")

        # ---- 预算偏差（**记录但不阻断**）----
        #
        # ⚠️ **为什么不直接拒稿**：导演预算是一个"意图"，
        # 而分镜受硬约束（每镜 ≤10 秒、每场 ≥2 镜）挤压 ——
        # 某场稍多或稍少是正常的。
        # 直接驳回会让"导演"变成第二个门禁，而它没有标定过。
        #
        # 所以：**记下来，让报告看得见**。等量出偏差分布，再决定要不要拦。
        # （与 `PASS_THRESHOLD` / 顾问批评家同一套纪律：先有分布，再有权重。）
        self.budget_deviations = []
        budgets = {}
        for s in (self.plan.get("scenes") or ()):
            sid = str(s.get("sceneId") or "")
            if sid:
                budgets[sid] = int(s.get("shotBudget") or 0)
        if budgets:
            actual: dict[str, int] = {}
            for sh in shots:
                actual[str(getattr(sh, "scene_id", ""))] = (
                    actual.get(str(getattr(sh, "scene_id", "")), 0) + 1)
            for sid, want in budgets.items():
                got = actual.get(sid, 0)
                if want > 0 and got != want:
                    self.budget_deviations.append(
                        f"{sid}: 预算 {want} 镜，实给 {got} 镜"
                        f"（{'超' if got > want else '少'} {abs(got - want)}）")

        out = ShotList(
            frm="storyboard_artist", to="*", stage="storyboard",
            episode_id=draft.episode_id,
            shots=tuple(shots),
            total_seconds=round(sum(s.duration_sec for s in shots), 2),
        )
        return CallResult.success(
            {"shot.list": out, "shot.breakdown": _breakdown(out, shots)},
            evidence=[{
                "kind": "llm-call",
                "agent": self.agent_id,
                "shotCount": len(shots),
                "sceneCount": len(draft.scenes),
                "budgetDeviations": list(self.budget_deviations),
                "hasPlan": bool(self.plan),
                "servedModel": (self.client.call_log[-1].get("servedModel")
                                if self.client.call_log else ""),
                "usage": (self.client.call_log[-1].get("usage")
                          if self.client.call_log else {}),
            }],
        )

    # ---- 内部 ----------------------------------------------------------

    def _build_prompt(self, draft: Any) -> str:
        v = LEGAL_VOCAB
        lines = [f"【本集】{getattr(draft, 'episode_id', '')} "
                 f"《{getattr(draft, 'title', '')}》",
                 f"【共 {len(draft.scenes)} 场 —— 每场至少 2 镜】", ""]
        for sc in draft.scenes:
            sid = str(sc.get("scene_id") or sc.get("id") or "?")
            cast = "、".join(str(c) for c in (sc.get("characters") or ()))
            lines += [f"═══ {sid}　{sc.get('heading', '')}",
                      f"人物：{cast or '（未标）'}",
                      str(sc.get("text", "")), ""]
        # ---- 导演预算（若给了）----
        budget_lines: list[str] = []
        budgets = {}
        for s in (self.plan.get("scenes") or ()):
            sid = str(s.get("sceneId") or "")
            if sid:
                budgets[sid] = s
        if budgets:
            budget_lines.append("【导演给的镜头预算 —— 必须遵守】")
            for sc in draft.scenes:
                sid = str(sc.get("scene_id") or sc.get("id") or "?")
                b = budgets.get(sid)
                if b:
                    budget_lines.append(
                        f"  {sid}: **{b.get('shotBudget')} 镜**"
                        + (f"　保住：{b.get('focus')}" if b.get("focus") else ""))
                else:
                    budget_lines.append(f"  {sid}: （导演未给预算，按 ≥2 镜）")
            if self.plan.get("tone"):
                budget_lines.append(f"  基调：{self.plan['tone']}")
            if self.plan.get("pacing"):
                budget_lines.append(f"  节奏：{self.plan['pacing']}")
            if self.plan.get("motifs"):
                budget_lines.append(
                    f"  视觉动机（尽量体现）：{'、'.join(self.plan['motifs'])}")
            budget_lines.append("")

        lines += budget_lines

        # ---- 灯光方案（若给了）----
        lights = {}
        for s in (self.lighting.get("scenes") or ()):
            sid = str(s.get("sceneId") or "")
            if sid:
                lights[sid] = s
        if lights:
            lines.append("【灯光方案 —— 画面描述要体现】")
            for sc in draft.scenes:
                sid = str(sc.get("scene_id") or sc.get("id") or "?")
                L = lights.get(sid)
                if not L:
                    continue
                bits = [x for x in (L.get("timeOfDay"), L.get("keyLight"),
                                    L.get("colorTemp")) if x]
                lines.append(f"  {sid}: {'；'.join(bits)}"
                             f"　影调 {L.get('contrast')}"
                             + (f"　{L.get('note')}" if L.get("note") else ""))
            if self.lighting.get("palette"):
                lines.append(f"  主色板：{'、'.join(self.lighting['palette'])}")
            lines.append("")

        lines += [
            f"请为以上 {len(draft.scenes)} 场切分镜。总镜头数不超过 "
            f"{self.max_shots}。",
            f"⚠️ 每镜 ≤ {MAX_SHOT_SECONDS:.0f} 秒，每场 ≥ 2 镜。",
            f"⚠️ shotSize 只能取 {list(v['shotSize'])}；",
            f"   cameraMovement 只能取 {list(v['cameraMovement'])}；",
            f"   action 只能取 {list(v['action'])}。",
            "输出 JSON，见系统提示给出的结构。",
        ]
        return "\n".join(lines)

    def _to_shots(self, data: Mapping[str, Any],
                  draft: Any) -> tuple[list[Shot], list[str]]:
        """转成 `Shot` 并**逐条校验硬约束**。返回 (镜头, 问题列表)。"""
        v = LEGAL_VOCAB
        known_scenes = [str(sc.get("scene_id") or sc.get("id") or "")
                        for sc in draft.scenes]
        known_set = set(known_scenes)
        raw = data.get("shots")
        if not isinstance(raw, (list, tuple)):
            return [], [f"`shots` 不是数组（拿到 {type(raw).__name__}）"]

        shots: list[Shot] = []
        problems: list[str] = []
        per_scene: dict[str, int] = {}

        for idx, s in enumerate(raw[:self.max_shots], 1):
            if not isinstance(s, Mapping):
                problems.append(f"第 {idx} 项不是对象")
                continue
            sid = str(s.get("sceneId") or s.get("scene_id") or "")
            if sid not in known_set:
                problems.append(f"第 {idx} 项的 sceneId={sid!r} 不在剧本里")
                continue

            size = str(s.get("shotSize") or s.get("shot_size") or "medium")
            if size not in v["shotSize"]:
                problems.append(f"{sid} 第 {idx} 项 shotSize={size!r} 非法")
                continue
            move = str(s.get("cameraMovement") or s.get("camera_movement")
                       or "static")
            if move not in v["cameraMovement"]:
                problems.append(f"{sid} 第 {idx} 项 cameraMovement={move!r} 非法")
                continue
            action = str(s.get("action") or "stand")
            if action not in v["action"]:
                problems.append(f"{sid} 第 {idx} 项 action={action!r} 非法")
                continue

            try:
                dur = float(s.get("durationSec") or s.get("duration_sec") or 5.0)
            except (TypeError, ValueError):
                problems.append(f"{sid} 第 {idx} 项 durationSec 不是数字")
                continue
            if dur <= 0:
                problems.append(f"{sid} 第 {idx} 项 durationSec={dur} 非正")
                continue
            if dur > MAX_SHOT_SECONDS:
                problems.append(
                    f"{sid} 第 {idx} 项时长 {dur}s 超过 {MAX_SHOT_SECONDS:.0f}s 上限")
                continue

            desc = str(s.get("description") or "").strip()
            if not desc:
                problems.append(f"{sid} 第 {idx} 项缺 description（画面描述是出图依据）")
                continue

            chars = s.get("characters") or []
            if isinstance(chars, str):
                chars = [c.strip() for c in chars.replace("，", ",").split(",")
                         if c.strip()]

            per_scene[sid] = per_scene.get(sid, 0) + 1
            n = per_scene[sid]
            shots.append(Shot(
                frm="storyboard_artist", to="cinematographer",
                stage="storyboard",
                shot_id=f"SH-{getattr(draft, 'episode_id', '')}-"
                        f"{sid[-3:]}-{n:03d}",
                scene_id=sid,
                shot_size=size,
                camera_movement=move,
                action=action,
                duration_sec=round(dur, 2),
                characters=tuple(str(c) for c in chars),
                description=desc[:200],
            ))

        # 场次下限：**逐场检查**，不只看总数
        for sid in known_scenes:
            got = per_scene.get(sid, 0)
            if got < 2:
                problems.append(f"{sid} 只有 {got} 个镜头（下限 2）")
        return shots, problems


def _breakdown(out: ShotList, shots: Sequence[Shot]) -> dict[str, Any]:
    """与规则桩同口径的统计 —— 下游（与测试）依赖这些键。"""
    from little_crew.agent.crew_stubs import _digest

    return {
        "shotCount": out.shot_count,
        "totalSeconds": out.total_seconds,
        "bySize": {s: sum(1 for x in shots if x.shot_size == s)
                   for s in {x.shot_size for x in shots}},
        "byMove": {m: sum(1 for x in shots if x.camera_movement == m)
                   for m in {x.camera_movement for x in shots}},
        "digest": _digest(out.episode_id, out.shot_count, out.total_seconds),
    }


def _as_draft(x: Any) -> Any:
    """宽容还原 `ScriptDraft`（黑板可能存过序列化形式）。"""
    from little_crew.agent.crew_llm import _as_draft as _d

    return _d(x)
