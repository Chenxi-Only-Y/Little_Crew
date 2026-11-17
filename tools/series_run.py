"""多集顺序跑（2026-11-17）：把 `series/` 台账 + 当前剧集接起来。

```
scripts/E01.txt, E02.txt, ...  ->  逐集跑 episode_demo（带系列台账）
                                  ->  scene views 决策/复用/升档
                                  -> 回写 series/series.json
```

用法：
```powershell
$env:PYTHONPATH="<项目>\\src"
python tools/series_run.py --series-dir research/series/最后一班-迷你 \
    --scripts-dir research/series/最后一班-迷你/scripts \
    --shots 2 --render-path cloud --scene-views --hero-max 1
```
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _episode_id(stem: str) -> str:
    s = stem.strip()
    return s if s else "EP"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--series-dir", required=True)
    ap.add_argument("--scripts-dir", default="")
    ap.add_argument("--episodes", type=int, default=0, help="跑前 N 集（0=全部）")
    ap.add_argument("--shots", type=int, default=2)
    ap.add_argument("--render-path", default="cloud")
    ap.add_argument("--crew-profile", default="standard")
    ap.add_argument("--scene-views", action="store_true", default=False)
    ap.add_argument("--hero-max", type=int, default=2)
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--resume", action="store_true", default=True)
    ap.add_argument("--out-root", default="")
    ap.add_argument("--mode", choices=["serial", "parallel", "hybrid"], default="serial",
                    help="执行模式：serial 串行；parallel 并行；hybrid 并行+边界串行审")
    ap.add_argument("--plan", default="", help="chapter_plan.json（章卡/契约/依赖）")
    ap.add_argument("--boundary-vlm", action="store_true",
                    help="边界画面也用 VLM 审（默认只审契约）")
    ap.add_argument("--timeout", type=int, default=1800)
    a = ap.parse_args()

    series_dir = Path(a.series_dir)
    scripts_dir = Path(a.scripts_dir) if a.scripts_dir else series_dir / "scripts"
    if not scripts_dir.is_dir():
        print(f"  ❌ 找不到剧本目录：{scripts_dir}")
        return 2
    scripts = sorted(scripts_dir.glob("*.txt"))
    if a.episodes > 0:
        scripts = scripts[:a.episodes]
    if not scripts:
        print("  ❌ 目录里没有 *.txt 剧本")
        return 2
    plan: dict = {}
    if a.plan:
        plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
        from little_crew.runtime.chapter_plan import topo_order, validate_plan
        _errs = validate_plan(plan)
        if _errs:
            print("  ❌ chapter_plan 校验失败：", _errs[:5])
            return 2
        _order = topo_order(plan)
        _by_id = {str(c.get("chapterId")): c for c in plan.get("chapters") or []}
        _files = {s.stem: s for s in scripts}
        _ordered = []
        for cid in _order:
            f = _files.get(cid)
            if f is None:
                print(f"  ❌ 找不到章节 {cid} 的剧本文件")
                return 2
            _ordered.append((_by_id[cid], f))
        scripts = [f for _c, f in _ordered]
    out_root = Path(a.out_root) if a.out_root else ROOT / "outputs" / "_series" / series_dir.name
    out_root.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    env["LITTLE_CREW_ALLOW_UNVERIFIED"] = "1"
    env["LITTLE_CREW_SERIES_DIR"] = str(series_dir)
    if a.mode in ("parallel", "hybrid"):
        # B3：先把各章并行跑到"完成"，再由主循环按顺序做边界审（resume 分支）
        import concurrent.futures as _cf
        _jobs: list[tuple[str, Path, list[str]]] = []
        for _s in scripts:
            _ep = _episode_id(_s.stem)
            _wd = out_root / _ep
            if a.resume and (_wd / "delivery" / "series.json").is_file():
                continue
            _cmd = [sys.executable, "-X", "utf8", "-m",
                    "little_crew.runtime.episode_demo",
                    "--story-file", str(_s), "--workdir", str(_wd),
                    "--episode-id", _ep, "--shots-target", str(a.shots),
                    "--render-path", a.render_path,
                    "--crew-profile", a.crew_profile, "--accept-script",
                    "--series-dir", str(series_dir),
                    "--scene-views-hero-max", str(a.hero_max)]
            if a.scene_views:
                _cmd.append("--scene-views")
            if a.no_llm:
                _cmd.append("--scene-views-no-llm")
            _ch0 = next((c for c in (plan.get("chapters") or ())
                         if str(c.get("chapterId")) == _ep), None) if plan else None
            if _ch0 is not None:
                from little_crew.runtime.chapter_plan import build_brief as _bb
                _bp = _wd / "episode.brief.json"
                _wd.mkdir(parents=True, exist_ok=True)
                _bp.write_text(json.dumps(_bb(plan, _ep), ensure_ascii=False,
                                          indent=2), encoding="utf-8")
                _cmd += ["--episode-brief", str(_bp)]
            _jobs.append((_ep, _wd, _cmd))
        print(f"  并行模式：{len(_jobs)} 集同时跑（边界审仍串行）")
        def _run_job(job):
            ep0, wd0, cmd0 = job
            try:
                pr = subprocess.run(cmd0, cwd=str(ROOT), env=env,
                                    capture_output=True, text=True,
                                    encoding="utf-8", errors="replace",
                                    timeout=a.timeout)
                return ep0, pr.returncode
            except Exception as e:                            # noqa: BLE001
                return ep0, f"ERR:{e}"
        with _cf.ThreadPoolExecutor(max_workers=max(1, min(4, len(_jobs)))) as _ex:
            for ep0, rc0 in _ex.map(_run_job, _jobs):
                print(f"  并行完成 {ep0} rc={rc0}", flush=True)

    rows: list[dict] = []
    prev_exit: dict | None = None
    prev_frames: list[str] = []
    boundaries: list[dict] = []
    injection: dict = {"kind": "pilot.injection", "checks": []}
    t_all = time.time()
    for _idx, script in enumerate(scripts):
        _ch = None
        if plan:
            _cid = _episode_id(script.stem)
            _ch = next((c for c in plan.get("chapters") or ()
                        if str(c.get("chapterId")) == _cid), None)
        ep = _episode_id(script.stem)
        wd = out_root / ep
        marker = wd / "delivery" / "series.json"
        if a.resume and marker.is_file():
            try:
                rep = json.loads((wd / "delivery" / "run.report.json")
                                 .read_text(encoding="utf-8")) if (wd / "delivery" / "run.report.json").is_file() else {}
            except Exception:                                # noqa: BLE001
                rep = {}
            if rep.get("ok"):
                sv = {}
                if (wd / "delivery" / "scene_views.json").is_file():
                    try:
                        sv = json.loads((wd / "delivery" / "scene_views.json").read_text(encoding="utf-8"))
                    except Exception:                        # noqa: BLE001
                        sv = {}
                rows.append({"episodeId": ep, "rc": 0, "ok": True, "resumed": True,
                             "shots": rep.get("rendered"), "coverage": rep.get("coverage"),
                             "wallS": rep.get("wallSeconds"),
                             "sceneCostCny": sv.get("costCny"),
                             "reusedScenes": sum(1 for v in (sv.get("scenes") or {}).values()
                                                 if v.get("reused"))})
                if plan and _ch is not None:
                    from little_crew.runtime.boundary_audit import audit_contract
                    _bc = audit_contract(prev_exit, _ch.get("entry")) \
                        if prev_exit is not None else {
                            "kind": "boundary.contract", "level": "green",
                            "ok": True, "diffs": [], "note": "第一章无前序"}
                    _level = _bc.get("level", "green")
                    boundaries.append({"kind": "boundary.audit", "chapterId": ep,
                                       "level": _level, "contract": _bc,
                                       "visual": {"level": "skip"},
                                       "retakeScope": "boundary_shots_only"
                                       if _level == "red" else ""})
                    prev_exit = _ch.get("exit")
                    rows[-1]["boundary"] = _level
                    if _level == "red":
                        try:
                            from little_crew.runtime import boundary_retake as _brt2
                            _rp2 = _brt2.plan({"level": _level}, wd)
                            _brt2.write(wd, _rp2)
                            rows[-1]["retakePlanned"] = True
                        except Exception:                # noqa: BLE001
                            pass
                print(f"  ↷ {ep} 已成功（续跑跳过）")
                continue
        cmd = [sys.executable, "-X", "utf8", "-m", "little_crew.runtime.episode_demo",
               "--story-file", str(script), "--workdir", str(wd),
               "--episode-id", ep, "--shots-target", str(a.shots),
               "--render-path", a.render_path, "--crew-profile", a.crew_profile,
               "--accept-script", "--series-dir", str(series_dir),
               "--scene-views-hero-max", str(a.hero_max)]
        if a.scene_views:
            cmd.append("--scene-views")
        if a.no_llm:
            cmd.append("--scene-views-no-llm")
        _brief_path = wd / "episode.brief.json"
        if _ch is not None:
            from little_crew.runtime.chapter_plan import build_brief
            _brief = build_brief(plan, str(_ch.get("chapterId")))
            wd.mkdir(parents=True, exist_ok=True)
            _brief_path.write_text(json.dumps(_brief, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
            cmd += ["--episode-brief", str(_brief_path)]
        t0 = time.time()
        try:
            p = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=a.timeout)
            rc, tail = p.returncode, (p.stdout or "")[-600:]
        except subprocess.TimeoutExpired:
            rc, tail = -9, f"timeout>{a.timeout}s"
        wall = round(time.time() - t0, 1)
        rep: dict = {}
        rp = wd / "delivery" / "run.report.json"
        if rp.is_file():
            try:
                rep = json.loads(rp.read_text(encoding="utf-8"))
            except Exception:                                # noqa: BLE001
                rep = {}
        sv: dict = {}
        sp = wd / "delivery" / "scene_views.json"
        if sp.is_file():
            try:
                sv = json.loads(sp.read_text(encoding="utf-8"))
            except Exception:                                # noqa: BLE001
                sv = {}
        ok = bool(rep.get("ok")) and rc == 0
        row = {"episodeId": ep, "rc": rc, "ok": ok,
               "shots": rep.get("rendered"), "coverage": rep.get("coverage"),
               "wallS": rep.get("wallSeconds") or wall,
               "sceneCostCny": sv.get("costCny"),
               "tierSource": (sv.get("tierPlan") or {}).get("source"),
               "reusedScenes": sum(1 for v in (sv.get("scenes") or {}).values()
                                   if v.get("reused")),
               "sceneFailed": sv.get("failed"),
               "failure": "" if ok else (str(rep.get("reason") or "")[:160] or tail[-160:])}
        # ---- 边界审（上一章 exit vs 本章 entry；画面可选 VLM）----
        if ok and plan and _ch is not None:
            from little_crew.runtime.boundary_audit import audit_contract, audit_visual
            _cur_entry = _ch.get("entry")
            _bc = audit_contract(prev_exit, _cur_entry) if prev_exit is not None else {
                "kind": "boundary.contract", "level": "green", "ok": True,
                "diffs": [], "note": "第一章无前序"}
            _cur_frames = sorted(str(x) for x in (wd / "assets").rglob("*.png")) if (wd / "assets").is_dir() else []
            _bv = {"kind": "boundary.visual", "level": "skip", "ok": None,
                   "diffs": ["未开启 VLM 或缺少帧"], "judge": ""}
            if a.boundary_vlm and prev_frames and _cur_frames:
                _bv = audit_visual(prev_frames, _cur_frames, composite=wd / "boundary_compare.jpg")
            _level = "red" if "red" in (_bc.get("level"), _bv.get("level")) else (
                "yellow" if "yellow" in (_bc.get("level"), _bv.get("level")) else "green")
            _bd = {"kind": "boundary.audit", "chapterId": ep, "level": _level,
                   "contract": _bc, "visual": _bv,
                   "retakeScope": "boundary_shots_only" if _level == "red" else ""}
            (wd / "delivery").mkdir(parents=True, exist_ok=True)
            (wd / "delivery" / "boundary.json").write_text(
                json.dumps(_bd, ensure_ascii=False, indent=2), encoding="utf-8")
            boundaries.append(_bd)
            row["boundary"] = _level
            if _level == "red":
                try:
                    from little_crew.runtime import boundary_retake as _brt
                    _rp = _brt.plan(_bd, wd)
                    _brt.write(wd, _rp)
                    row["retakePlanned"] = bool(_rp.get("needRetake"))
                    row["retakeShots"] = _rp.get("retakeShots")
                except Exception:                        # noqa: BLE001
                    pass
            if _level == "red":
                row["failure"] = (row.get("failure") or "") + " 边界审红项：只重拍边界镜"
            prev_exit = _ch.get("exit")
            prev_frames = _cur_frames[-2:] if _cur_frames else []
        rows.append(row)
        mark = "✅" if ok else "❌"
        print(f"  {mark} {ep} rc={rc} 覆盖={row['coverage']} 墙钟={row['wallS']}s "
              f"场景¥{row.get('sceneCostCny')} 复用{row.get('reusedScenes')}场"
              + ("" if ok else f" · {row['failure'][:80]}"), flush=True)
        if not ok and rep:
            pass

    total_cost = round(sum(float((r.get("sceneCostCny") or 0)) for r in rows), 2)
    summary = {"kind": "series.run", "seriesDir": str(series_dir),
               "episodes": len(rows), "ok": sum(1 for r in rows if r.get("ok")),
               "wallS": round(time.time() - t_all, 1),
               "sceneCostCny": total_cost,
               "reusedScenes": sum(int(r.get("reusedScenes") or 0) for r in rows),
               "boundaries": boundaries,
               "injectionOk": (injection.get("ok") if plan else None),
               "rows": rows}
    # ---- 注入错误验证（R1）：故意破坏契约，必须检出 ----
    if plan:
        from little_crew.runtime.boundary_audit import audit_contract
        _chs = [dict(c) for c in (plan.get("chapters") or [])]
        if len(_chs) >= 2:
            _mut = json.loads(json.dumps(plan))
            _mut["chapters"][1]["entry"] = dict(_mut["chapters"][1].get("entry") or {})
            _mut["chapters"][1]["entry"]["characters"] = []
            _mut["chapters"][1]["entry"]["placeKey"] = "错误地点"
            _d = audit_contract(plan["chapters"][0].get("exit"),
                                _mut["chapters"][1].get("entry"))
            injection["checks"].append({
                "case": "人物消失+地点改变",
                "detected": _d["level"] == "red",
                "diffs": _d["diffs"]})
        injection["ok"] = bool(injection["checks"]) and all(
            c["detected"] for c in injection["checks"])
        (series_dir / "injection-report.json").write_text(
            json.dumps(injection, ensure_ascii=False, indent=2), encoding="utf-8")
    if plan:
        summary["injectionOk"] = injection.get("ok")
    (series_dir / "boundary-summary.json").write_text(
        json.dumps({"kind": "pilot.boundaries", "boundaries": boundaries},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (series_dir / "run-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 多集运行汇总", "",
             f"- 剧集目录：`{series_dir}`",
             f"- 成功：**{summary['ok']}/{summary['episodes']}**",
             f"- 场景视图成本：**¥{total_cost}** · 复用场景 {summary['reusedScenes']} 场",
             f"- 总墙钟：{summary['wallS']}s", "",
             "| 集 | 结果 | 覆盖 | 墙钟s | 场景¥ | 复用场 | 档位来源 |",
             "|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['episodeId']} | {'✅' if r.get('ok') else '❌'} "
                     f"| {r.get('coverage')} | {r.get('wallS')} "
                     f"| {r.get('sceneCostCny')} | {r.get('reusedScenes')} "
                     f"| {r.get('tierSource') or ''} |")
    (series_dir / "run-summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"  --- {summary['ok']}/{summary['episodes']} 成功 · 场景¥{total_cost} · "
          f"墙钟 {summary['wallS']}s")
    print(f"  汇总：{series_dir / 'run-summary.md'}")
    return 0 if summary["ok"] == summary["episodes"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
