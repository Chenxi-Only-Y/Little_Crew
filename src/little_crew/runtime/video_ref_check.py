"""`runtime/video_ref.py` 自检 —— 纯算术，不联网。

## 守什么

| 段 | 守什么 |
|---|---|
| A 表 | 三家契约登记齐全、区间自洽 |
| B 解析 | **刚好过线**（不是"越大越好"）；偶数边长；比例合法 |
| C 交集 | 三家一起要时取最紧的那条 |
| D 校核 | **先测拒绝** —— 太小/太大/太长/超大/奇数边 |
| E 缺观测 | 没给目标模型 → 不能判通过 |

## 为什么 B 段要钉"刚好过线"

白模视频的参考视频**直接换 token**。
"稍微大一点更保险"是错的 —— 超出的每个像素都在花钱买更僵硬的成片。
"""
from __future__ import annotations

import sys

from little_crew.runtime.video_ref import (
    LIMITS,
    Profile,
    check_video,
    resolve_profile,
    violations,
)

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


def _table() -> None:
    print()
    print("A. 契约表")
    check("A1 三家都在表里",
          {"seedance-2.0", "minimax-h3", "wan-animate-2.2"} <= set(LIMITS),
          str(sorted(LIMITS)))
    s = LIMITS["seedance-2.0"]
    check("A2 ★ Seedance 像素下限登记为 407,696（一手文档）",
          s.min_pixels == 407_696, str(s.min_pixels))
    m = LIMITS["minimax-h3"]
    check("A3 ★ MiniMax 体积上限 50MB（三家最紧）", m.max_mb == 50.0,
          str(m.max_mb))
    w = LIMITS["wan-animate-2.2"]
    check("A4 Wan Animate 时长到 30 秒", w.max_seconds == 30.0,
          str(w.max_seconds))
    check("A5 每条都带 notes（来源可追溯）",
          all(x.notes for x in LIMITS.values()),
          str([k for k, v in LIMITS.items() if not v.notes]))


def _resolve() -> None:
    print()
    print("B. 解析：刚好过线")
    p = resolve_profile(["seedance-2.0"])
    check("B1 ★★ 总像素**刚好**过 Seedance 下限",
          p.pixels >= 407_696 and p.pixels < 407_696 * 1.10,
          f"{p.width}×{p.height}={p.pixels} 超 "
          f"{(p.pixels/407_696 - 1)*100:.1f}%")
    check("B2 ★★ 边长都是偶数（h264 yuv420p 硬要求）",
          p.width % 2 == 0 and p.height % 2 == 0, f"{p.width}×{p.height}")
    check("B3 ★ 比例接近 16:9",
          abs(p.aspect - 16 / 9) < 0.02, f"{p.aspect:.3f}")
    check("B4 fps 落在区间内", 24 <= p.fps <= 60, str(p.fps))

    p2 = resolve_profile(["minimax-h3"])
    check("B5 MiniMax 没有像素下限 → 取更小的边长（不被无谓抬高）",
          p2.pixels <= p.pixels, f"{p2.width}×{p2.height}={p2.pixels}")

    p3 = resolve_profile(["wan-animate-2.2"])
    check("B6 Wan Animate 边长 ≥ 200", min(p3.width, p3.height) >= 200,
          f"{p3.width}×{p3.height}")


def _intersect() -> None:
    print()
    print("C. 三家一起要 → 取最紧")
    all3 = ("seedance-2.0", "minimax-h3", "wan-animate-2.2")
    p = resolve_profile(all3)
    check("C1 ★★ 同时满足 Seedance 像素下限（最紧的一条）",
          p.pixels >= 407_696, f"{p.width}×{p.height}={p.pixels}")
    check("C2 ★★ 同时满足 MiniMax 50MB（体积取最紧）",
          p.max_mb == 50.0, str(p.max_mb))
    check("C3 ★ 时长取交集 [2, 15]（Wan 到 30 秒也要收紧到 15）",
          p.min_seconds == 2.0 and p.max_seconds == 15.0,
          f"[{p.min_seconds}, {p.max_seconds}]")
    check("C4 记下了目标是哪三家（manifest 要写）",
          set(p.models) == set(all3), str(p.models))
    check("C5 交集解不比为单家准备的更小",
          p.pixels >= resolve_profile(["seedance-2.0"]).pixels,
          str(p.pixels))


def _clamp() -> None:
    print()
    print("C′. 时长跟随该镜")
    p = resolve_profile(["seedance-2.0"])
    check("C6 ★ 2.5s 的镜头渲 2.5s（不被统一成 5s）",
          p.clamp_seconds(2.5) == 2.5, str(p.clamp_seconds(2.5)))
    check("C7 超过上限被夹到 15",
          p.clamp_seconds(40.0) == 15.0, str(p.clamp_seconds(40.0)))
    check("C8 低于下限被夹到 2",
          p.clamp_seconds(0.5) == 2.0, str(p.clamp_seconds(0.5)))


def _reject() -> None:
    print()
    print("D. 校核：先测拒绝")
    models = ["seedance-2.0", "minimax-h3", "wan-animate-2.2"]
    p = resolve_profile(models)
    good = {"width": p.width, "height": p.height, "fps": 24,
            "seconds": 5.0, "bytes": 4 * 1024 * 1024}
    ok, probs = check_video(good, models)
    check("D0 基线合规（否则后面全无意义）", ok, str(probs))

    b, pr = check_video(dict(good, width=640, height=384), models)
    check("D1 ★★ 640×384 被拒（总像素不足）",
          not b and any("总像素" in x for x in pr), str(pr))

    b, pr = check_video(dict(good, width=p.width, height=p.height - 1), models)
    check("D2 ★★ 奇数边长被拒（h264 yuv420p）",
          not b and any("偶数" in x for x in pr), str(pr))

    b, pr = check_video(dict(good, seconds=40.0), models)
    check("D3 超长被拒", not b and any("时长" in x for x in pr), str(pr))

    b, pr = check_video(dict(good, bytes=90 * 1024 * 1024), models)
    check("D4 ★ 90MB 被拒（MiniMax 只有 50MB）",
          not b and any("体积" in x for x in pr), str(pr))

    b, pr = check_video(dict(good, fps=12), models)
    check("D5 帧率过低被拒", not b and any("帧率" in x for x in pr), str(pr))


def _missing() -> None:
    print()
    print("E. 缺观测 ≠ 通过")
    ok, probs = check_video({"width": 864, "height": 486, "fps": 24,
                             "seconds": 5.0, "bytes": 1024}, [])
    check("E1 ★★ 没给目标模型 → 不判通过",
          not ok and any("缺观测" in x for x in probs), str(probs))
    probs2 = violations({"fps": 24, "seconds": 5.0}, ["seedance-2.0"])
    check("E2 ★ 缺分辨率 → 明确报出来（不是静默放行）",
          any("缺分辨率" in x for x in probs2), str(probs2))
    try:
        resolve_profile(["not-a-model"])
        check("E3 未登记模型应报错", False, "没报错")
    except KeyError as e:
        check("E3 ★ 未登记模型 → KeyError（并列出已登记）",
              "已登记" in str(e), str(e)[:80])


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")            # type: ignore[attr-defined]
    except Exception:                                        # noqa: BLE001
        pass
    print("=" * 74)
    print("参考视频规格解析 —— 自检（纯算术）")
    print("=" * 74)
    _table()
    _resolve()
    _intersect()
    _clamp()
    _reject()
    _missing()
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
