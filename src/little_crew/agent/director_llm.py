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
            '             "focus": "这场必须保住什么"}],\n'
            ' "motifs": ["反复出现的视觉动机"]}\n\n'
            "规则：\n"
            "  · **`scenes` 必须覆盖剧本的每一场**，不能漏场\n"
            "  · **`shotBudget` 之和应等于或接近"
            f" {self.total_shots}** —— 但**单场不少于 2**\n"
            "    （一场只给一个镜头，等于没拍这场）\n"
            "  · `focus` 要有内容：写「少女的犹豫」而不是「情绪」\n"
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
        scenes.append({"sceneId": sid,
                       "shotBudget": max(2, budget),
                       "focus": str(s.get("focus") or "")[:160]})

    # 总和超预算 -> 按比例收回（不低于 2）
    if scenes:
        total = sum(x["shotBudget"] for x in scenes)
        cap = max(2 * len(scenes), int(total_shots))
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
