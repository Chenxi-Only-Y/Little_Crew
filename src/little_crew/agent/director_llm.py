"""**导演**：从剧本产出一份 `episode.plan`（导演阐述）。

## 为什么是"阐述"而不是"又一份场次表"

名册里 `director` 的 `requires` 是**整条链**（`script.final` / `shot.list` /
`shot.image` / `shot.video`）—— 它是**统筹角色**，不是又一个单阶段产出者。
所以它的产物该回答的是"**这一集该怎么拍**"，而不是"有哪些场次"（那是编剧的）。

`episode.plan` 的实际内容：

| 字段 | 用途 |
|---|---|
| `tone` | 基调（一句话）—— 给分镜师定画面气质 |
| `pacing` | 节奏策略（前松后紧 / 匀速 / 倒叙…） |
| `scenes[].shotBudget` | **每场给几个镜头** —— 这是最有用的数字 |
| `scenes[].focus` | 这场要保住什么（情绪落点 / 信息点） |
| `motifs` | 反复出现的视觉动机（物件、光线） |

**`shotBudget` 是最关键的一项**：分镜师现在自己决定每场几个镜头，
而"一场只给一个镜头"或"一场给八个镜头"都是失控。导演先定预算，
分镜在预算内分配 —— 这是"导演"这个职位存在的实际理由。

## 边界

**它不产场次表、不产分镜、不改剧本。** 越界就会和编剧/分镜师抢活，
而那正是"多智能体"退化成"一个模型换四个提示词"的开始。
"""
from __future__ import annotations

from typing import Any, Mapping

from little_crew.agent.llm import LLMError, LlmClient
from little_crew.agent.protocol import Budget, CallEnvelope, CallResult

__all__ = [
    "DEFAULT_TOTAL_SHOTS",
    "DirectorLlm",
    "normalize_plan",
]

#: 一集的目标镜头总数。与 `R11` 实测的 20~24 镜一致。
DEFAULT_TOTAL_SHOTS: int = 20

_SYSTEM = (
    "你是一位导演，负责把一集剧本定成可执行的拍摄方案。"
    "你**不做**场次表（那是编剧），**不做**分镜（那是分镜师）。"
    "你决定的是：基调、节奏、每场给几个镜头、这场必须保住什么。"
)


class DirectorLlm:
    """把 `director` 实现为真实 LLM 职位。

    `impl(*, envelope, runtime) -> CallResult` —— 与其他职位同一契约。
    """

    agent_id = "director"
    capability = "episode.direct"
    #: **产物要落在名册声明的 `produces` 键下**（`episode.plan`），
    #: 不是 capability 名（`episode.direct`）。
    #: ⚠️ 两者不同时用 capability 名会"调用成功但产物取不到" —— 实测踩过。
    produces_key = "episode.plan"
    artifact_key = "episode.plan"
    #: 它消费的是剧本，不是自己的产物
    consumes_key = "script.draft"

    def __init__(self, *, client: LlmClient | None = None,
                 total_shots: int = DEFAULT_TOTAL_SHOTS,
                 temperature: float = 0.4) -> None:
        self.client = client or LlmClient("deepseek-flash")
        self.total_shots = max(1, int(total_shots))
        self.temperature = temperature

    # ---- 提示词 --------------------------------------------------------

    def _render(self, payload: Mapping[str, Any]) -> tuple[str, str]:
        art = payload.get(self.consumes_key)
        body = art if isinstance(art, str) else _as_text(art)
        prompt = (
            f"## 剧本\n{body or '（空）'}\n\n"
            f"## 任务\n"
            f"这一集预计 **{self.total_shots} 个镜头**。请给出导演阐述。\n\n"
            "严格输出 JSON：\n"
            '{"tone": "基调，一句话",\n'
            ' "pacing": "节奏策略，一句话",\n'
            ' "scenes": [{"sceneId": "S01", "shotBudget": 4,\n'
            '             "role": "高潮|铺垫|抒情|过场",\n'
            '             "priority": 5,\n'
            '             "avgTarget": "偏短|中性|偏长",\n'
            '             "allowLong": false,\n'
            '             "sceneSeconds": 24,\n'
            '             "focus": "这场必须保住什么"}],\n'
            ' "motifs": ["反复出现的视觉动机"]}\n\n'
            "规则：\n"
            "  · **`scenes` 必须覆盖剧本的每一场**，不能漏场\n"
            "  · **`shotBudget` 之和应等于或接近"
            f" {self.total_shots}** —— 但**单场不少于 2**\n"
            "    （一场只给一个镜头，等于没拍这场）\n"
            "  · `focus` 要有内容：写「少女的犹豫」而不是「情绪」\n"
            "  · **场级节奏必须逐场不同** —— 这不是装饰：\n"
            "      `role` 决定这场是什么作用（高潮/铺垫/抒情/过场）\n"
            "      `priority` 1~5，**5 最重要**（1 是最低，别写反）\n"
            "      `avgTarget` 该场镜头偏短（碎切/紧张）还是偏长（长镜头/留白）\n"
            "      `allowLong` 是否允许超过 8 秒的长镜头\n"
            "      `sceneSeconds` 这场总时长上限（秒），**高潮/重场给大，过场给小**\n"
            "  · ⚠️ **不许每场都一样** —— 全片场场 2 镜 12 秒等于没有节奏\n"
            "  · **不要写分镜、不要写景别、不要改剧情**"
        )
        return prompt, _SYSTEM

    # ---- 调用 ----------------------------------------------------------

    def __call__(self, *, envelope: CallEnvelope,
                 runtime: Any = None) -> CallResult:
        payload = dict(envelope.payload or {})
        if not payload.get(self.consumes_key):
            return CallResult.fail(
                "F-INPUT", f"director 需要 payload[{self.consumes_key!r}]")
        text = (payload[self.consumes_key]
                if isinstance(payload[self.consumes_key], str)
                else _as_text(payload[self.consumes_key]))
        if not text.strip():
            return CallResult.fail("F-INPUT", "剧本正文为空 —— 无法定方案")
        try:
            prompt, system = self._render(payload)
        except Exception as e:                                # noqa: BLE001
            return CallResult.fail("F-RENDER", f"提示词构造失败: {e}")

        try:
            data = self.client.chat_json(prompt, system=system,
                                         what="导演阐述")
        except LLMError as e:
            return CallResult.fail(f"F-LLM-{e.kind}",
                                   f"director 调用失败: {e}",
                                   escalate=not e.retryable)
        except Exception as e:                                # noqa: BLE001
            return CallResult.fail("F-LLM", f"director 解析失败: {e}")

        plan = normalize_plan(data, total_shots=self.total_shots)
        return CallResult.success({self.produces_key: plan})


# ---- 规整 --------------------------------------------------------------

#: 场级节奏的**枚举**与**按角色的默认值**。
#:
#: 2026-11-06：加了这些字段之后，导演才真正参与"节奏设计" ——
#: 在此之前每场只有一个 `shotBudget`，于是配额算出来场场相同，
#: 盲判里 B 组的"每场镜数方差"只有 A 的一半（0.35 vs 0.70），判者读作"平"。
ROLE_CHOICES = ("高潮", "铺垫", "抒情", "过场")
AVG_CHOICES = ("偏短", "中性", "偏长")
#: role -> (avgTarget, allowLong, priority, sceneSeconds 系数)
_ROLE_DEFAULTS: dict[str, tuple[str, bool, int, float]] = {
    "高潮": ("偏短", False, 5, 1.2),
    "铺垫": ("中性", False, 3, 1.0),
    "抒情": ("偏长", True, 3, 1.0),
    "过场": ("偏短", False, 1, 0.8),
}


def _clean_role(v: Any) -> str:
    s = str(v or "").strip()
    return s if s in ROLE_CHOICES else ""


def _clean_avg(v: Any) -> str:
    s = str(v or "").strip()
    return s if s in AVG_CHOICES else ""


def normalize_plan(data: Mapping[str, Any], *,
                   total_shots: int = DEFAULT_TOTAL_SHOTS) -> dict[str, Any]:
    """把模型输出规整成**可下游消费**的 plan。

    三条硬规矩（都在测试里钉住）：

    1. **`shotBudget` 单场不少于 2** —— 一场只给一个镜头，等于没拍这场。
       模型经常这么干（尤其场次多时），所以要提上来。
    2. **总和超出预算时按比例收回**，但不把任何一场压到 1。
    3. **`sceneId` 必须保留** —— 下游要靠它把预算对到场次上。
    """
    raw_scenes = data.get("scenes")
    scenes: list[dict[str, Any]] = []
    for s in (raw_scenes if isinstance(raw_scenes, list) else ()):
        if not isinstance(s, Mapping):
            continue
        sid = str(s.get("sceneId") or s.get("scene_id") or "").strip()
        if not sid:
            continue                      # 没场次号的预算无法对账
        try:
            budget = int(s.get("shotBudget") or s.get("shots") or 0)
        except (TypeError, ValueError):
            budget = 0
        # ---- 场级节奏字段（2026-11-06）----
        # 缺省时按 role 推；role 也没有就用"铺垫"的中性默认。
        # **非法值不猜**：清成空串，交给下游 fallback 并如实记录。
        role = _clean_role(s.get("role"))
        avg = _clean_avg(s.get("avgTarget"))
        allow = s.get("allowLong")
        allow_long = None if allow is None else bool(allow)
        try:
            prio = int(s.get("priority") or 0)
        except (TypeError, ValueError):
            prio = 0
        try:
            secs = float(s.get("sceneSeconds") or 0)
        except (TypeError, ValueError):
            secs = 0.0

        _d_avg, _d_long, _d_prio, _d_k = _ROLE_DEFAULTS.get(role or "铺垫")
        if not role:
            role = ""
        if allow_long is None:
            allow_long = _d_long
        if not (1 <= prio <= 5):
            prio = _d_prio
        if secs <= 0:
            secs = round(max(8.0, min(45.0, max(2, budget) * 6.0 * _d_k)), 1)
        else:
            secs = round(max(8.0, min(45.0, secs)), 1)

        scenes.append({"sceneId": sid,
                       "shotBudget": max(2, budget),
                       "role": role,
                       "priority": prio,
                       "avgTarget": avg or _d_avg,
                       "allowLong": bool(allow_long),
                       "sceneSeconds": secs,
                       "focus": str(s.get("focus") or "")[:160]})

    # 总和超预算 -> 按比例收回（不低于 2）
    if scenes:
        total = sum(x["shotBudget"] for x in scenes)
        # ⚠️ **同时受「每场 4 镜」约束**（2026-11-02 修）。
        #
        # 原来只有 `total_shots`（默认 20）这一个上限：
        #   2 场剧本 -> max(4, 20) = 20 -> 永不收缩
        # 结果 2 场的小剧本也被切成 20 镜。盲判（30 组×2，人读节奏）实测：
        #   短剧本 A 组 7 镜、B 组 20.7 镜 -> 节奏分 5.00 vs 3.53（−1.47）
        # 也就是说：**预算没考虑剧本长度，把短剧本压成了呆板的等长切分。**
        #
        # 现在取两者较小：2 场 -> 8 · 4 场 -> 16 · 6 场 -> 20（与原一致）
        # 每场预算随**场数递减**（2026-11-03 第二修）。
        #
        # 依据：盲判 ≤4 分样本（n=14）反查 ——
        #   低分：5.4 场 / 17.9 镜    满分：3.9 场 / 13.5 镜
        #   而"相邻同景别率 4% vs 2%""碎切 0% vs 0%"几乎无差别 ——
        #   **所以节奏差的主因不是镜头技法，是场多、镜密。**
        #
        # 固定 4 镜/场 在 2~3 场合适，5 场以上就偏多（每场内容量反而更小）。
        # 改成 6 - 0.5×场数：2场→5 · 4场→4 · 6场→3 · 8场→2（下限 2）
        _per = max(2.0, 6.0 - 0.5 * len(scenes))
        cap = max(2 * len(scenes), min(int(total_shots), int(round(_per * len(scenes)))))
        if total > cap and total > 0:
            for x in scenes:
                x["shotBudget"] = max(2, int(x["shotBudget"] * cap / total))

    motifs = [str(m)[:60] for m in (data.get("motifs") or ())
              if str(m).strip()][:6]
    return {
        "tone": str(data.get("tone") or "")[:200],
        "pacing": str(data.get("pacing") or "")[:200],
        "scenes": scenes,
        "motifs": motifs,
        # 便于报告直接读
        "totalShotBudget": sum(x["shotBudget"] for x in scenes),
    }


def _as_text(art: Any) -> str:
    """把剧本对象摊成文本。**优先用项目已有的 `script_text()`。**"""
    try:
        from little_crew.agent.character_sheet import script_text
        got = script_text(art)
        if isinstance(got, str) and got.strip():
            return got
    except Exception:                                         # noqa: BLE001
        pass
    for attr in ("as_text", "to_text", "text"):
        v = getattr(art, attr, None)
        if isinstance(v, str) and v.strip():
            return v
        if callable(v):
            try:
                got = v()
                if isinstance(got, str) and got.strip():
                    return got
            except Exception:                                 # noqa: BLE001
                pass
    return str(art or "")
