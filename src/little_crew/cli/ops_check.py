"""运维 CLI 自检（2026-11-12）：`doctor` / `library` / `report`。

不依赖真服务：doctor 用环境变量造场景；library 用临时目录造假报告；
report 用临时 workdir 造最小交付。
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


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("运维 CLI（doctor / library / report）—— 自检")
    print("=" * 66)

    print(); print("A. doctor 结构")
    from little_crew.cli.doctor import run_checks

    old = {k: os.environ.get(k) for k in ("COMFYUI_ENDPOINT", "BREEZE_TTS_ENDPOINT")}
    os.environ["COMFYUI_ENDPOINT"] = "http://127.0.0.1:59999"
    os.environ["BREEZE_TTS_ENDPOINT"] = "http://127.0.0.1:59998"
    try:
        checks = run_checks()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    names = [c["name"] for c in checks]
    check("A1 ★★★ 覆盖六项（服务/key/模型/磁盘/ffmpeg）",
          set(names) >= {"ComfyUI", "TTS", "LLM key", "模型矩阵", "输出目录", "FFmpeg"},
          str(names))
    check("A2 ★★★ 服务不通 -> 标记为阻塞",
          any(c["name"] == "ComfyUI" and c["blocking"] for c in checks),
          str([c for c in checks if c["name"] == "ComfyUI"])[:80])
    check("A3 ★★ 模型矩阵不该阻塞（只是提示）",
          not any(c["name"] == "模型矩阵" and c["blocking"] for c in checks), "")

    print(); print("B. library 读取")
    from little_crew.cli.library import collect

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for i, ok in enumerate((True, False)):
            d = root / f"_E{i}"
            (d / "delivery").mkdir(parents=True)
            (d / "delivery" / "run.report.json").write_text(json.dumps({
                "episodeId": f"E{i}", "ok": ok, "plannedShots": 10,
                "coverage": 1.0, "wallSeconds": 600,
                "video": {"seconds": 100.0}, "scriptVerdict": "PASS",
            }), encoding="utf-8")
        rows = collect(root)
        check("B1 ★★★ 两集都能列出", len(rows) == 2, str(len(rows)))
        check("B2 ★★★ ok 状态如实（一真一假）",
              sorted(r["ok"] for r in rows) == [False, True],
              str([r["ok"] for r in rows]))
        check("B3 ★★ 墙钟换算成分钟（600s -> 10 分）",
              any(r["wallMinutes"] == 10.0 for r in rows),
              str([r["wallMinutes"] for r in rows]))
        check("B4 ★★ 每行标注来源文件（可核对）",
              all(r["source"].endswith("run.report.json") for r in rows), "")
        rows2 = [r for r in rows if not r["ok"]]
        check("B5 ★★ --failed 语义：只剩未交付", len(rows2) == 1, str(len(rows2)))

    print(); print("C. report 生成")
    from little_crew.runtime.report_html import build_report

    with tempfile.TemporaryDirectory() as td2:
        wd = Path(td2)
        (wd / "delivery").mkdir()
        (wd / "delivery" / "run.report.json").write_text(json.dumps({
            "episodeId": "ERPT", "ok": True, "plannedShots": 3,
            "coverage": 1.0, "video": {"seconds": 30.0},
        }), encoding="utf-8")
        page = build_report(wd)
        check("C1 ★★★ 生成了 HTML 文件", page.is_file(), str(page))
        html = page.read_text(encoding="utf-8")
        check("C2 ★★★ 页面里认得出是哪一集", "ERPT" in html, html[:60])
        check("C3 ★★ 是完整 HTML（有 <html> 与 </html>）",
              "<html" in html.lower() and "</html>" in html.lower(), "")

    print(); print("D. 打包与任务入口（2026-11-12）")
    root = Path(__file__).resolve().parents[3]
    check("D1 ★★★ Dockerfile 存在", (root / "Dockerfile").is_file(), "")
    check("D2 ★★★ Dockerfile 声明了 CMD（能直接起服务）",
          "CMD" in (root / "Dockerfile").read_text(encoding="utf-8")
          if (root / "Dockerfile").is_file() else False, "")
    mf = root / "Makefile"
    mk = mf.read_text(encoding="utf-8") if mf.is_file() else ""
    check("D3 ★★★ Makefile 有 doctor/test/episode/lib/report 目标",
          all(f"\n{t}:" in mk for t in ("doctor", "test", "episode", "lib", "report")),
          str([t for t in ("doctor", "test", "episode", "lib", "report") if f"\n{t}:" not in mk]))
    check("D4 ★★ make.ps1 存在且带 Set-Location（相对路径可解析）",
          (root / "make.ps1").is_file()
          and "Set-Location" in (root / "make.ps1").read_text(encoding="utf-8"), "")
    check("D5 ★★ .dockerignore 存在（别把 outputs/research 打进镜像）",
          (root / ".dockerignore").is_file(), "")

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
