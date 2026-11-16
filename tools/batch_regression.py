"""20 集批量回归（2026-11-16）：验证**平台本体稳定性**，不是内容质量。

## 口径

| 项 | 说明 |
|---|---|
出图 | `--render-path cloud`（Sudorelay ¥0.05/张）或 `local` |
链路 | 出图 → KenBurns 合成 →（可选）配音 →（可选）口型；**不含视频模型** |
语料 | 确定性生成（`little_crew.cli.regression_corpus`） |
指标 | 成功率 / 覆盖率 / 硬规则 / 出图成本 / 墙钟 / 报告页 / 失败分类 |
对照 | 2 集 `full` + 1 集 `minimal` 编制（验证跳过 ≠ 通过） |

产物：`research/regression/<date>/`（stories + summary）
每集工作目录：`outputs/_regression/<date>/eps/`（**不进仓库**）
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable or "python"
sys.path.insert(0, str(ROOT / "src"))

from little_crew.cli.regression_corpus import make_story  # noqa: E402

def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:                                         # noqa: BLE001
        return {}


def _env() -> dict:
    e = dict(os.environ)
    e["PYTHONPATH"] = str(ROOT / "src")
    e["PYTHONIOENCODING"] = "utf-8"
    e["LITTLE_CREW_ALLOW_UNVERIFIED"] = "1"
    # 回归明确**不接视频模型**（视频能力可选、默认关）
    e.pop("LITTLE_CREW_VIDEO_MODEL", None)
    e.pop("LITTLE_CREW_NEED_VOICE", None)
    return e


def _one(ep: int, *, out: Path, work_base: Path, shots: int,
         render_path: str, profile: str, voice: bool,
         lipsync: bool, timeout: int) -> dict:
    sid = f"REG-E{ep:02d}"
    wd = work_base / f"E{ep:02d}"
    story = out / "stories" / f"E{ep:02d}.txt"
    args = [PY, "-X", "utf8", "-m", "little_crew.runtime.episode_demo",
            "--story-file", str(story), "--workdir", str(wd),
            "--episode-id", sid, "--shots-target", str(shots),
            "--render-path", render_path, "--crew-profile", profile,
            "--accept-script"]
    if voice:
        args.append("--voice")
    if lipsync:
        args.append("--lipsync")
    t0 = time.time()
    try:
        p = subprocess.run(args, cwd=str(ROOT), env=_env(),
                           capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        rc, tail = p.returncode, (p.stdout or "")[-800:]
    except subprocess.TimeoutExpired:
        rc, tail = -9, f"timeout>{timeout}s"
    wall = round(time.time() - t0, 1)

    rep = _read(wd / "delivery" / "run.report.json")
    calls = _read(wd / "delivery" / "image_calls.json")
    lip = _read(wd / "delivery" / "lipsync.json")
    crew = rep.get("crew") or {}
    cloud = (calls.get("cloud") or {}) if isinstance(calls, dict) else {}
    m = {
        "episode": f"E{ep:02d}", "episodeId": sid, "rc": rc,
        "ok": bool(rep.get("ok")) and rc == 0,
        "coverage": rep.get("coverage"),
        "shots": rep.get("rendered") or rep.get("shotCount"),
        "wallSeconds": rep.get("wallSeconds") or wall,
        "cacheHits": rep.get("cacheHits"),
        "imageCny": round(float(cloud.get("estCostCny") or 0.0), 2),
        "lipsyncCny": round(float((lip or {}).get("costCny") or 0.0), 2),
        "crewPreset": crew.get("preset"), "crewLevel": crew.get("level"),
        "profile": profile, "renderPath": render_path,
        "voice": bool(voice), "lipsync": bool(lipsync),
        "failure": "" if (rc == 0 and rep.get("ok")) else (
            str(rep.get("reason") or "")[:200] or tail[-200:]),
    }
    # 报告页（顺便验证报告生成链路）
    try:
        r = subprocess.run([PY, "-X", "utf8", "-m", "little_crew.cli", "report",
                            str(wd)], cwd=str(ROOT), env=_env(),
                           capture_output=True, text=True, timeout=120)
        m["report"] = "report/index.html" if (wd / "report" / "index.html").is_file() else ""
        m["reportOk"] = r.returncode == 0 and bool(m["report"])
    except Exception:                                         # noqa: BLE001
        m["report"] = ""
        m["reportOk"] = False
    return m


def _summary(rows: list[dict], *, out: Path, render_path: str,
             shots: int) -> None:
    main = [r for r in rows if r["episode"] != "E21" and r["episode"] != "E22"
            and r["episode"] != "E23"]
    ctrl = [r for r in rows if r not in main]
    okN = sum(1 for r in main if r["ok"] and r.get("reportOk"))
    cost = round(sum(r["imageCny"] + r["lipsyncCny"] for r in rows), 2)
    wall = round(sum(float(r["wallSeconds"] or 0) for r in rows), 1)
    fails = [r for r in main if not (r["ok"] and r.get("reportOk"))]
    (out / "summary.json").write_text(json.dumps(
        {"kind": "batch.regression", "date": out.name,
         "renderPath": render_path, "shotsTarget": shots,
         "videoMode": "not-used", "episodes": len(main),
         "success": okN, "costCny": cost, "wallSeconds": wall,
         "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")

    L = [f"# 20 集批量回归 · {out.name}", "",
         "> 语料为脚本内**确定性生成的回归语料**（不是真实剧本）；"
         "**视频模型未接入/未使用**（可选能力，默认关）。", "",
         "| 指标 | 值 |", "|---|---|",
         f"| 渲染路径 | {render_path} |",
         f"| 每集目标镜数 | {shots} |",
         f"| 主批集数 | {len(main)} |",
         f"| **成功（含报告页）** | **{okN}/{len(main)}** |",
         f"| 总成本 | **¥{cost:.2f}** |",
         f"| 总墙钟 | {wall:.1f}s |",
         f"| 视频模型 | 未使用（可选） |", ""]
    if fails:
        L += ["## 失败分类", "", "| 集 | rc | 原因 |", "|---|---|---|"]
        for r in fails:
            L.append(f"| {r['episode']} | {r['rc']} | {str(r.get('failure'))[:120]} |")
        L.append("")
    L += ["## 逐集", "",
          "| 集 | 编制 | 镜数 | 覆盖 | 墙钟s | 图¥ | 报告 | 结果 |",
          "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        res = "✅" if (r["ok"] and r.get("reportOk")) else "❌"
        L.append(f"| {r['episode']} | {r.get('profile')}/{r.get('crewLevel')} "
                 f"| {r.get('shots')} | {r.get('coverage')} | {r.get('wallSeconds')} "
                 f"| {r.get('imageCny')} | {'✓' if r.get('reportOk') else '✗'} | {res} |")
    L += ["", "## 对照", ""]
    for r in ctrl:
        L.append(f"- {r['episode']}（{r.get('profile')}）："
                 f"{'✅' if r['ok'] else '❌'} · 覆盖 {r.get('coverage')} · "
                 f"¥{r.get('imageCny')} · 编制级别 {r.get('crewLevel')}")
    (out / "summary.md").write_text("\n".join(L), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--episodes", type=int, default=20)
    ap.add_argument("--shots", type=int, default=6)
    ap.add_argument("--render-path", default="cloud")
    ap.add_argument("--profile", default="standard")
    ap.add_argument("--voice-first", type=int, default=5,
                    help="前 N 集加 --voice（验证音轨链路）")
    ap.add_argument("--lipsync-episode", type=int, default=1,
                    help="第 N 集加 --lipsync（0=不做）")
    ap.add_argument("--controls", action="store_true", default=True,
                    help="跑 2 集 full + 1 集 minimal 对照")
    ap.add_argument("--no-controls", dest="controls", action="store_false")
    ap.add_argument("--resume", action="store_true",
                    help="跳过已成功项（读 summary.json）")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--out", default="")
    ap.add_argument("--workdir-base", default="",
                    help="每集工作目录根（默认 outputs/_regression/<date>/eps，"
                         "不进仓库）")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")              # type: ignore[attr-defined]
    except Exception:                                         # noqa: BLE001
        pass

    date = _dt.date.today().isoformat()
    out = Path(a.out) if a.out else ROOT / "research" / "regression" / date
    work_base = (Path(a.workdir_base) if a.workdir_base
                 else ROOT / "outputs" / "_regression" / date / "eps")
    work_base.mkdir(parents=True, exist_ok=True)
    (out / "stories").mkdir(parents=True, exist_ok=True)
    for i in range(1, a.episodes + 1):
        (out / "stories" / f"E{i:02d}.txt").write_text(make_story(i), encoding="utf-8")

    rows: list[dict] = []
    done: set[str] = set()
    if a.resume and (out / "summary.json").is_file():
        old = _read(out / "summary.json")
        for r in (old.get("rows") or []):
            rows.append(r)
            if r.get("ok") and r.get("reportOk"):
                done.add(str(r.get("episode")))
    elif a.force:
        rows = []

    print(f"  回归输出：{out}（报告/交付物）")
    print(f"  工作目录：{work_base}（不进仓库）")
    print(f"  口径：{a.render_path} · 每集 {a.shots} 镜 · 编制 {a.profile} · "
          f"视频模型 not-used（可选）")
    t0 = time.time()
    for i in range(1, a.episodes + 1):
        ep = f"E{i:02d}"
        if ep in done:
            print(f"  ↷ {ep} 已成功（续跑跳过）")
            continue
        voice = i <= a.voice_first
        lipsync = (a.lipsync_episode and i == a.lipsync_episode)
        m = _one(i, out=out, work_base=work_base, shots=a.shots,
                 render_path=a.render_path,
                 profile=a.profile, voice=voice, lipsync=bool(lipsync),
                 timeout=a.timeout)
        rows = [r for r in rows if r.get("episode") != ep] + [m]
        _summary(rows, out=out, render_path=a.render_path, shots=a.shots)
        mark = "✅" if (m["ok"] and m.get("reportOk")) else "❌"
        print(f"  {mark} {ep} rc={m['rc']} 覆盖={m['coverage']} "
              f"图¥{m['imageCny']} 墙钟={m['wallSeconds']}s"
              + (f" · {str(m.get('failure'))[:80]}" if not m["ok"] else ""),
              flush=True)

    # ---- 对照：2 集 full + 1 集 minimal ----
    if a.controls:
        for k, (ep_i, prof) in enumerate(((a.episodes + 1, "full"),
                                          (a.episodes + 2, "full"),
                                          (a.episodes + 3, "minimal")), 1):
            epname = f"E{ep_i:02d}"
            (out / "stories" / f"{epname}.txt").write_text(
                make_story(ep_i), encoding="utf-8")
            m = _one(ep_i, out=out, work_base=work_base, shots=a.shots,
                     render_path=a.render_path, profile=prof,
                     voice=False, lipsync=False, timeout=a.timeout)
            rows = [r for r in rows if r.get("episode") != epname] + [m]
            _summary(rows, out=out, render_path=a.render_path, shots=a.shots)
            print(f"  对照 {epname}（{prof}）：{'✅' if m['ok'] else '❌'} "
                  f"覆盖={m['coverage']} 编制级别={m['crewLevel']}", flush=True)

    _summary(rows, out=out, render_path=a.render_path, shots=a.shots)
    main_rows = [r for r in rows if r["episode"] not in ("E21", "E22", "E23")]
    okN = sum(1 for r in main_rows if r["ok"] and r.get("reportOk"))
    print(f"  完成：{okN}/{len(main_rows)} 成功 · "
          f"总成本 ¥{sum(r['imageCny'] + r['lipsyncCny'] for r in rows):.2f} · "
          f"墙钟 {time.time() - t0:.0f}s")
    print(f"  汇总：{out / 'summary.md'}")
    return 0 if okN == len(main_rows) and len(main_rows) >= a.episodes else 1


if __name__ == "__main__":
    raise SystemExit(main())
