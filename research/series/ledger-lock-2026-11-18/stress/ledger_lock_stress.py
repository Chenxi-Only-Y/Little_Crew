"""#8 独立并发压力验证：N 进程 × M 集 × R 轮 merge_episode，验证 0 丢更新。

用法（TEMP 脚本）：
    python ledger_lock_stress.py [N_PROC] [EPS_PER] [ROUNDS] [OUT_JSON]

临时目录做 series base：父进程先起全部 worker（各自 ready 报到），
再发 go 信号让它们真正同时抢锁；父进程在临时目录销毁前核对
episodes/scenes/usage/cast 计数与 JSON 完整性，并写一份 JSON 结果。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WORKER = r'''
import json
import os
import sys
import time

from little_crew.runtime import series_bible as SB

base, tag, go_flag, ready = sys.argv[1:5]
open(ready, "w", encoding="utf-8").write("ready")
deadline = time.time() + 60
while not os.path.exists(go_flag) and time.time() < deadline:
    time.sleep(0.003)
eps = [f"{tag}-E{i}" for i in range(1, 6)]
scenes = [{"sceneKey": f"{tag}-场景{i}", "heading": f"SC-{tag}-{i}",
           "card": {"space": f"{tag}-{i}"}, "tier": 1 + (i % 3),
           "decisionSource": "llm", "reason": "stress",
           "assets": {"view": f"/{tag}-{i}.png"}, "action": "generated"}
          for i in range(1, 6)]
usage = [{"sceneKey": s["sceneKey"], "tier": s["tier"], "action": s["action"],
          "source": "llm", "assets": s["assets"]} for s in scenes]
cast = [{"characterId": f"角色{tag}", "name": f"角色{tag}",
         "appearance": f"{tag}-外观", "anchor": f"/{tag}.png"}]
calls = 0
wait_ms = 0
stale = 0
for _round in range(int(sys.argv[5])):
    for ep in eps:
        row = SB.merge_episode(base, episode_id=ep, title=ep, scenes=scenes,
                               usage=usage, cast=cast, shots=2, timeout_s=60)
        calls += 1
        wait_ms += int(row.get("waitMs") or 0)
        stale += 1 if row.get("staleBroke") else 0
print(json.dumps({"tag": tag, "calls": calls, "waitMs": wait_ms,
                  "staleBroke": stale}))
'''


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    n_proc = int(argv[1]) if len(argv) > 1 else 8
    eps_per = int(argv[2]) if len(argv) > 2 else 5
    rounds = int(argv[3]) if len(argv) > 3 else 5
    out_json = Path(argv[4]) if len(argv) > 4 else None
    if eps_per != 5:
        raise SystemExit("本脚本 worker 固定 5 集/进程（EPS_PER=5）")
    print("=" * 78)
    print(f"#8 并发压力：{n_proc} 进程 × {eps_per} 集 × {rounds} 轮 merge_episode"
          f"（共 {n_proc * eps_per * rounds} 次锁内合并）")
    print("=" * 78)
    from little_crew.runtime import series_bible as SB

    t_all = time.monotonic()
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        base = tdp / "series"
        worker = tdp / "_stress_worker.py"
        worker.write_text(WORKER, encoding="utf-8")
        go_flag = tdp / "go.flag"
        env = dict(os.environ)
        env["PYTHONPATH"] = (r"D:\AI\ds.8\Little_Crew\src"
                             + (os.pathsep + env["PYTHONPATH"]
                                if env.get("PYTHONPATH") else ""))
        env["PYTHONIOENCODING"] = "utf-8"
        env["LITTLE_CREW_ALLOW_UNVERIFIED"] = "1"
        tags = [f"W{i}" for i in range(1, n_proc + 1)]
        procs = []
        for tag in tags:
            ready = tdp / f"ready_{tag}.flag"
            procs.append((tag, ready, subprocess.Popen(
                [sys.executable, "-X", "utf8", str(worker), str(base), tag,
                 str(go_flag), str(ready), str(rounds)],
                cwd=str(td), env=env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace")))
        t0 = time.monotonic()
        while (not all(rd.is_file() for _t, rd, _p in procs)
               and time.monotonic() - t0 < 120):
            time.sleep(0.005)
        startup = round(time.monotonic() - t0, 2)
        go_flag.write_text("go", encoding="utf-8")
        rows, errs = [], []
        for tag, _rd, pr in procs:
            out, err = pr.communicate(timeout=600)
            if pr.returncode != 0:
                errs.append((tag, pr.returncode, (err or "")[-300:]))
            else:
                try:
                    rows.append(json.loads((out or "").strip().splitlines()[-1]))
                except Exception:                            # noqa: BLE001
                    errs.append((tag, "BADOUT", (out or "")[-300:]))
        wall = round(time.monotonic() - t_all, 2)

        d = SB.load(base)
        eps_got = sorted(str(e.get("episodeId")) for e in d.get("episodes") or ())
        want_eps = sorted(f"{tag}-E{i}" for tag in tags
                          for i in range(1, eps_per + 1))
        want_scene = {f"{tag}-场景{i}" for tag in tags
                      for i in range(1, eps_per + 1)}
        usage_by_ep: dict[str, int] = {}
        for u in d.get("usage") or ():
            if isinstance(u, dict):
                k = str(u.get("episodeId"))
                usage_by_ep[k] = usage_by_ep.get(k, 0) + 1
        files = sorted(p.name for p in base.iterdir())
        json_ok = False
        try:
            raw = json.loads((base / "series.json").read_text(encoding="utf-8"))
            json_ok = isinstance(raw, dict)
        except Exception:                                    # noqa: BLE001
            pass
        calls = sum(int(r.get("calls") or 0) for r in rows)
        wait_ms = sum(int(r.get("waitMs") or 0) for r in rows)
        stale = sum(int(r.get("staleBroke") or 0) for r in rows)

    checks = {
        "worker_all_ok": not errs,
        "episodes": len(d.get("episodes") or ()) == n_proc * eps_per,
        "episodes_set": eps_got == want_eps,
        "scenes": set(d.get("scenes") or {}) == want_scene,
        "usage": len(d.get("usage") or ()) == n_proc * eps_per * eps_per,
        "usage_per_ep": all(usage_by_ep.get(e) == eps_per for e in want_eps),
        "cast": len(d.get("cast") or {}) == n_proc,
        "json_ok": json_ok,
        "no_residue": files == ["series.json"],
        "call_count": calls == n_proc * eps_per * rounds,
    }
    result = {
        "kind": "ledger.lock.stress",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config": {"processes": n_proc, "episodesPerProcess": eps_per,
                   "rounds": rounds, "totalLockedMerges": calls},
        "result": {
            "workersOk": not errs,
            "startupS": startup,
            "wallS": wall,
            "lockWaitMsTotal": wait_ms,
            "staleLocksBroke": stale,
            "episodes": len(d.get("episodes") or ()),
            "scenes": len(d.get("scenes") or {}),
            "usage": len(d.get("usage") or ()),
            "cast": len(d.get("cast") or {}),
            "usagePerEpisode": sorted(set(usage_by_ep.values())),
            "residueFiles": files,
            "jsonIntact": json_ok,
            "checks": checks,
            "lostUpdates": 0 if all(checks.values()) else "see checks",
        },
        "expected": {
            "episodes": n_proc * eps_per,
            "scenes": n_proc * eps_per,
            "usage": n_proc * eps_per * eps_per,
            "cast": n_proc,
        },
    }
    print(f"worker 报错: {errs}")
    print(f"startup={startup}s  total calls={calls}  累计等锁={wait_ms}ms"
          f"  破陈旧锁={stale}  整体墙钟={wall}s")
    print(f"episodes={result['result']['episodes']}"
          f"（期望 {result['expected']['episodes']}）"
          f"  scenes={result['result']['scenes']}（期望 {result['expected']['scenes']}）"
          f"  usage={result['result']['usage']}（期望 {result['expected']['usage']}）"
          f"  cast={result['result']['cast']}（期望 {result['expected']['cast']}）")
    print(f"目录残留={files}  JSON完整={json_ok}  "
          f"usagePerEp 分布={result['result']['usagePerEpisode']}")
    bad = [k for k, v in checks.items() if not v]
    print("-" * 78)
    print(f"结论：{'✅ 全部通过，0 丢更新' if not bad else '❌ 失败项 ' + str(bad)}")
    if out_json is not None:
        out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(f"结果 JSON：{out_json}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
