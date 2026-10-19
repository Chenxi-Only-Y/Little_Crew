"""**制片主任**：从镜头表产出 `production.schedule`（排期与预算）。

## 为什么它必须**在渲染之前**跑

它给的是「**这片要花多少时间/多少钱**」——一个**做之前就该知道的数**。
放到渲染后跑，就变成了"事后记账"，而记账不改变任何决策。

## 它用的是**实测单价**，不是模型猜的

这是本职位最容易做错的地方：让模型凭空估"一镜多少秒"，它会给出
"约 2 分钟"这类看着合理但与本机无关的数字。

所以：**单镜耗时由调用方传入**（来自实测），模型只负责
「**分配与风险判断**」—— 哪些镜头会超支、哪些可以合并渲染、整体风险在哪。

| 谁给 | 给什么 |
|---|---|
| 调用方（实测） | 每镜基准秒数 / 单价 |
| 模型（本职位） | 风险标注、顺序建议、总量汇总的**文字判断** |

## 边界

**不写分镜、不改镜头、不排剧情。** 只回答"怎么排、哪里会出问题"。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from little_crew.agent.llm import LLMError, LlmClient
from little_crew.agent.protocol import Budget, CallEnvelope, CallResult

__all__ = [
    "DEFAULT_SECONDS_PER_SHOT",
    "LineProducerLlm",
    "normalize_schedule",
]

#: 本机实测单镜视频耗时（秒）。`alpha.75` 整集 6308.3s / 20 镜 = 315.4s。
DEFAULT_SECONDS_PER_SHOT: float = 315.4
#: 本机实测单镜静帧耗时（秒）。
DEFAULT_SECONDS_PER_STILL: float = 16.1

_SYSTEM = (
    "你是一位制片主任。你只负责**排期与风险**。"
    "你**不改分镜**、**不改剧情**、**不估模型速度**（速度由实测数据给出）。"
    "你的价值在于判断：哪些镜头会超支、顺序怎么排更稳、整体风险在哪。"
)


class LineProducerLlm:
    """把 `line_producer` 实现为真实 LLM 职位。"""

    agent_id = "line_producer"
    capability = "production.schedule"
    produces_key = "production.schedule"
    #: 它消费**镜头表**，不是剧本
    consumes_key = "shot.list"

    def __init__(self, *, client: LlmClient | None = None,
                 seconds_per_shot: float = DEFAULT_SECONDS_PER_SHOT,
                 seconds_per_still: float = DEFAULT_SECONDS_PER_STILL,
                 cny_per_shot: float = 0.0,
                 temperature: float = 0.2) -> None:
        self.client = client or LlmClient("deepseek-flash")
        self.seconds_per_shot = float(seconds_per_shot)
        self.seconds_per_still = float(seconds_per_still)
        self.cny_per_shot = float(cny_per_shot)
        self.temperature = temperature

    def _shots(self, art: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        shots = getattr(art, "shots", None)
        if shots is None and isinstance(art, (list, tuple)):
            shots = art
        for s in (shots or ()):
            d = s.as_dict() if hasattr(s, "as_dict") else dict(s or {})
            rows.append({"shotId": str(d.get("shot_id") or d.get("shotId") or ""),
                         "sceneId": str(d.get("scene_id") or d.get("sceneId") or ""),
                         "shotSize": str(d.get("shot_size") or d.get("shotSize") or ""),
                         "durationSec": float(d.get("duration_sec")
                                              or d.get("durationSec") or 0)})
        return rows

    def _render(self, rows: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
        listing = "\n".join(
            f"  {r['shotId']}  场{r['sceneId']}  {r['shotSize']}  {r['durationSec']}s"
            for r in rows)
        n = len(rows)
        prompt = (
            f"## 镜头表（{n} 镜）\n{listing}\n\n"
            f"## **实测**基准（不要自己估，直接用）\n"
            f"  · 每镜静帧 {self.seconds_per_still:.1f} 秒\n"
            f"  · 每镜视频 {self.seconds_per_shot:.1f} 秒\n"
            f"  · 合计约 {n * (self.seconds_per_still + self.seconds_per_shot) / 3600:.2f} 小时\n\n"
            "## 任务\n给出排期与风险判断。\n\n"
            "严格输出 JSON：\n"
            '{"totalSeconds": 数字,\n'
            ' "riskShots": [{"shotId": "…", "why": "为什么会超支"}],\n'
            ' "advice": ["可执行的排期建议，2~4 条"],\n'
            ' "note": "一句话总体判断"}\n\n'
            "规则：\n"
            "  · **`totalSeconds` 必须用上面给的基准算**，不要另估\n"
            "  · `riskShots` 只列**真的会超支**的（如人物多、动作复杂、"
            "同场多镜重复），不要凑数\n"
            "  · `advice` 要能执行：写「把第 3 场的 4 镜连着跑，避免换场重载」"
            "而不是「注意时间管理」\n"
            "  · **不要改镜头、不要改剧情**"
        )
        return prompt, _SYSTEM

    def __call__(self, *, envelope: CallEnvelope,
                 runtime: Any = None) -> CallResult:
        payload = dict(envelope.payload or {})
        art = payload.get(self.consumes_key)
        if art is None:
            return CallResult.fail(
                "F-INPUT", f"line_producer 需要 payload[{self.consumes_key!r}]")
        rows = self._shots(art)
        if not rows:
            return CallResult.fail("F-INPUT", "镜头表为空 —— 无法排期")
        try:
            prompt, system = self._render(rows)
        except Exception as e:                                # noqa: BLE001
            return CallResult.fail("F-RENDER", f"提示词构造失败: {e}")
        try:
            data = self.client.chat_json(prompt, system=system,
                                         what="排期")
        except LLMError as e:
            return CallResult.fail(f"F-LLM-{e.kind}",
                                   f"line_producer 调用失败: {e}",
                                   escalate=not e.retryable)
        except Exception as e:                                # noqa: BLE001
            return CallResult.fail("F-LLM", f"line_producer 解析失败: {e}")
        sched = normalize_schedule(
            data, shots=rows, seconds_per_shot=self.seconds_per_shot,
            seconds_per_still=self.seconds_per_still,
            cny_per_shot=self.cny_per_shot)
        return CallResult.success({self.produces_key: sched})


def normalize_schedule(data: Mapping[str, Any], *, shots: Sequence[Mapping[str, Any]],
                       seconds_per_shot: float = DEFAULT_SECONDS_PER_SHOT,
                       seconds_per_still: float = DEFAULT_SECONDS_PER_STILL,
                       cny_per_shot: float = 0.0) -> dict[str, Any]:
    """规整排期。

    ⚠️ **`totalSeconds` 由代码算，不取模型自报。**

    理由与"分数由代码算"（`§3.4.3`）一致：模型对"多长时间"没有标定，
    而我们有**实测单价**。让它算总量等于把一年前的经验值换成一个猜测。
    """
    n = len(shots)
    computed = round(n * (seconds_per_shot + seconds_per_still), 1)
    known = {str(s.get("shotId") or "") for s in shots}
    risks = []
    for r in (data.get("riskShots") or ()):
        if not isinstance(r, Mapping):
            continue
        sid = str(r.get("shotId") or "").strip()
        # 只接受**镜头表里真实存在**的 shotId —— 模型会编出不存在的镜头
        if sid and sid in known:
            risks.append({"shotId": sid, "why": str(r.get("why") or "")[:140]})
    return {
        "shots": n,
        "totalSeconds": computed,
        "totalHours": round(computed / 3600, 2),
        "totalCny": round(n * cny_per_shot, 4),
        "riskShots": risks,
        "advice": [str(a)[:140] for a in (data.get("advice") or ())
                   if str(a).strip()][:5],
        "note": str(data.get("note") or "")[:160],
        # 模型自报的总量**留档但不用** —— 便于日后核对它与实测的偏差
        "modelClaimedSeconds": (data.get("totalSeconds")
                                if isinstance(data.get("totalSeconds"), (int, float))
                                else None),
    }
