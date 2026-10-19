"""**预演师**（LLM）：从分镜判断每镜的空间布局，供 Blender 出白模。

## 它到底决定什么

| 参数 | 谁定 |
|---|---|
| `shot_size` / `duration_sec` / `camera_movement` | **分镜师**（已有），这里只搬 |
| `lens_mm` | **查表**（景别→镜头，摄影常识，不需要判断） |
| **`azimuth_deg` / `angle_deg`** | **本职位判断** —— 在哪一侧、多高 |
| **`subjects` 的 `x` / `z`** | **本职位判断** —— 谁在左、谁更近 |
| **`reason`** | **本职位必须给** —— 一句可被人反驳的空间理由 |

**它不重新决定景别与运镜** —— 那是分镜师的活。

## 为什么必须带 `reason`

> **可反驳的判断才是判断**，不可反驳的只是一串数字。

评审能说「不对，这场该从窗外拍」—— 这才叫有人对空间负责。
没有 `reason`，它只是个参数翻译器，而翻译器不值得占用一个职位。

## 失败回环

```
生成 → 校验 ─KEEP→ 收
             ├RETRY→ 把每条 fix 塞回提示词，重试 **1 次**
             └ESCALATE→ 直接上报（缺参数这类问题重试也没用）
```

**重试一次而不是三次**：校验器的 `fix` 很具体，一次改不动通常说明
模型没理解空间关系，再问三次也是浪费。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from little_crew.agent.llm import LLMError, LlmClient
from little_crew.agent.protocol import Budget, CallEnvelope, CallResult
from little_crew.agent.previs_layout import (
    SHOT_COVER,
    Verdict,
    validate_layout,
)

__all__ = [
    "LENS_BY_SIZE",
    "PrevisAgentLlm",
    "normalize_layout",
]

#: 景别 → 镜头焦距（mm）。**摄影常识，查表即可**，不让模型猜。
LENS_BY_SIZE: dict[str, int] = {
    "extreme_wide": 18, "wide": 24, "full": 28, "medium_full": 35,
    "medium": 50, "medium_close": 65, "close": 85, "extreme_close": 100,
}

_SYSTEM = (
    "你是一位预演师，负责把分镜的**空间关系**定下来，供白模渲染。"
    "景别、运镜、时长已由分镜师决定，你**不改它们**。"
    "你决定的是：机位从哪个方位、多高看过去，人物站在画面的左右与远近。"
)


class PrevisAgentLlm:
    """把 `previs` 实现为真实 LLM 职位。"""

    agent_id = "previs"
    capability = "previs.layouts"
    produces_key = "previs.layouts"
    consumes_key = "shot.list"

    def __init__(self, *, client: LlmClient | None = None,
                 aspect: float = 640 / 384,
                 max_retry: int = 1,
                 temperature: float = 0.3) -> None:
        self.client = client or LlmClient("deepseek-flash")
        self.aspect = aspect
        self.max_retry = max_retry
        self.temperature = temperature
        #: 上一轮的校验记录（测试与证据用）
        self.last_checks: list[dict[str, Any]] = []

    # ---- 提示词 --------------------------------------------------------

    def _render(self, shot: Mapping[str, Any], *,
                fixes: Sequence[str] = ()) -> tuple[str, str]:
        size = str(shot.get("shot_size") or shot.get("shotSize") or "medium")
        lens = LENS_BY_SIZE.get(size, 50)
        cast = "、".join(str(c) for c in (shot.get("characters") or ())) or "（未标）"
        desc = str(shot.get("action") or shot.get("description") or "")[:200]
        prompt = (
            f"## 镜头\n"
            f"  shotId: {shot.get('shot_id') or shot.get('shotId')}\n"
            f"  景别: {size}（焦距已定 **{lens}mm**，不要改）\n"
            f"  运镜: {shot.get('camera_movement') or shot.get('cameraMovement')}\n"
            f"  时长: {shot.get('duration_sec') or shot.get('durationSec')} 秒\n"
            f"  出场人物: {cast}\n"
            f"  动作/描述: {desc or '（无）'}\n\n"
            "## 任务\n决定机位方位与人物站位，供白模渲染。\n\n"
            "严格输出 JSON：\n"
            '{"azimuth_deg": 数字,   // 0=正面, 90=侧面, 负数=偏左\n'
            ' "angle_deg": 数字,     // 0=平视, 正=俯拍, 负=仰拍\n'
            ' "subjects": [{"who": "名字", "x": 数字, "z": 数字, "height_cm": 数字}],\n'
            ' "set": [{"label": "门", "x": 数字, "z": 数字, "w": 宽, "d": 进深, "h": 高}],\n'
            ' "reason": "一句话说明为什么这样摆"}\n\n'
            "坐标约定（都用米）：\n"
            "  · `x`：左右偏移，0 = 画面正中，正数偏右\n"
            "  · `z`：**离相机多远** —— 主体应接近该景别的主体距离\n"
            "  · `height_cm`：身高（厘米，唯一用厘米的字段）\n"
            "  · `set` 的 `w`/`d`/`h`：宽/进深/高 —— "
            "门约 0.9×0.1×2.0，桌约 1.2×0.7×0.75，窗约 1.2×0.05×1.2\n\n"
            "规则：\n"
            "  · **必须给 `reason`**，且要具体到能被反驳："
            "写「机位在桌侧偏左朝窗，留出窗的空间」而不是「构图好看」\n"
            "  · `subjects` 要覆盖**这一镜出场的人物**\n"
            "  · `set` 只放**剧本里真的出现的空间物件**（门/窗/桌/床/树/墙），"
            "**不要凭空添家具**；没有就留空数组\n"
            "  · **不要改景别、不要改运镜、不要改时长**"
        )
        if fixes:
            prompt += ("\n## ⚠️ 上一次没通过校验，必须修正\n"
                       + "\n".join(f"  - {f}" for f in fixes) + "\n")
        return prompt, _SYSTEM

    # ---- 调用 ----------------------------------------------------------

    def _one(self, shot: Mapping[str, Any]) -> dict[str, Any]:
        """跑一镜：生成 → 校验 →（RETRY 时）带 fix 重试一次。"""
        fixes: list[str] = []
        last: dict[str, Any] = {}
        for attempt in range(self.max_retry + 1):
            prompt, system = self._render(shot, fixes=fixes)
            try:
                data = self.client.chat_json(prompt, system=system,
                                             what="空间布局")
            except LLMError as e:
                return {"shotId": shot.get("shot_id") or shot.get("shotId"),
                        "verdict": Verdict.ESCALATE.value,
                        "error": f"{e.kind}: {e}"[:200],
                        "attempts": attempt + 1}
            except Exception as e:                            # noqa: BLE001
                return {"shotId": shot.get("shot_id") or shot.get("shotId"),
                        "verdict": Verdict.ESCALATE.value,
                        "error": f"{type(e).__name__}: {e}"[:200],
                        "attempts": attempt + 1}

            layout = normalize_layout(shot, data)
            chk = validate_layout(layout, aspect=self.aspect)
            last = {"shotId": layout["shotId"], "verdict": chk.verdict.value,
                    "layout": layout, "issues": [i.as_dict() for i in chk.issues],
                    "attempts": attempt + 1}
            if chk.verdict is Verdict.KEEP:
                return last
            if chk.verdict is Verdict.ESCALATE:
                # 缺参数这类问题重试也没用
                return last
            # RETRY：把 fix 塞回提示词再来一次
            fixes = [f"{i.detail} → {i.fix}" for i in chk.issues
                     if i.severity == "major" and i.fix]
            if not fixes:
                return last
        return last

    def __call__(self, *, envelope: CallEnvelope,
                 runtime: Any = None) -> CallResult:
        payload = dict(envelope.payload or {})
        art = payload.get(self.consumes_key)
        if art is None:
            return CallResult.fail(
                "F-INPUT", f"previs 需要 payload[{self.consumes_key!r}]")
        shots = list(getattr(art, "shots", None) or art or ())
        if not shots:
            return CallResult.fail("F-INPUT", "镜头表为空 —— 无法预演")
        self.last_checks = []
        out: list[dict[str, Any]] = []
        for s in shots:
            d = s.as_dict() if hasattr(s, "as_dict") else dict(s or {})
            got = self._one(d)
            self.last_checks.append(got)
            out.append(got)
        kept = sum(1 for x in out if x.get("verdict") == Verdict.KEEP.value)
        # **全都没过**要显式失败 —— 不能交一堆"有问题的布局"当成成功
        if kept == 0:
            return CallResult.fail(
                "F-PREVIS-EMPTY",
                f"{len(out)} 镜的布局全部未通过校验 —— 不产出可用布局")
        return CallResult.success({self.produces_key: out})


def normalize_layout(shot: Mapping[str, Any],
                     data: Mapping[str, Any]) -> dict[str, Any]:
    """把模型输出拼成校验器要的形状。

    **景别/运镜/时长从分镜搬，不从模型拿** —— 那是分镜师的职权；
    `lens_mm` 按景别查表，不让模型猜。
    """
    size = str(shot.get("shot_size") or shot.get("shotSize") or "medium")
    subs: list[dict[str, Any]] = []
    for s in (data.get("subjects") or ()):
        if not isinstance(s, Mapping):
            continue
        subs.append({"who": str(s.get("who") or "")[:40],
                     "x": _f(s.get("x"), 0.0),
                     "z": _f(s.get("z"), 0.0),
                     "height_cm": _f(s.get("height_cm"), 168.0)})
    return {
        "shotId": str(shot.get("shot_id") or shot.get("shotId") or ""),
        "shot": {
            "shot_size": size,
            "lens_mm": float(LENS_BY_SIZE.get(size, 50)),
            "azimuth_deg": _f(data.get("azimuth_deg"), 0.0),
            "angle_deg": _f(data.get("angle_deg"), 0.0),
            "duration_sec": _f(shot.get("duration_sec")
                               or shot.get("durationSec"), 3.0),
            "camera_movement": str(shot.get("camera_movement")
                                   or shot.get("cameraMovement") or "static"),
        },
        "subjects": subs,
        # **场景道具**：与 subjects 共用同一套坐标（x 左右 / z 离相机多远）。
        # 地面由渲染器无条件画，这里只放剧本里的空间物件。
        "set": _set_of(data),
        "reason": str(data.get("reason") or "")[:200],
    }


def _set_of(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """规整场景道具。**尺寸必须为正** —— 宽/高为 0 的盒子在画面里不存在，
    留着只会让"人站在门里"这种判断失效。"""
    out: list[dict[str, Any]] = []
    for it in (data.get("set") or ()):
        if not isinstance(it, Mapping):
            continue
        label = str(it.get("label") or "").strip()
        if not label:
            continue
        w = _f(it.get("w"), 0.0)
        d = _f(it.get("d"), 0.0)
        h = _f(it.get("h"), 0.0)
        if w <= 0 or d <= 0 or h <= 0:
            continue
        out.append({"label": label[:20], "x": _f(it.get("x"), 0.0),
                    "z": _f(it.get("z"), 0.0),
                    "w": round(w, 3), "d": round(d, 3), "h": round(h, 3)})
    return out


def _f(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default
