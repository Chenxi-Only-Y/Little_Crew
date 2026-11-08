"""人工 Gate：成片前**人看一眼**，并如实标记"人在环里"（2026-11-08）。

## 为什么要有它，又要能跳过

- **要有**：机器现在能判"崩没崩""有没有镜头"，但判不了"这集能不能给人看"
- **要能跳过**：整集跑 20~30 分钟、批量实验几十次，每次都停下来问人不现实

## 三种结果，都必须留痕

| 结果 | 含义 | 对数据的影响 |
|---|---|---|
| `approve` | 人看过并认可 | `humanReviewed=True`，`assisted=False` |
| `reject` | 人驳回（写原因） | 本集**不算通过**，`assisted=True` |
| `skip` | 没问人（非交互 / 关掉） | `humanReviewed=False`，**不假装通过** |

## 纪律

**跳过不是通过。** `skip` 必须能和 `approve` 区分开 ——
否则"人工确认"会被悄悄算成"系统跑通"，答辩数据就脏了。
"""
from __future__ import annotations

import sys

#: 允许的决策
DECISIONS = ("approve", "reject", "skip")


def is_interactive() -> bool:
    """能不能真的问人（stdin 是 TTY 且不是 CI）。"""
    try:
        import os

        if os.environ.get("CI") or os.environ.get("LITTLE_CREW_NONINTERACTIVE"):
            return False
        return bool(sys.stdin and sys.stdin.isatty())
    except Exception:                                        # noqa: BLE001
        return False


def summarize(shots: int, seconds: float, *, pending: int = 0,
              vision_errors: int = 0, gate_ok: bool = True) -> str:
    """给"人"看的一屏摘要（不堆砌指标，只放判断需要的信息）。"""
    lines = [
        "=" * 62,
        "人工确认（这一步只有人能判）",
        "-" * 62,
        f"  镜头 {shots} 个 · 成片 {seconds:.1f} 秒",
        f"  门禁 {'通过' if gate_ok else '未过'} · 待办 {pending} 条",
        f"  视觉闸严重项 {vision_errors} 条",
        "",
        "  机器判不了：这集能不能给人看、节奏闷不闷、像不像样。",
        "=" * 62,
    ]
    return "\n".join(lines)


def decide(*, auto: str = "", timeout_s: int = 0, summary: str = "") -> dict:
    """问一次。返回 `{ok, decision, reason, by, interactive}`。

    `auto`：非交互时的预设决定（`approve` / `reject` / `skip`）。
    """
    if summary:
        print(summary)
    if auto:
        d = str(auto).strip().lower()
        if d not in DECISIONS:
            return {"ok": False, "decision": "skip", "by": "auto-invalid",
                    "reason": f"非法预设 {auto!r}（可选 {DECISIONS}）",
                    "interactive": False}
        return {"ok": True, "decision": d, "by": "flag",
                "reason": "由 --gate-auto 指定（非交互）",
                "interactive": False}
    if not is_interactive():
        return {"ok": True, "decision": "skip", "by": "none",
                "reason": "非交互环境（没有问人，**不等于通过**）",
                "interactive": False}
    print("  输入 y 认可 / n 驳回（可附原因）/ 直接回车跳过：")
    try:
        line = input("  > ").strip()
    except EOFError:
        return {"ok": True, "decision": "skip", "by": "eof",
                "reason": "输入结束，未作答", "interactive": True}
    low = line.lower()
    if low in ("y", "yes", "1", "ok"):
        return {"ok": True, "decision": "approve", "by": "human",
                "reason": "", "interactive": True}
    if low in ("n", "no", "0"):
        return {"ok": True, "decision": "reject", "by": "human",
                "reason": "人工驳回（未填原因）", "interactive": True}
    if low.startswith("n "):
        return {"ok": True, "decision": "reject", "by": "human",
                "reason": line[2:].strip()[:200], "interactive": True}
    return {"ok": True, "decision": "skip", "by": "human",
            "reason": "人未作答（跳过）", "interactive": True}


def as_record(res: dict, *, shots: int = 0, seconds: float = 0.0) -> dict:
    """落盘形状（证据链用）。"""
    d = str(res.get("decision") or "skip")
    return {
        "kind": "human.gate",
        "decision": d,
        "reviewed": d in ("approve", "reject"),
        # **approved 只有在"人真的点了认可"时才为真**
        "approved": d == "approve",
        # assisted：人动过（驳回）或修正过 —— 这样实验数据能把它剔掉
        "assisted": d == "reject",
        "by": res.get("by", ""), "reason": res.get("reason", ""),
        "interactive": bool(res.get("interactive")),
        "shots": int(shots), "seconds": round(float(seconds), 1),
        "notJudged": ["节奏闷不闷", "能不能给人看", "像不像样"],
    }


def apply(ok: bool, reason: str, record: dict) -> tuple[bool, str]:
    """把人工决定作用到"本集算不算通过"上。

    **只有 reject 会改判**：approve 不会把失败变成成功（机器判失败就是失败），
    skip 什么都不做。这样"人工认可"不能掩盖真实故障。
    """
    if str((record or {}).get("decision")) != "reject":
        return ok, reason
    why = str((record or {}).get("reason") or "（未填原因）")
    return False, f"人工驳回：{why}"
