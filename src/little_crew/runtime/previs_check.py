"""`runtime/previs.py` 自检 —— 不调 Blender、不联网。

## 守什么

| 段 | 守什么 |
|---|---|
| A 规格 | `layout_to_spec`：分辨率默认 640×384、身高带过去、镜头字段不漏 |
| B 警告 | **丢掉的信息必须报出来**（缺 subjects / 没有主体） |
| C 失败 | Blender 不存在 / 返回非零 / 没输出 RENDER_DONE —— 全部 `ok=False` 且**带回原因** |
| D 首帧 | 首帧必须来自**同一份帧序列**（复制而非另画） |

## 为什么这些值得测

`render_layout` 是**可选增强**：它坏了不该让整集渲染挂掉。
但"不该挂掉"与"坏了没人知道"是两件事 ——
所以每条失败路径都必须**返回原因**，而这里就是钉住这些原因的地方。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from little_crew.runtime.previs import (
    DEFAULT_RESOLUTION,
    default_blender_script,
    first_frame,
    layout_to_spec,
    render_layout,
)

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


def _layout(**over: object) -> dict:
    base = {
        "shotId": "SH-001",
        "shot": {"shot_size": "medium", "lens_mm": 50.0, "azimuth_deg": 12.0,
                 "angle_deg": 3.0, "duration_sec": 2.5,
                 "camera_movement": "dolly_in"},
        "subjects": [{"who": "少女", "x": -0.4, "z": 2.6, "height_cm": 158},
                     {"who": "旅人", "x": 0.8, "z": 5.2, "height_cm": 180}],
        "set": [{"label": "门", "x": 0.8, "z": 5.3, "w": 0.9, "d": 0.1, "h": 2.0}],
        "reason": "机位偏左朝窗",
    }
    base.update(over)
    return base


def _spec() -> None:
    print()
    print("A. 布局 → spec")
    spec, warns = layout_to_spec(_layout(), out_dir="/tmp/x")
    check("A1 ★ 默认分辨率 640×384（与出图一致）",
          (spec["resolution"]["width"], spec["resolution"]["height"])
          == DEFAULT_RESOLUTION, str(spec["resolution"]))
    check("A2 ★★ 两个角色都进 subjects（不再丢人）",
          len(spec["subjects"]) == 2, str(len(spec["subjects"])))
    check("A3 ★★ 站位 x/z 原样带过去（不再固定在原点）",
          spec["subjects"][0]["x"] == -0.4 and spec["subjects"][0]["z"] == 2.6,
          str(spec["subjects"][0]))
    check("A4 ★ 场景 set 原样带过去", len(spec["set"]) == 1, str(spec["set"]))
    check("A5 镜头四要素不漏",
          spec["shot"]["shot_size"] == "medium"
          and spec["shot"]["lens_mm"] == 50.0
          and spec["shot"]["azimuth_deg"] == 12.0
          and spec["shot"]["camera_movement"] == "dolly_in",
          str(spec["shot"]))
    check("A6 时长带过去（决定帧数）", spec["shot"]["duration_sec"] == 2.5,
          str(spec["shot"].get("duration_sec")))
    check("A7 两位角色都进 spec 时零警告（没丢东西就不该报警）",
          warns == [], str(warns))


def _warn() -> None:
    print()
    print("B. 丢掉的信息必须报出来")
    spec, warns = layout_to_spec(_layout(subjects=[]), out_dir="/tmp/x")
    check("B1 ★ 没有 subjects → 有警告", bool(warns), str(warns))
    check("B2 spec 里 subjects 为空数组（下游会拒绝，不静默）",
          spec["subjects"] == [], str(spec["subjects"]))


def _fail() -> None:
    print()
    print("C. 失败要带回原因")
    # ⚠️ **不要用 `/tmp/...`**（2026-10-19 实测）：
    # Windows 上它会变成盘符相对的 `\\tmp\\`，且上一次运行留下的目录
    # 会让清理时的 `unlink()` 报 PermissionError。
    # `tempfile.mkdtemp()` 每次都新建、名字唯一、跨平台。
    tmp = Path(tempfile.mkdtemp(prefix="previs_chk_"))
    r = render_layout(_layout(), blender="definitely-not-a-real-blender",
                      out_root=str(tmp))
    check("C1 ★ blender 不存在 → ok=False（不抛异常）", r["ok"] is False,
          str(r.get("ok")))
    check("C2 ★★ 且**带回原因**（不能静默失败）",
          bool(r.get("reason")), str(r.get("reason"))[:80])
    check("C3 原因里提到 Blender", "Blender" in str(r.get("reason")),
          str(r.get("reason"))[:80])


def _first() -> None:
    print()
    print("D. 首帧来自同一份帧序列")
    d = Path(tempfile.mkdtemp(prefix="previs_first_"))
    (d / "gray").mkdir(parents=True, exist_ok=True)
    # 造两帧，内容可区分
    (d / "gray" / "f00001.png").write_bytes(b"AAA")
    (d / "gray" / "f00002.png").write_bytes(b"BBB")
    png = first_frame(d / "gray", d / "previs_first.png")
    check("D1 ★ 首帧 = 帧序列的第一帧（内容一致，不是另画一张）",
          png.read_bytes() == b"AAA", png.read_bytes()[:10].decode("latin1"))
    empty = d / "empty"
    empty.mkdir(exist_ok=True)
    try:
        first_frame(empty, d / "x.png")
        check("D2 空目录应报错", False, "没报错")
    except Exception as e:                                    # noqa: BLE001
        check("D2 ★ 空帧目录 → 报错（不产出空首帧）", True, type(e).__name__)


def _script() -> None:
    print()
    print("E. 内置脚本随包发布")
    p = default_blender_script()
    check("E1 ★★ Blender 脚本在包内（不依赖 research/）", p.is_file(), str(p))
    check("E2 脚本里含 RENDER_DONE 协议（适配器靠它判断成功）",
          p.is_file() and "RENDER_DONE" in p.read_text(encoding="utf-8"),
          "缺 RENDER_DONE")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")            # type: ignore[attr-defined]
    except Exception:                                        # noqa: BLE001
        pass
    print("=" * 74)
    print("白模生产模块（runtime/previs.py）—— 自检")
    print("=" * 74)
    _spec()
    _warn()
    _fail()
    _first()
    _script()
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
