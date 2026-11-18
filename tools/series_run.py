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


def _boundary_retake_judge(workdir: Path, prev_frames: list[str],
                           cur_frames: list[str]):
    """B2 自动循环的复审判官：真调 `boundary_audit.audit_visual`（VLM）。

    - 本章边界帧优先取本次重拍 shot 的 shot manifest `out`（真补拍产物）
    - 取不到再退回本章归档帧 `sorted(png)`（调用方已排好序）
    - 注意：这里会真的调云端 VLM（只有 `--boundary-vlm` 且两章都有帧时才会接线）
    """
    from little_crew.runtime import shot_manifest as _sm
    from little_crew.runtime.boundary_audit import audit_visual

    def judge(ctx: dict) -> dict:
        wd = Path(str(workdir))
        frames: list[str] = []
        for sid in list(ctx.get("shots") or ()):
            rec = _sm.load(wd, str(sid)) or {}
            out = Path(str(rec.get("out") or ""))
            if out.is_file():
                frames.append(str(out))
        if not frames:
            frames = list(cur_frames)
        return audit_visual(prev_frames, frames,
                            composite=wd / "boundary_retake_compare.jpg")

    return judge


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
    ap.add_argument("--retake-red", action="store_true",
                    help="边界红项自动只补拍边界镜（B2 自动循环，需要 shot manifest）")
    ap.add_argument("--retake-max", type=int, default=2,
                    help="边界红自动补拍最多尝试次数（默认 2；0=不自动补拍只升级）")
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
    # #8：并行模式下 t_all 要在"并行跑各集"之前起表，否则 summary.wallS 只算到
    # 并行结束后的串行收尾（实测会成 0.0s，不真实）。
    t_all = time.time()
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
        # 并行安全：episode.py 回写系列台账改为"按集增量 + 跨进程合并锁"
        # （series_bible.merge_episode），所以场景视图可以与多集同时开。
        if a.scene_views:
            _sv_note = ("并行开场景视图（LLM 档位规划）" if not a.no_llm
                        else "并行开场景视图（no-LLM/启发式档位）")
            _sv_note += f"，hero-max={a.hero_max}"
        else:
            _sv_note = "场景视图未开"
        print(f"  并行模式：{len(_jobs)} 集同时跑（{_sv_note}；"
              f"系列台账按集写+合并锁；边界审仍串行）")
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
    for _idx, script in enumerate(scripts):
        _ch = None
        if plan:
            _cid = _episode_id(script.stem)
            _ch = next((c for c in plan.get("chapters") or ()
                        if str(c.get("chapterId")) == _cid), None)
        ep = _episode_id(script.stem)
        wd = out_root / ep
        _retake_doc: dict = {}
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
                    _bd2 = {"kind": "boundary.audit", "chapterId": ep,
                            "level": _level, "contract": _bc,
                            "visual": {"level": "skip", "ok": None,
                                       "diffs": [], "judge": ""},
                            "retakeScope": "boundary_shots_only"
                            if _level == "red" else ""}
                    boundaries.append(_bd2)
                    (wd / "delivery").mkdir(parents=True, exist_ok=True)
                    (wd / "delivery" / "boundary.json").write_text(
                        json.dumps(_bd2, ensure_ascii=False, indent=2),
                        encoding="utf-8")
                    prev_exit = _ch.get("exit")
                    rows[-1]["boundary"] = _level
                    if _level == "red":
                        try:
                            from little_crew.runtime import boundary_retake as _brt2
                            _rp2 = _brt2.plan(_bd2, wd)
                            _brt2.write(wd, _rp2)
                            rows[-1]["retakePlanned"] = bool(_rp2.get("needRetake"))
                            rows[-1]["retakeShots"] = _rp2.get("retakeShots")
                            if a.retake_red:
                                # 续跑没有本次新渲染帧、也不假装开 VLM：
                                # judge=None -> run_retake_loop 诚实升级人工（不重渲）
                                _loop2 = _brt2.run_retake_loop(
                                    wd, _bd2,
                                    retake_shots=list(_rp2.get("retakeShots") or []),
                                    judge=None, max_retries=a.retake_max,
                                    episode_id=ep, series_id=series_dir.name)
                                rows[-1]["retakeAttempts"] = _loop2.get("attempts")
                                rows[-1]["retakeEscalated"] = bool(_loop2.get("escalated"))
                                rows[-1]["retakeLevel"] = _loop2.get("level")
                                rows[-1]["retakeHint"] = _loop2.get("lastHint")
                                rows[-1]["retakeResult"] = {
                                    "ok": _loop2.get("ok"),
                                    "costCny": _loop2.get("costCny"),
                                    "shots": _loop2.get("shots")}
                        except Exception as _e:            # noqa: BLE001
                            rows[-1]["retakeError"] = f"{type(_e).__name__}: {str(_e)[:160]}"
                            rows[-1]["retakeEscalated"] = True
                            rows[-1]["retakeLevel"] = _level
                _rt_msg = ""
                if rows[-1].get("retakeAttempts") is not None or rows[-1].get("retakeError"):
                    _rt_msg = (f" · 边界重拍 attempts={rows[-1].get('retakeAttempts')} "
                               f"level={rows[-1].get('retakeLevel')}")
                    if rows[-1].get("retakeEscalated"):
                        _rt_msg += " ⚠升级人工"
                    _h2 = str(rows[-1].get("retakeHint") or "")[:60]
                    if _h2:
                        _rt_msg += f" · hint={_h2}"
                print(f"  ↷ {ep} 已成功（续跑跳过）{_rt_msg}")
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
                    if a.retake_red and _rp.get("needRetake"):
                        # B2 自动循环：hint 从 diff 自动生成，最多 --retake-max 次补拍-复审
                        # 复审判官要真 VLM：只有开了 --boundary-vlm 且两章都有帧才接线；
                        # 否则 judge=None -> run_retake_loop 诚实升级人工（不假装绿）
                        _judge = None
                        if a.boundary_vlm and prev_frames and _cur_frames:
                            _judge = _boundary_retake_judge(wd, prev_frames, _cur_frames)
                        _loop = _brt.run_retake_loop(
                            wd, _bd,
                            retake_shots=list(_rp.get("retakeShots") or []),
                            judge=_judge, max_retries=a.retake_max,
                            episode_id=ep, series_id=series_dir.name)
                        row["retakeAttempts"] = _loop.get("attempts")
                        row["retakeEscalated"] = bool(_loop.get("escalated"))
                        row["retakeLevel"] = _loop.get("level")
                        row["retakeHint"] = _loop.get("lastHint")
                        row["retakeResult"] = {"ok": _loop.get("ok"),
                                               "costCny": _loop.get("costCny"),
                                               "shots": _loop.get("shots")}
                        _retake_doc = _loop
                except Exception as _e:                  # noqa: BLE001
                    row["retakeError"] = f"{type(_e).__name__}: {str(_e)[:160]}"
                    row["retakeEscalated"] = True
                    row["retakeLevel"] = _level
            if _level == "red":
                row["failure"] = (row.get("failure") or "") + " 边界审红项：只重拍边界镜"
            prev_exit = _ch.get("exit")
            prev_frames = _cur_frames[-2:] if _cur_frames else []
        rows.append(row)
        mark = "✅" if ok else "❌"
        print(f"  {mark} {ep} rc={rc} 覆盖={row['coverage']} 墙钟={row['wallS']}s "
              f"场景¥{row.get('sceneCostCny')} 复用{row.get('reusedScenes')}场"
              + ("" if ok else f" · {row['failure'][:80]}"), flush=True)
        if _retake_doc:
            _hint_show = str(_retake_doc.get("lastHint") or "")[:80]
            _rt_state = ("⚠ 升级人工（" + str(_retake_doc.get("reason") or "")[:60] + "）"
                         if _retake_doc.get("escalated") else "已通过")
            print(f"    边界重拍：attempts={_retake_doc.get('attempts')} "
                  f"level={_retake_doc.get('level')} {_rt_state}"
                  + (f" · hint={_hint_show}" if _hint_show else ""), flush=True)
        if not ok and rep:
            pass

    total_cost = round(sum(float((r.get("sceneCostCny") or 0)) for r in rows), 2)
    retake_cost = round(sum(float(((r.get("retakeResult") or {}).get("costCny") or 0))
                            for r in rows), 4)
    retake_escalated = [str(r.get("episodeId")) for r in rows
                        if r.get("retakeEscalated")]
    # ---- 并行开场景视图：如实进 summary（#8：台账写已加合并锁）----
    _parallel = a.mode in ("parallel", "hybrid")
    _scene_parallel = bool(_parallel and a.scene_views)
    summary = {"kind": "series.run", "seriesDir": str(series_dir),
               "episodes": len(rows), "ok": sum(1 for r in rows if r.get("ok")),
               "wallS": round(time.time() - t_all, 1),
               "mode": a.mode,
               "sceneViews": bool(a.scene_views),
               "sceneViewsParallel": _scene_parallel,
               "sceneViewsNoLlm": bool(a.no_llm),
               "heroMax": int(a.hero_max),
               "sceneCostCny": total_cost,
               "retakeCostCny": retake_cost,
               "retakeEscalated": retake_escalated,
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
             f"- 执行模式：`{a.mode}`"
             + ("（**并行开场景视图**" +
                ("，LLM 档位规划" if not a.no_llm else "，no-LLM/启发式档位")
                + f"，hero-max={a.hero_max}）" if _scene_parallel else ""),
             f"- 场景视图成本：**¥{total_cost}** · 复用场景 {summary['reusedScenes']} 场",
             f"- 边界重拍：**¥{retake_cost}** · 升级人工 {len(retake_escalated)} 集"
             + (f"（{', '.join(retake_escalated)}）" if retake_escalated else ""),
             f"- 总墙钟：{summary['wallS']}s", "",
             "| 集 | 结果 | 覆盖 | 墙钟s | 场景¥ | 复用场 | 档位来源 | 重拍 |",
             "|---|---|---|---|---|---|---|---|"]

    def _retake_cell(r: dict) -> str:
        if not r.get("retakeLevel"):
            return "—"
        _n = int(r.get("retakeAttempts") or 0)
        if r.get("retakeEscalated"):
            return f"升级({_n}次)"
        return f"{r['retakeLevel']}({_n}次)" if _n else str(r["retakeLevel"])

    for r in rows:
        lines.append(f"| {r['episodeId']} | {'✅' if r.get('ok') else '❌'} "
                     f"| {r.get('coverage')} | {r.get('wallS')} "
                     f"| {r.get('sceneCostCny')} | {r.get('reusedScenes')} "
                     f"| {r.get('tierSource') or ''} | {_retake_cell(r)} |")
    (series_dir / "run-summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"  --- {summary['ok']}/{summary['episodes']} 成功 · 场景¥{total_cost} · "
          f"重拍¥{retake_cost} · 墙钟 {summary['wallS']}s"
          + (" · 并行开场景视图" if _scene_parallel else "")
          + (f" · 重拍升级 {len(retake_escalated)} 集：{', '.join(retake_escalated)}"
             if retake_escalated else ""))
    print(f"  汇总：{series_dir / 'run-summary.md'}")
    return 0 if summary["ok"] == summary["episodes"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
