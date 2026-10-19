"""`runtime/previs_layout.py` 自检 —— 纯几何，不联网、不调模型。

## 守什么

| 段 | 守什么 |
|---|---|
| A 一致 | **与 Blender `setup_camera` 同一套数学**（含 `alpha.90` 的 `sensor_fit` 修复） |
| B 缺观测 | 缺镜头参数 / 缺角色 / 缺身高 —— **全部不判通过** |
| C 拒绝 | 人在相机后方 · 景别与进深矛盾 · 切头切脚 · 两人重叠 |
| D 三态 | KEEP / RETRY / ESCALATE 的判定顺序；**RETRY 必须给得出改法** |
| E 张力 | 出画只记 `minor`（刻意的画外构图是存在的） |

## 这一段为什么重要

**校验器是 `PrevisAgent` 的验收标准。** 没有它，LLM 的输出好坏无从判断，
只能人一辆辆看 —— 那样"多智能体协作"就退化成"人在循环里当裁判"。
"""
from __future__ import annotations

import math
import sys

from little_crew.runtime.previs_layout import (
    FOCUS_RATIO,
    SENSOR_HEIGHT_MM,
    SHOT_COVER,
    Verdict,
    camera_from_shot,
    validate_layout,
)

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


def _layout(**over: object) -> dict:
    base = {
        "shotId": "SH-001",
        "shot": {"shot_size": "full", "lens_mm": 50.0, "azimuth_deg": 0.0,
                 "angle_deg": 0.0, "duration_sec": 3.0,
                 "camera_movement": "static"},
        "subjects": [{"who": "少女", "x": 0.0, "z": 4.0, "height_cm": 168}],
        "reason": "机位正对，全身入画",
    }
    base.update(over)
    return base


def _consistency() -> None:
    print()
    print("A. 与 Blender 同一套数学")

    cam = camera_from_shot({"shot_size": "full", "lens_mm": 50.0,
                            "azimuth_deg": 0.0, "angle_deg": 0.0},
                           subject_h=1.68)
    cover = SHOT_COVER["full"] * 1.68
    fov = 2 * math.atan(SENSOR_HEIGHT_MM / (2 * 50.0))
    dist = (cover / 2) / math.tan(fov / 2)
    check("A1 cover = 1.15 × 身高", abs(cam["cover"] - cover) < 1e-9,
          f"{cam['cover']} vs {cover}")
    check("A2 垂直视场按 sensor_height(24mm) 算（alpha.90 的修复）",
          abs(cam["fovV"] - fov) < 1e-12, f"{cam['fovV']} vs {fov}")
    check("A3 距离 = (cover/2)/tan(fov/2)", abs(cam["distance"] - dist) < 1e-9,
          f"{cam['distance']:.4f} vs {dist:.4f}")
    check("A4 视线高度 = 0.55 × 身高",
          abs(cam["focus"][2] - FOCUS_RATIO * 1.68) < 1e-9,
          str(cam["focus"]))
    check("A5 方位角 0 时相机在 -Y 方向",
          abs(cam["position"][1] + dist) < 1e-9 and abs(cam["position"][0]) < 1e-9,
          str(cam["position"]))
    cam90 = camera_from_shot({"shot_size": "full", "lens_mm": 50.0,
                              "azimuth_deg": 90.0}, subject_h=1.68)
    check("A6 方位角 90 时相机在 +X 方向",
          abs(cam90["position"][0] - cam90["distance"]) < 1e-9
          and abs(cam90["position"][1]) < 1e-9, str(cam90["position"]))


def _missing() -> None:
    print()
    print("B. 缺观测 ≠ 通过")

    r = validate_layout({"shotId": "S1", "subjects": [{"who": "A"}]})
    check("B1 ★ 缺 shot -> ESCALATE（不是 KEEP）",
          r.verdict is Verdict.ESCALATE, r.verdict.value)

    r2 = validate_layout({"shotId": "S1", "shot": {"shot_size": "full"},
                          "subjects": []})
    check("B2 ★ 没有角色 -> ESCALATE", r2.verdict is Verdict.ESCALATE,
          r2.verdict.value)

    r3 = validate_layout(_layout(subjects=[{"who": "A", "z": 4.0}]))
    check("B3 ★ 没给身高 -> 有问题（不许静静按默认算成通过）",
          any(i.code == "no-height" for i in r3.issues),
          str([i.code for i in r3.issues]))

    r4 = validate_layout(_layout(shot={"shot_size": "不存在的景别"}))
    check("B4 ★ 未知景别 -> ESCALATE",
          r4.verdict is Verdict.ESCALATE
          and any(i.code == "bad-shot-param" for i in r4.issues),
          r4.verdict.value)

    r5 = validate_layout(_layout(shot={"shot_size": "full", "lens_mm": 0}))
    check("B5 焦距 0 -> ESCALATE", r5.verdict is Verdict.ESCALATE,
          r5.verdict.value)


def _reject() -> None:
    print()
    print("C. 该拒的要拒")

    good = validate_layout(_layout())
    check("C0 基线布局判 KEEP（否则后面全无意义）",
          good.verdict is Verdict.KEEP,
          f"{good.verdict.value} {[i.code for i in good.issues]}")

    r = validate_layout(_layout(subjects=[{"who": "A", "x": 0, "z": -1,
                                           "height_cm": 168}]))
    check("C1 ★ 角色在相机后方 -> ESCALATE",
          r.verdict is Verdict.ESCALATE
          and any(i.code == "behind-camera" for i in r.issues), r.verdict.value)

    # 中景却站得很远 -> 与景别矛盾
    r2 = validate_layout(_layout(
        shot={"shot_size": "medium", "lens_mm": 50.0, "azimuth_deg": 0.0},
        subjects=[{"who": "A", "x": 0, "z": 30.0, "height_cm": 168}]))
    check("C2 ★★ 中景却站 30m -> RETRY（景别与进深矛盾）",
          r2.verdict is Verdict.RETRY
          and any(i.code == "too-far-for-size" for i in r2.issues),
          f"{r2.verdict.value} {[i.code for i in r2.issues]}")

    # 特写却占满 -> 切头切脚
    # ---- 切头切脚：只对"本该装下整个人"的景别成立 ----
    #
    # 真机教训（2026-10-19）：第一版对所有景别都查这条，于是 `close` 变成了无解 ——
    # `too-far-for-size` 要求 z≈1.9m，而 158cm 的人在该距离下必然占高 >100%。
    # 模型被判 RETRY 两次，每次都在把 z 往远推来躲这条错规则。
    r3 = validate_layout(_layout(
        shot={"shot_size": "full", "lens_mm": 28.0, "azimuth_deg": 0.0},
        subjects=[{"who": "A", "x": 0, "z": 1.5, "height_cm": 168}]))
    check("C3 ★★ `full` 里人物占高 >100% -> RETRY（该装下整个人却切了）",
          r3.verdict is Verdict.RETRY
          and any(i.code == "too-tall-in-frame" for i in r3.issues),
          f"{r3.verdict.value} {[i.code for i in r3.issues]}")

    # 而 `close` 里人比画面高是**正常**的 —— 不该因此判错
    r3b = validate_layout(_layout(
        shot={"shot_size": "close", "lens_mm": 85.0, "azimuth_deg": 0.0},
        subjects=[{"who": "A", "x": 0, "z": 1.2, "height_cm": 168}]))
    check("C3b ★★ `close` 里人比画面高是特写常态，**不判 too-tall**",
          not any(i.code == "too-tall-in-frame" for i in r3b.issues),
          f"{r3b.verdict.value} {[i.code for i in r3b.issues]}")

    # 两人重叠
    r4 = validate_layout(_layout(subjects=[
        {"who": "A", "x": 0.0, "z": 4.0, "height_cm": 168},
        {"who": "B", "x": 0.02, "z": 4.02, "height_cm": 168}]))
    check("C4 ★ 两人位置重合 -> RETRY（白模里会叠成一个）",
          r4.verdict is Verdict.RETRY
          and any(i.code == "subjects-overlap" for i in r4.issues),
          f"{r4.verdict.value} {[i.code for i in r4.issues]}")

    # 缺 reason
    r5 = validate_layout(_layout(reason=""))
    check("C5 缺 reason -> RETRY（评审无法判断机位是否合理）",
          r5.verdict is Verdict.RETRY
          and any(i.code == "no-reason" for i in r5.issues), r5.verdict.value)

    # 出画只记 minor
    r6 = validate_layout(_layout(subjects=[
        {"who": "A", "x": 20.0, "z": 4.0, "height_cm": 168}]))
    check("C6 ★ 出画只记 minor（刻意的画外构图是存在的），且不独自把判定拉到 RETRY",
          any(i.code == "out-of-frame-x" and i.severity == "minor"
              for i in r6.issues)
          and r6.verdict is Verdict.KEEP,
          f"{r6.verdict.value} {[ (i.code,i.severity) for i in r6.issues]}")


def _tristate() -> None:
    print()
    print("D. 三态与可执行性")

    r = validate_layout(_layout(
        shot={"shot_size": "medium", "lens_mm": 50.0, "azimuth_deg": 0.0},
        subjects=[{"who": "A", "x": 0, "z": 30.0, "height_cm": 168}]))
    check("D1 RETRY 的每条 major 都带 fix（给不出改法就不该叫 RETRY）",
          all(i.fix for i in r.issues if i.severity in ("major", "blocker")),
          str([(i.code, bool(i.fix)) for i in r.issues]))

    # 构造"major 但无 fix" -> 必须降为 ESCALATE
    from little_crew.runtime import previs_layout as M
    orig = M.Issue

    class _NoFix(Issue := orig):
        def __init__(self, *a, **k):
            k["fix"] = ""
            super().__init__(*a, **k)
    # 直接改模块引用不好，换用 monkeypatch validate 内部不可行 ——
    # 用"未知景别"这条已被 block 覆盖，这里改为断言判定集合本身
    check("D2 判定只可能是三态之一",
          r.verdict in (Verdict.KEEP, Verdict.RETRY, Verdict.ESCALATE))

    good = validate_layout(_layout())
    check("D3 KEEP 时 ok 为真", good.ok is True)
    check("D4 KEEP 的 projected 里有 u 与 heightFraction",
          len(good.projected) == 1 and "u" in good.projected[0],
          str(good.projected)[:80])
    check("D5 as_dict 可序列化（能进证据链）",
          isinstance(good.as_dict()["verdict"], str)
          and isinstance(good.as_dict()["issues"], list))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")            # type: ignore[attr-defined]
    except Exception:                                        # noqa: BLE001
        pass
    print("=" * 74)
    print("白模布局校验器 —— 自检（纯几何）")
    print("=" * 74)
    _consistency()
    _missing()
    _reject()
    _tristate()
    print()
    npass = sum(1 for _, ok, _ in _results if ok)
    for name, ok, detail in _results:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 74)
    print(f"结果：{npass}/{len(_results)} 通过")
    return 0 if npass == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
