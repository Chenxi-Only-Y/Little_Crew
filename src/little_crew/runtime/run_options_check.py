"""运行选项公共装配层自检（2026-11-14，A1）。

锁住五件事：
1 `parse_cast_anchors`：列表/字典两种形式；**路径不存在必须报错**（不静默）
2 外观注入：读同目录 `sheet.json` 的 appearance，并带"禁止换装"约束
3 `apply_options`：把语义落到 runner 属性（含 cover/char-sheets/云端模型名）
4 剧本阈值：设置环境变量、0 表示不设
5 主渲染模型选择：默认 z-image-turbo；云端模型可选；未注册报错
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


class _Runner:
    pass


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("运行选项公共装配层（run_options）")
    print("=" * 66)

    from little_crew.runtime import run_options as RO

    print(); print("A. 角色锚点解析")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        png = d / "sheet.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        (d / "sheet.json").write_text(
            json.dumps({"appearance": "灰紫露肩针织短上衣、炭黑阔腿长裤、银色星芒耳饰"},
                       ensure_ascii=False), encoding="utf-8")
        a1 = RO.parse_cast_anchors([f"少女={png}"])
        check("A1 ★★★ 列表形式 NAME=PATH 解析", a1.get("少女") == str(png.resolve()), str(a1))
        a2 = RO.parse_cast_anchors({"少女": str(png)})
        check("A2 ★★ 字典形式解析", a2.get("少女") == str(png.resolve()), str(a2))
        try:
            RO.parse_cast_anchors([f"少女={d / 'nope.png'}"])
            bad_path = False
        except FileNotFoundError:
            bad_path = True
        check("A3 ★★★ 路径不存在 -> 明确报错（不静默忽略）", bad_path, "")
        try:
            RO.parse_cast_anchors(["没有等号"])
            bad_fmt = False
        except ValueError:
            bad_fmt = True
        check("A4 ★★ 缺 NAME=PATH 格式 -> 报错", bad_fmt, "")

        print(); print("B. 外观注入")
        apps = RO.anchor_appearances(a1)
        check("B1 ★★★ 从 sheet.json 读到 appearance", len(apps) == 1
              and "灰紫" in apps[0], str(apps))
        style = RO.style_with_appearances("高精度 3D 半写实国漫 CG", a1)
        check("B2 ★★★ 外观拼进画风且带「禁止换装」", "灰紫" in style
              and "禁止换装" in style and "高精度" in style, style[:80])
        check("B3 ★★ 无锚点时风格原样返回",
              RO.style_with_appearances("X", {}) == "X", "")

        print(); print("C. apply_options 与阈值")
        r = _Runner()
        r.style_hint = "高精度 3D 半写实国漫 CG"
        opts = RO.normalize(cast_anchors=[f"少女={png}"],
                            style_hint="高精度 3D 半写实国漫 CG",
                            render_model="gpt-image-2.6-sunburst",
                            shots_target=12, target_seconds=60,
                            cover=True, cover_title="最后一班",
                            char_sheets=True, vision_max=6,
                            script_threshold=55)
        eff = RO.apply_options(r, opts)
        check("C1 ★★★ runner 属性全部落地",
              r.shots_target == 12
              and abs(r.target_seconds - 60) < 1e-9 and r.cover_enabled is True
              and r.cover_title == "最后一班" and r.character_sheets_enabled is True
              and r.vision_max_shots == 6 and r.cast_anchors.get("少女"), "")
        check("C2 ★★★ 外观注入生效（禁止换装）", "禁止换装" in str(r.style_hint), "")
        check("C3 ★★ 摘要可写进证据", eff.get("cover") is True
              and eff.get("scriptThreshold") == 55.0, str(eff))
        check("C4 ★★★ 阈值写入环境变量",
              os.environ.get("LITTLE_CREW_SCRIPT_THRESHOLD") == "55.0", "")
        RO.set_script_threshold(0)
        os.environ.pop("LITTLE_CREW_SCRIPT_THRESHOLD", None)

        print(); print("D. 主渲染模型选择")
        spec1, be1 = RO.pick_main_spec("")
        check("D1 ★★★ 默认仍是 z-image-turbo（本地）",
              spec1.model_id == "z-image-turbo" and RO.is_local_backend(be1), spec1.model_id)
        spec2, be2 = RO.pick_main_spec("gpt-image-2.6-sunburst")
        check("D2 ★★★ 可切云端模型且被识别为非本地",
              spec2.model_id == "gpt-image-2.6-sunburst"
              and not RO.is_local_backend(be2), str(be2.endpoint))
        try:
            RO.pick_main_spec("no-such-model")
            err_ok = False
        except RuntimeError:
            err_ok = True
        check("D3 ★★ 未注册模型 -> 明确报错", err_ok, "")

    print(); print("E. 渲染路径（用户显式选）")
    o_local = RO.normalize()
    check("E1 ★★★ 默认 local（不会偷偷烧云端）", o_local["render_path"] == "local", "")
    o_cloud = RO.normalize(render_path="cloud")
    check("E2 ★★★ 显式 cloud 才生效", o_cloud["render_path"] == "cloud", "")
    try:
        RO.normalize(render_path="cloudx")
        bad_path = False
    except ValueError:
        bad_path = True
    check("E3 ★★★ 非法路径 -> 报错（不被当成云端）", bad_path, "")
    spec_c, be_c = RO.pick_main_spec("", "cloud")
    check("E4 ★★ 云端路径默认模型是 gpt-image-2.6-sunburst",
          spec_c.model_id == "gpt-image-2.6-sunburst", spec_c.model_id)

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
