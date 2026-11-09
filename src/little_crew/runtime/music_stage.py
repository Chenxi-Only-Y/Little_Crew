"""音乐床阶段的**可单测函数**：优先 composer 职位，失败退回内联（2026-11-09）。

## 为什么抽出来

昨晚两次尝试把音乐床"直接改成调 composer"都引入了回归：
`NameError: _film_s` 被外层 `try/except` 吞掉 → 音乐床与混流整段没跑，
外部表现却是「职位已绑定却从未被调用」。

**根因不是 composer，是那段代码只能靠"整集真跑"验证。**

所以这一版先把决策抽成纯函数（本模块），再让 `episode.py` 只写一行调用。
这样"接对没接对"用**单测**就能回答，不必等 20 分钟一集。

## 三层优先级

```
① composer 已绑定且成功  -> 用它（source 如实来自 shot.music）
② composer 绑定但失败    -> 打印原因，退回内联
③ composer 未绑定        -> 退回内联（与历史行为一致）
```
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def film_seconds(shots) -> float:
    """成片时长（= 各镜时长之和）。**统一在这里算**，避免各段各写一份。"""
    return round(sum(float(getattr(s, "duration_sec", 0) or 0) for s in (shots or ())), 3)


def _from_agent(orch, shots, total: float, out_dir,
                ffmpeg: str = "") -> tuple[dict, str]:
    """跑 composer 职位。返回 `(bed, why)`；`why` 非空表示没拿到。"""
    impls = getattr(orch, "_impls", {}) or {}
    if "composer" not in impls:
        return {}, "composer 未绑定"
    try:
        out = orch.run_stage("composer", "shot.music",
                             {"shot.list": list(shots),
                              "totalSeconds": total,
                              "outDir": str(out_dir),
                              # ⚠️ 必须透传：Agent 自己的默认是 `"ffmpeg"`（不在 PATH），
                              # 不透传就会"agent 失败 → 退回内联"，看起来像没接上。
                              "ffmpeg": ffmpeg or ""})
        if not getattr(out, "ok", False):
            return {}, f"composer 未产出：{str(getattr(out, 'error', '') or '')[:80]}"
        plan = orch.blackboard.value("shot.music")
        if not isinstance(plan, dict) or not plan.get("path"):
            return {}, "composer 没有给出可用文件"
        return ({"path": str(plan["path"]),
                 "source": str(plan.get("source") or ""),
                 "seconds": float(plan.get("seconds") or 0),
                 "via": "composer"}, "")
    except Exception as e:                                    # noqa: BLE001
        return {}, f"composer 异常：{type(e).__name__}: {str(e)[:70]}"


def resolve_bed(*, orch, shots, out_dir, ffmpeg: str = "ffmpeg",
                total_seconds: float | None = None) -> dict:
    """决定用哪条音乐床。返回 `{path, source, seconds, via, note}`（可全空）。

    **不抛异常**：音乐床失败不该拖垮整集（后面混流会如实记 `bgmSource`）。
    """
    total = float(total_seconds if total_seconds is not None
                  else film_seconds(shots))
    out = Path(str(out_dir))
    notes: list[str] = []

    bed, why = _from_agent(orch, shots, total, out, ffmpeg)
    if bed:
        return {**bed, "requestedSeconds": total, "note": ""}
    if why and "未绑定" not in why:
        notes.append(why)

    # 退回内联（历史行为）
    try:
        from little_crew.runtime import music_bed as MB

        r = MB.resolve(total, out, ffmpeg=ffmpeg)
        return {"path": str(r.get("path") or ""),
                "source": str(r.get("source") or ""),
                "seconds": float(r.get("seconds") or 0),
                "via": "inline", "requestedSeconds": total,
                "note": "；".join(notes)}
    except Exception as e:                                    # noqa: BLE001
        notes.append(f"内联音乐床失败：{type(e).__name__}: {str(e)[:60]}")
        return {"path": "", "source": "", "seconds": 0.0, "via": "",
                "requestedSeconds": total, "note": "；".join(notes)}
