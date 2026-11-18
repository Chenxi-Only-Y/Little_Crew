"""#8 真实并行跑后校验：series 台账无丢场景/丢 usage、run-summary 正确、无锁残留。

用法：
    python validate_locktest.py <repo_root>

读取：
  repo/research/series/ledger-lock-2026-11-18/series（跑后）+ _seed_before.json（跑前快照）
  repo/outputs/_LOCKTEST_2026-11-18/{E01,E02,E03}/delivery/*
  repo/research/series/ledger-lock-2026-11-18/run-summary.json
写出：
  repo/research/series/ledger-lock-2026-11-18/validation.json  + 控制台摘要
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

EVID = Path("research/series/ledger-lock-2026-11-18")
OUT = Path("outputs/_LOCKTEST_2026-11-18")
EPS = ["E01", "E02", "E03"]
# 剧本场景：sceneId -> 稳定 sceneKey（与 series_bible._loc_norm 一致）
SCRIPT_SCENES = {
    "E01": [("SC-E01-001", "地铁站台"), ("SC-E01-002", "医院走廊")],
    "E02": [("SC-E02-001", "地铁站台"), ("SC-E02-002", "河堤")],
    "E03": [("SC-E03-001", "地铁站台")],
}
EXPECTED_PAIRS = {(ep, key) for ep in EPS for _sid, key in SCRIPT_SCENES[ep]}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    ev = root / EVID
    out = root / OUT
    sb = ev / "series"
    problems: list[str] = []
    facts: dict = {"kind": "ledger.lock.real-parallel-validation",
                   "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    def chk(name: str, cond: bool, detail: str = "") -> None:
        facts.setdefault("checks", []).append(
            {"name": name, "ok": bool(cond), "detail": detail})
        if not cond:
            problems.append(f"{name}: {detail}")

    # ---------- 1. 台账文件完整 / 无残留 ----------
    files = sorted(p.name for p in sb.iterdir())
    post = json.loads((sb / "series.json").read_text(encoding="utf-8"))
    before = json.loads((sb / "_seed_before.json").read_text(encoding="utf-8"))
    chk("台账 JSON 可解析", isinstance(post, dict), str(type(post)))
    chk("台账目录无锁/tmp 残留",
        [f for f in files if f.endswith((".lock", ".tmp"))] == [], str(files))
    chk("系列目录含 series.json（其余为 run 汇总/脚本子目录，允许）",
        "series.json" in files
        and set(files) <= {"series.json", "_seed_before.json", "run-summary.json",
                           "run-summary.md", "boundary-summary.json",
                           "injection-report.json", "scripts"}, str(files))

    # ---------- 2. 集清单 ----------
    eps = [str(e.get("episodeId")) for e in post.get("episodes") or ()]
    chk("3 集全在（无丢集）", sorted(eps) == EPS, str(eps))
    for ep in EPS:
        row = next((e for e in post["episodes"] if e.get("episodeId") == ep), None)
        chk(f"{ep} episode 记录形状（scenes/shots/status）",
            bool(row) and row.get("scenes") == sorted(
                s for s, _ in SCRIPT_SCENES[ep]) and row.get("shots") == 2
            and row.get("status") == "done", str(row))

    # ---------- 3. 场景：不丢、只升不降、卡片未被意外改写 ----------
    scenes = post.get("scenes") or {}
    chk("场景集合不丢（seed 3 场都在）",
        set(before["scenes"]) <= set(scenes), str(sorted(scenes)))
    chk("场景数=3（无重复建场）", len(scenes) == 3, str(len(scenes)))
    for key, prev in before["scenes"].items():
        cur = scenes.get(key) or {}
        chk(f"{key} tier 只升不降",
            int(cur.get("tier") or 0) >= int(prev.get("tier") or 0),
            f"{prev.get('tier')} -> {cur.get('tier')}")
        chk(f"{key} 卡片哈希未被意外改写（同名卡复用）",
            str(cur.get("cardHash")) == str(prev.get("cardHash")),
            f"{prev.get('cardHash')} -> {cur.get('cardHash')}")
        chk(f"{key} assets 视图不丢（旧资产仍在）",
            set(prev.get("assets") or {}) <= set(cur.get("assets") or {}),
            str(sorted(set(prev.get("assets") or {}) - set(cur.get("assets") or {}))))
    station = scenes.get("地铁站台") or {}
    chk("共享场景 地铁站台 episodes=[E01,E02,E03] 无丢更新",
        station.get("episodes") == EPS, str(station.get("episodes")))

    # ---------- 4. usage：本次每集每场恰好 1 条、总数=旧+新、无重复行 ----------
    seed_usage_keys = {(str(u.get("episodeId")), str(u.get("sceneKey")),
                        str(u.get("action"))) for u in before.get("usage") or ()}
    usage_rows = post.get("usage") or ()
    actual_keys = [(str(u.get("episodeId")), str(u.get("sceneKey")),
                    str(u.get("action"))) for u in usage_rows]
    actual_key_set = set(actual_keys)
    actual_pairs = {(ep, key) for ep, key, _act in actual_key_set}
    new_keys = actual_key_set - seed_usage_keys
    chk("每集每场在最终台账都有 usage 记录（无丢更新）",
        EXPECTED_PAIRS <= actual_pairs,
        f"missing={sorted(EXPECTED_PAIRS - actual_pairs)}")
    # 本次应新增的 usage：场景视图里 reused 的场次动作必为 reused；
    # 若 seed 里已有同 (ep,scene,action)（历史跑过同样动作）则幂等不新增。
    expected_new: set[tuple[str, str, str]] = set()
    for ep in EPS:
        sv = json.loads((out / ep / "delivery" / "scene_views.json")
                        .read_text(encoding="utf-8"))
        by_id = {sid: row for sid, row in (sv.get("scenes") or {}).items()}
        for sid, key in SCRIPT_SCENES[ep]:
            row = by_id.get(sid) or {}
            if row.get("reused"):
                cand = (ep, key, "reused")
                if cand not in seed_usage_keys:
                    expected_new.add(cand)
    chk("本次新增 usage 键集合 = 期望（幂等去重语义）",
        new_keys == expected_new,
        f"missing={sorted(expected_new - new_keys)} "
        f"extra={sorted(new_keys - expected_new)}")
    pair_cnt = Counter((ep, key) for ep, key, _act in actual_key_set)
    chk("每集每场 usage 唯一键不超过 2 条（历史动作 + 本次动作，无重复）",
        set(pair_cnt.values()) <= {1, 2}, str(dict(pair_cnt)))
    chk("usage 行数 = 唯一键数（无重复行）",
        len(actual_keys) == len(actual_key_set),
        f"{len(actual_keys)} vs {len(actual_key_set)}")
    chk("usage 总数 = 旧 usage + 本次新增（只增不乱）",
        len(actual_keys) == len(seed_usage_keys) + len(new_keys),
        f"{len(actual_keys)} = {len(seed_usage_keys)} + {len(new_keys)}")
    per_ep = {ep: sum(1 for u in usage_rows
                      if str(u.get("episodeId")) == ep) for ep in EPS}
    chk("每集都有本次 usage 写入",
        all(v >= len(SCRIPT_SCENES[ep]) for ep, v in per_ep.items()), str(per_ep))
    facts["usage"] = {
        "seedRows": len(seed_usage_keys),
        "newRows": len(new_keys),
        "total": len(usage_rows),
        "perEpisode": per_ep,
        "stationUsageCounter": int(station.get("usage") or 0),
        "newKeys": sorted([list(k) for k in new_keys]),
    }

    # 动作语义：场景视图说 reused 的，usage 动作必须是 reused（不丢复用事实）
    for ep in EPS:
        sv = json.loads((out / ep / "delivery" / "scene_views.json")
                        .read_text(encoding="utf-8"))
        by_id = {sid: row for sid, row in (sv.get("scenes") or {}).items()}
        for sid, key in SCRIPT_SCENES[ep]:
            row = by_id.get(sid) or {}
            acts = sorted({a for (e, k, a) in actual_key_set
                           if e == ep and k == key})
            if row.get("reused"):
                # 本次复用动作必须进 usage（可能历史上已有同样动作 → 幂等不新增）
                chk(f"{ep}/{key} 复用动作进 usage", "reused" in acts, str(acts))

    # ---------- 5. cast 不丢 ----------
    cast = post.get("cast") or {}
    chk("cast 不丢（seed 角色都在）",
        set(before.get("cast") or {}) <= set(cast),
        str(sorted(set(before.get("cast") or {}) - set(cast))))

    # ---------- 6. 每集 delivery/series.json 形状 ----------
    for ep in EPS:
        dev = json.loads((out / ep / "delivery" / "series.json")
                         .read_text(encoding="utf-8"))
        chk(f"{ep} delivery/series.json 形状正确",
            dev.get("kind") == "series.state"
            and dev.get("episodeId") == ep
            and isinstance(dev.get("summary"), dict)
            and isinstance(dev.get("actions"), dict),
            str({k: dev.get(k) for k in ("kind", "episodeId")}))
        n_ep = int((dev.get("summary") or {}).get("episodes") or 0)
        chk(f"{ep} summary 集数在 2~3（合并后最新台账视角）",
            2 <= n_ep <= 3, str(dev.get("summary")))
        acts = sum(int(v) for v in (dev.get("actions") or {}).values())
        chk(f"{ep} actions 总数=该集场景数",
            acts == len(SCRIPT_SCENES[ep]), str(dev.get("actions")))

    # ---------- 7. 场景视图：并行开启 + 复用 ----------
    for ep in EPS:
        sv = json.loads((out / ep / "delivery" / "scene_views.json")
                        .read_text(encoding="utf-8"))
        chk(f"{ep} 场景视图有产物记录", bool(sv.get("scenes")), str(sv.get("kind")))
        chk(f"{ep} 场景全部复用（成本 0 预期）",
            all(bool(v.get("reused")) for v in (sv.get("scenes") or {}).values()),
            str({k: v.get("reused") for k, v in (sv.get("scenes") or {}).items()}))
        facts.setdefault("sceneViews", {})[ep] = {
            "costCny": sv.get("costCny"), "generated": sv.get("generated"),
            "reused": sv.get("reused"),
            "tierPlanSource": (sv.get("tierPlan") or {}).get("source")}

    # ---------- 8. run-summary ----------
    rs = json.loads((ev / "series" / "run-summary.json").read_text(encoding="utf-8"))
    chk("run-summary ok=3/3", int(rs.get("ok") or 0) == 3
        and int(rs.get("episodes") or 0) == 3,
        str({k: rs.get(k) for k in ("ok", "episodes")}))
    chk("run-summary 如实记录并行开场景视图",
        rs.get("mode") == "parallel" and rs.get("sceneViews") is True
        and rs.get("sceneViewsParallel") is True,
        str({k: rs.get(k) for k in ("mode", "sceneViews", "sceneViewsParallel")}))
    chk("run-summary 每集 ok=true",
        all(bool(r.get("ok")) for r in rs.get("rows") or ()),
        str([(r.get("episodeId"), r.get("ok")) for r in rs.get("rows") or ()]))

    # ---------- 9. 成本对账 ----------
    scene_cost = float(rs.get("sceneCostCny") or 0.0)
    render_cost = 0.0
    tokens = {"input": 0, "output": 0, "calls": 0}
    for ep in EPS:
        ic = json.loads((out / ep / "delivery" / "image_calls.json")
                        .read_text(encoding="utf-8"))
        render_cost += float((ic.get("cloud") or {}).get("estCostCny") or 0.0)
        tu = json.loads((out / ep / "delivery" / "token_usage.json")
                        .read_text(encoding="utf-8"))
        tokens["input"] += int(tu.get("totalInput") or 0)
        tokens["output"] += int(tu.get("totalOutput") or 0)
        tokens["calls"] += int(tu.get("totalCalls") or 0)
    facts["cost"] = {
        "sceneViewsCny": round(scene_cost, 4),
        "cloudRenderCny": round(render_cost, 4),
        "estimatedDirectCny": round(scene_cost + render_cost, 4),
        "llmTokens": tokens,
        "llmCostNote": "deepseek-flash 每集约 1k token：成本可忽略，未计入直接成本",
        "totalWallS": rs.get("wallS"),
    }
    chk("场景视图成本=¥0（全复用）", abs(scene_cost) < 1e-9, str(scene_cost))
    chk("云渲染成本=3集×2镜×¥0.05=¥0.30",
        abs(render_cost - 0.30) < 1e-6, str(render_cost))

    # ---------- 10. 逐集流水线结果 + 并发时间证据 ----------
    walls, merge_times = {}, {}
    for ep in EPS:
        rp = json.loads((out / ep / "delivery" / "run.report.json")
                        .read_text(encoding="utf-8"))
        walls[ep] = rp.get("wallSeconds")
        chk(f"{ep} 流水线 ok + 覆盖 1.0",
            bool(rp.get("ok")) and float(rp.get("coverage") or 0) >= 1.0,
            f"ok={rp.get('ok')} coverage={rp.get('coverage')} "
            f"reason={str(rp.get('reason'))[:80]}")
        mt = (out / ep / "delivery" / "series.json").stat().st_mtime
        merge_times[ep] = datetime.fromtimestamp(mt).strftime("%H:%M:%S")
    facts["wallSeconds"] = walls
    facts["ledgerMergeWriteTime"] = merge_times
    facts["runSummary"] = {k: rs.get(k) for k in
                           ("mode", "sceneViews", "sceneViewsParallel", "ok",
                            "episodes", "wallS", "sceneCostCny")}

    # ---------- 汇总 ----------
    facts["problems"] = problems
    facts["ok"] = not problems
    (ev / "validation.json").write_text(
        json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=" * 74)
    print(f"真实并行跑校验：{'✅ 全部通过' if not problems else '❌ ' + str(len(problems)) + ' 项失败'}")
    print(f"   台账：episodes={len(post.get('episodes') or ())}"
          f" scenes={len(scenes)} usage={len(post.get('usage') or ())}"
          f" cast={len(cast)}（seed usage {len(seed_usage_keys)} + 新 {len(new_keys)}）")
    print(f"   成本：场景视图 ¥{facts['cost']['sceneViewsCny']}"
          f" + 云渲染 ¥{facts['cost']['cloudRenderCny']}"
          f" = ¥{facts['cost']['estimatedDirectCny']}"
          f"（LLM {tokens['calls']} 次调用 / "
          f"{tokens['input'] + tokens['output']} tokens，成本可忽略）")
    print(f"   墙钟：{rs.get('wallS')}s；逐集 wallSeconds={walls}")
    print(f"   台账落盘时刻（并发证据）：{merge_times}")
    print(f"   usage 分布：{per_ep}")
    for p in problems:
        print(f"   ❌ {p}")
    print(f"   证据：{ev / 'validation.json'}")
    print("=" * 74)
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())


