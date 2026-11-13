"""后期统筹自检（2026-11-13）：`runtime/post_production.py`。

锁住五件事：
1 **计划可复现**：同一输入两次生成完全一致（纯函数）
2 **转场按场次变化**：同场 cut / 换场 fade（跳接 dissolve 规则保留）
3 **音轨策略二选一**：native（视频原生音轨）/ tts+bgm（静帧路径），且带说明
4 数字与已有实现对齐：响度 -16 LUFS、字幕 16 字/行 2 行、BGM -22dB
5 走编排器：产物进黑板、计入预算
"""
from __future__ import annotations

import sys

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


class _S:
    def __init__(self, sid: str, scene: str):
        self.shot_id = sid
        self.scene_id = scene


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("后期统筹（post_production）—— 自检")
    print("=" * 66)

    from little_crew.runtime import post_production as pp

    shots = [_S("SH-E1-S01-001", "S01"), _S("SH-E1-S01-002", "S01"),
             _S("SH-E1-S02-001", "S02"), _S("SH-E1-S02-002", "S02")]
    payload = {"shot.list": shots, "script.final": "少女：我不会走。\n老人：那就留下。"}

    print(); print("A. 计划内容")
    plan = pp.build_plan(payload)
    check("A1 ★★★ 产出 kind=post.plan", plan.get("kind") == "post.plan", str(plan.get("kind")))
    check("A2 ★★★ 镜数记录正确", plan.get("shotCount") == 4, str(plan.get("shotCount")))
    check("A3 ★★★ 默认音轨策略是 tts+bgm（静帧路径）",
          plan["audioStrategy"] == "tts+bgm" and "H3" in plan["audioNativeNote"],
          plan["audioStrategy"])
    check("A4 ★★★ 响度目标与成片实现一致（-16 LUFS）",
          plan["loudness"]["targetLufs"] == -16.0, str(plan["loudness"]))
    check("A5 ★★★ 字幕规格可数（16 字/行、2 行、底部安全区）",
          plan["subtitle"]["maxCharsPerLine"] == 16
          and plan["subtitle"]["maxLines"] == 2
          and plan["subtitle"]["safeAreaBottom"] == 0.08, str(plan["subtitle"]))

    print(); print("B. 转场规则")
    seq = [x["type"] for x in plan["transitions"]["sequence"]]
    check("B1 ★★★ 同场用 cut、换场用 fade",
          seq == ["cut", "cut", "fade", "cut"], str(seq))
    counts = plan["transitions"]["counts"]
    check("B2 ★★ 计数与序列一致", counts.get("cut") == 3 and counts.get("fade") == 1, str(counts))
    check("B3 ★★ 跳接规则保留（dissolve）",
          pp.TRANSITIONS.get("timeJump") == "dissolve", str(pp.TRANSITIONS))

    print(); print("C. 音轨策略与可复现")
    native = pp.build_plan({**payload, "nativeAudio": True})
    check("C1 ★★★ nativeAudio=True -> native 且不再叠加 TTS",
          native["audioStrategy"] == "native" and "不再叠加 TTS" in native["audioNativeNote"],
          native["audioStrategy"])
    again = pp.build_plan(payload)
    check("C2 ★★★ 同一输入两次生成**完全一致**（可复现）",
          plan == again, "")
    empty = pp.build_plan({})
    check("C3 ★★ 空输入不崩（0 镜、0 字幕）",
          empty.get("shotCount") == 0 and empty["subtitle"]["estimatedCount"] == 0, "")

    print(); print("D. 走编排器")
    from little_crew.agent.orchestrator import CrewOrchestrator

    o = CrewOrchestrator()
    o.bind("post_production", pp.PostProduction())
    out = o.run_stage("post_production", "post.plan", payload)
    check("D1 ★★★ 职位调用成功", out.ok, str(getattr(out, "reason", ""))[:60])
    check("D2 ★★★ post.plan 进黑板",
          isinstance(o.blackboard.value("post.plan"), dict), "")
    check("D3 ★★★ 调用计入预算（steps>=1）", o.ledger.spent_steps >= 1,
          str(o.ledger.spent_steps))

    n = sum(1 for _, ok, _ in _r if ok)
    print()
    for name, ok, detail in _r:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 66)
    print(f"结果：{n}/{len(_r)} 通过")
    return 0 if n == len(_r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
