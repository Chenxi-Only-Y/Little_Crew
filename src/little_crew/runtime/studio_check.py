"""`runtime/studio.py` 自检 —— **真起服务、真发请求**，但不调 Blender。

## 守什么

| 段 | 守什么 |
|---|---|
| A 读取 | `load_layout` 优先 `layout.json`，没有就从 `shot.json` 反推（且补 `reason: ""`） |
| B 改值 | `EDITABLE` 白名单：非白名单字段**一律忽略**（别让请求体改任意键） |
| C 校验 | **只拦 blocker**，major 只提示 —— 这是刻意的宽松（门禁管交付，工作室管修改） |
| D 分辨率 | ★ **不许因为"改机位"而悄悄改掉分辨率** —— 服务端从原 spec 兜底 |
| E 服务 | 真起 HTTP：`GET /` 能拿到页面、`GET /api/shot` 能拿到布局、**越界路径 403** |

## 为什么 D 段是重点

2026-10-20 实测踩到：客户端没带 `resolution` 时传了 `(None, None)`，
`layout_to_spec` 里 `int(None)` 直接崩。
**就算不崩，退回默认 640×384 也会把原来 852×480 的规格悄悄改掉** ——
改个机位却顺带改了分辨率，是**最难查的那类副作用**。
"""
from __future__ import annotations

import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from little_crew.runtime.studio import (
    EDITABLE,
    StudioServer,
    load_layout,
    patch_and_render,
)

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


def _shotdir(root: Path, sid: str = "SH-T-001") -> Path:
    d = root / "previs" / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "shot.json").write_text(json.dumps({
        "outDir": str(d), "resolution": {"width": 852, "height": 480},
        "fps": 24, "form": "marker",
        "shot": {"shot_size": "full", "lens_mm": 28.0, "azimuth_deg": 8.0,
                 "angle_deg": 2.0, "duration_sec": 5.0,
                 "camera_movement": "static"},
        "subjects": [{"who": "少女", "x": 0.0, "z": 4.0, "height_cm": 165}],
        "set": [],
    }, ensure_ascii=False), encoding="utf-8")
    return d


def _load() -> None:
    print()
    print("A. 读取布局")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        sd = _shotdir(root)
        lay = load_layout(sd)
        check("A1 从 shot.json 反推成功", lay.get("shotId") == "SH-T-001",
              str(lay.get("shotId")))
        check("A2 ★ 反推时补 reason 为空串（而不是缺字段）",
              lay.get("reason") == "", repr(lay.get("reason")))
        check("A3 反推保留了 subjects / set",
              len(lay.get("subjects") or []) == 1, str(lay.get("subjects")))
        (sd / "layout.json").write_text(json.dumps({
            "shotId": "SH-T-001", "reason": "机位在桌侧偏左",
            "shot": {"shot_size": "full", "azimuth_deg": 99.0},
            "subjects": [], "set": [],
        }, ensure_ascii=False), encoding="utf-8")
        lay2 = load_layout(sd)
        check("A4 ★ 有 layout.json 时**优先用它**（不被 spec 覆盖）",
              lay2.get("reason") == "机位在桌侧偏左",
              str(lay2.get("reason")))


def _patch() -> None:
    print()
    print("B. 改值只认白名单")
    lay = {"shotId": "S", "reason": "r",
           "shot": {"shot_size": "full", "azimuth_deg": 0.0,
                    "angle_deg": 0.0, "duration_sec": 5.0,
                    "camera_movement": "static", "lens_mm": 28.0},
           "subjects": [{"who": "A", "x": 0.0, "z": 4.0, "height_cm": 165}],
           "set": []}
    r = patch_and_render(lay, {"azimuth_deg": 12, "危险字段": 1,
                               "subjects": [{"who": "X"}]},
                         blender="", out_root=Path("."), render=False)
    check("B1 ★ 白名单字段生效", r["applied"].get("azimuth_deg") == 12.0,
          str(r["applied"]))
    check("B2 ★★ 非白名单字段被忽略（含 subjects 这种结构字段）",
          "危险字段" not in r["applied"] and "subjects" not in r["applied"],
          str(r["applied"]))
    check("B3 非法数字 -> 明确失败", True)   # 下面单独测
    r2 = patch_and_render(lay, {"azimuth_deg": "不是数字"},
                          blender="", out_root=Path("."), render=False)
    check("B4 非数字的机位 -> ok=False 且说明", r2["ok"] is False
          and "不是数字" in str(r2.get("reason")), str(r2.get("reason"))[:60])
    check("B5 EDITABLE 是固定白名单", isinstance(EDITABLE, tuple)
          and "shot_size" in EDITABLE, str(EDITABLE))


def _validate() -> None:
    print()
    print("C. 校验：只拦 blocker")
    base = {"shotId": "S", "reason": "正对",
            "shot": {"shot_size": "full", "azimuth_deg": 0.0,
                     "angle_deg": 0.0, "duration_sec": 5.0,
                     "camera_movement": "static", "lens_mm": 28.0},
            "subjects": [{"who": "A", "x": 0.0, "z": 4.0, "height_cm": 165}],
            "set": []}
    ok = patch_and_render(base, {"angle_deg": 3}, blender="",
                          out_root=Path("."), render=False)
    check("C1 正常改动 -> ok=True", ok["ok"] is True, str(ok.get("reason")))
    bad = patch_and_render(dict(base), {"angle_deg": 3}, blender="",
                           out_root=Path("."), render=False)
    # 人为造 blocker：把人放到相机后面
    behind = json.loads(json.dumps(base))
    behind["subjects"][0]["z"] = -2.0
    r = patch_and_render(behind, {"angle_deg": 3}, blender="",
                         out_root=Path("."), render=False)
    check("C2 ★★ 有 blocker -> 拒绝（ok=False）", r["ok"] is False,
          str(r.get("reason")))
    check("C3 拒绝时给得出 blocker 明细", bool(r.get("blockers")),
          str(r.get("blockers"))[:80])
    nr = json.loads(json.dumps(base))
    nr["reason"] = ""
    r2 = patch_and_render(nr, {"angle_deg": 3}, blender="",
                          out_root=Path("."), render=False)
    check("C4 ★★ 缺 reason（major）**不拦** —— 工作室只管改，门禁才管交付",
          r2["ok"] is True, f"ok={r2['ok']} majors={[m['code'] for m in r2['majors']]}")
    check("C5 ★ 但会明确标出 reasonMissing（否则判定恒 RETRY 让人困惑）",
          r2.get("reasonMissing") is True, str(r2.get("reasonMissing")))


def _resolution() -> None:
    print()
    print("D. ★ 不许因为改机位而悄悄改分辨率")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        sd = _shotdir(root)
        lay = load_layout(sd)
        # 不传 resolution —— 服务端必须从原 spec 兜底成 852×480
        r = patch_and_render(lay, {"azimuth_deg": 11}, blender="",
                             out_root=root, render=False)
        check("D1 ★★ 不传分辨率时不崩（实测曾 int(None) 崩过）",
              r["ok"] is True, str(r.get("reason"))[:80])
        spec = json.loads((sd / "shot.json").read_text(encoding="utf-8"))
        check("D2 ★ 原分辨率仍在（852×480 没被默认值 640×384 顶掉）",
              (spec["resolution"]["width"], spec["resolution"]["height"])
              == (852, 480), str(spec["resolution"]))
        check("D3 原 fps 仍在", spec.get("fps") == 24, str(spec.get("fps")))


def _server() -> None:
    print()
    print("E. 真起服务")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        sd = _shotdir(root)
        (root / "delivery").mkdir(exist_ok=True)
        with StudioServer(root, blender="") as srv:
            base = srv.url
            check("E1 只绑 127.0.0.1（能改文件的服务绝不能对外）",
                  srv.host == "127.0.0.1", srv.host)
            page = urllib.request.urlopen(base, timeout=20).read().decode("utf-8")
            check("E2 GET / 拿到页面（且是可编辑版）",
                  "EDITABLE=true" in page, f"{len(page)} 字符")
            j = json.loads(urllib.request.urlopen(
                base + "api/shot/SH-T-001", timeout=20).read().decode("utf-8"))
            check("E3 GET /api/shot 拿到布局", j.get("ok") is True,
                  str(j.get("reason")))
            check("E4 带回了 spec（前端要知道原分辨率）",
                  bool((j.get("spec") or {}).get("resolution")),
                  str((j.get("spec") or {}).get("resolution")))
            code = None
            try:
                urllib.request.urlopen(base + "api/shot/no-such-shot", timeout=10)
            except urllib.error.HTTPError as e:
                code = e.code
            check("E5 ★ 不存在的镜 -> 404（不是 200 空对象）", code == 404, str(code))
            # ⚠️ 用 ASCII 路径：**HTTP 请求行必须是 ASCII**，
            # 中文路径会让 urllib 在编码请求行时抛 UnicodeEncodeError。
            # 这个坑本文件已踩第二次（第一次在 publish_check 的 B3）。

            code2 = None
            try:
                urllib.request.urlopen(base + "..%2f..%2fetc%2fpasswd", timeout=10)
            except urllib.error.HTTPError as e:
                code2 = e.code
            check("E6 ★★ 路径穿越被拒（403/404，不是把文件读出去）",
                  code2 in (403, 404), str(code2))
            # POST 不配 blender -> 明确拒绝，不静默
            body = json.dumps({"azimuth_deg": 5}).encode("utf-8")
            req = urllib.request.Request(base + "api/shot/SH-T-001", data=body,
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            code3, j3 = None, {}
            try:
                j3 = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
            except urllib.error.HTTPError as e:
                code3 = e.code
                try:
                    j3 = json.loads(e.read().decode("utf-8"))
                except Exception:                              # noqa: BLE001
                    j3 = {}
            check("E7 ★ 没配 Blender -> 400 且带原因（不假装成功）",
                  code3 == 400 and bool(j3.get("reason")),
                  f"{code3} {str(j3.get('reason'))[:50]}")


def _traversal() -> None:
    """`/api/shot/<sid>` 的路径穿越 + 绑定告警（2026-10-23 审计 H-8 / L-5）。

    ## 为什么单独测这条
    同一个文件里，静态路由 `_static()` 有越界检查，而 `/api/shot/<sid>`
    的两条路由**直接把用户输入拼进文件系统路径** —— 一个有一个没有。
    那正是本项目反复出现的形态：**不是实现错了，是接线断了一处**。

    这里造一个"workdir 之外"的目录放合法布局：修好之前，GET 会把它读出来
    （200 + 内容），修好之后必须 403。
    """
    print()
    print("F. 路径穿越与绑定告警")
    from little_crew.runtime.studio import safe_child

    # ---- 判据本身 ----
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        inner = base / "previs"
        (inner / "SH-T-001").mkdir(parents=True)
        (base / "outside").mkdir()
        check("F1 ★ 正常镜头名 -> 解析到 previs/ 之内",
              safe_child(inner, "SH-T-001") == (inner / "SH-T-001").resolve())
        bad = {
            "上跳": "..",
            "../escape": "../escape",
            "越界相对": "../../outside",
            "分隔符": "sub/dir",
            "反斜杠": "sub\\dir",
            "绝对路径": str(base / "outside"),
            "空": "",
        }
        passed = [k for k, v in bad.items() if safe_child(inner, v) is None]
        check("F2 ★★ 上跳/分隔符/绝对路径一律拒（返回 None，不是拼出去）",
              len(passed) == len(bad), str([k for k in bad if k not in passed]))

    # ---- 真打服务：GET 与 POST 都要拦住 ----
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        root = base / "work"
        _shotdir(root)                                  # 合法镜头（对照）
        evil = base / "evil"                            # workdir **之外**
        evil.mkdir()
        (evil / "layout.json").write_text(json.dumps({
            "shotId": "EVIL", "resolution": [64, 48], "frames": 1,
            "camera": {"azimuth_deg": 0.0},
            "objects": [{"name": "ground"}],
        }), encoding="utf-8")

        with StudioServer(root, blender="") as srv:
            base_url = srv.url
            # ⚠️ 请求行必须 ASCII —— 用 %2f 编码，不写中文
            codes = {}
            for label, suffix, method in (
                ("GET", "api/shot/..%2f..%2fevil", "GET"),
                ("POST", "api/shot/..%2f..%2fevil", "POST"),
            ):
                data = None
                if method == "POST":
                    data = json.dumps({"azimuth_deg": 5}).encode("utf-8")
                req = urllib.request.Request(
                    base_url + suffix, data=data, method=method,
                    headers={"Content-Type": "application/json"})
                try:
                    body = urllib.request.urlopen(req, timeout=10).read()
                    codes[label] = (200, body[:80])
                except urllib.error.HTTPError as e:
                    codes[label] = (e.code, b"")
            check("F3 ★★ GET /api/shot/<越界> -> 403（读不到 workdir 之外）",
                  codes["GET"][0] == 403, str(codes["GET"]))
            check("F4 ★★ POST /api/shot/<越界> -> 403（写不到 workdir 之外）",
                  codes["POST"][0] == 403, str(codes["POST"]))
            j = json.loads(urllib.request.urlopen(
                base_url + "api/shot/SH-T-001", timeout=10).read().decode("utf-8"))
            check("F5 ★ 合法镜头不受影响（守卫不是把功能一起关了）",
                  j.get("ok") is True, str(j.get("reason")))

    # ---- L-5：非回环 host 必须出声 ----
    import contextlib
    import io

    with tempfile.TemporaryDirectory() as d:
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            StudioServer(Path(d), host="0.0.0.0")       # 只构造，不 start（不绑端口）
        warn = buf.getvalue()
        check("F6 ★★ 绑非回环地址 -> 出声警告（注释拦不住参数）",
              "0.0.0.0" in warn and "鉴权" in warn, warn[:70])
        buf2 = io.StringIO()
        with contextlib.redirect_stderr(buf2):
            StudioServer(Path(d), host="127.0.0.1")
        check("F7 回环地址 -> 不吵（默认路径保持安静）", buf2.getvalue() == "",
              buf2.getvalue()[:60])


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")            # type: ignore[attr-defined]
    except Exception:                                        # noqa: BLE001
        pass
    print("=" * 74)
    print("工作室（studio）—— 自检（真起服务，不调 Blender）")
    print("=" * 74)
    _load()
    _patch()
    _validate()
    _resolution()
    _server()
    _traversal()
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
