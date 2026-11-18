"""`runtime/series_bible.py` 自检（2026-11-17）。

系列资产台账：稳定 sceneKey / 场景卡版本 / 档位只升不降 /
角色版本与造型 / 复用判定 / 使用记录 / 汇总。

#8（2026-11-18）追加：跨进程写锁 / 原子写 / locked_merge / merge_episode /
多进程并发无丢更新 / 同集重复合并幂等 / 陈旧锁可破 / 异常释放锁。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


# #8 多进程并发用例的 worker：
#   先写 ready 文件报到 → 等父进程 go 信号（保证 4 进程真正同时抢锁）→
#   对本集增量重复 merge 6 轮（同集幂等 + 跨进程互斥/合并）。
_WORKER_SRC = r'''
import json
import os
import sys
import time

from little_crew.runtime import series_bible as SB

base, ep, go_flag, ready_flag = sys.argv[1:5]
with open(ready_flag, "w", encoding="utf-8") as fh:
    fh.write("ready")
deadline = time.time() + 30
while not os.path.exists(go_flag) and time.time() < deadline:
    time.sleep(0.005)
scenes = [{"sceneKey": f"{ep}-场景{i}", "heading": f"SC-{ep}-{i}",
           "card": {"space": f"{ep}-{i}"}, "tier": 1 + (i % 2),
           "decisionSource": "llm", "reason": "并发压力",
           "assets": {f"view{i}": f"/{ep}-{i}.png"}, "action": "generated"}
          for i in range(1, 4)]
usage = [{"sceneKey": s["sceneKey"], "tier": s["tier"], "action": s["action"],
          "source": "llm", "assets": s["assets"]} for s in scenes]
cast = [{"characterId": f"角色{ep}", "name": f"角色{ep}",
         "appearance": f"{ep}-外观", "anchor": f"/{ep}.png"}]
for _ in range(6):
    SB.merge_episode(base, episode_id=ep, title=f"并发{ep}", scenes=scenes,
                     usage=usage, cast=cast, shots=2, timeout_s=60)
    time.sleep(0.004)
print(json.dumps({"episodeId": ep, "ok": True}, ensure_ascii=False))
'''


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("系列资产台账 —— 自检")
    print("=" * 66)

    from little_crew.runtime import series_bible as SB

    print(); print("A. 稳定 sceneKey 与卡哈希")
    check("A1 ★★★ 标题第一段去时段",
          SB.stable_scene_key("地铁站台·夜") == "地铁站台", SB.stable_scene_key("地铁站台·夜"))
    check("A2 ★★★ 空站台/站台 可经别名归一",
          SB.stable_scene_key("空站台·夜", aliases={"站台": "地铁站台"}) == "地铁站台",
          SB.stable_scene_key("空站台·夜", aliases={"站台": "地铁站台"}))
    check("A3 ★★ 同一卡 → 同一卡哈希（内容指纹）",
          SB.card_hash({"space": "站台", "props": ["长椅"]})
          == SB.card_hash({"space": "站台", "props": ["长椅"]}), "")
    check("A4 ★★ 卡内容变化 → 卡哈希变化",
          SB.card_hash({"space": "站台"}) != SB.card_hash({"space": "站厅"}), "")

    print(); print("B. 场景登记：版本/档位/使用")
    with tempfile.TemporaryDirectory() as td:
        data = SB.load(td)
        check("B1 ★★★ 空目录 -> 空台账", not data.get("scenes") and not data.get("cast"), "")
        e1 = SB.upsert_scene(data, scene_key="地铁站台", heading="地铁站台·夜",
                             card={"space": "站台"}, tier=1, episode_id="E01",
                             decision_source="llm", assets={"base": "/tmp/a.png"})
        check("B2 ★★★ 首次登记 version=1 / tier=1 / episodes=[E01]",
              e1["version"] == 1 and e1["tier"] == 1 and e1["episodes"] == ["E01"], str(e1))
        e2 = SB.upsert_scene(data, scene_key="地铁站台", heading="地铁站台·夜",
                             card={"space": "站台"}, tier=2, episode_id="E02",
                             decision_source="llm-series",
                             assets={"front_wide": "/tmp/b.png"})
        check("B3 ★★★ 档位只升不降（1→2）", e2["tier"] == 2, str(e2["tier"]))
        e3 = SB.upsert_scene(data, scene_key="地铁站台", card={"space": "站台"},
                             tier=1, episode_id="E03")
        check("B4 ★★★ 低档位不会把台账拉低", e3["tier"] == 2, str(e3["tier"]))
        check("B5 ★★★ 同一卡不升级版本", e3["version"] == 1, str(e3["version"]))
        e4 = SB.upsert_scene(data, scene_key="地铁站台",
                             card={"space": "站台（改）"}, tier=2, episode_id="E04")
        check("B6 ★★★ 卡内容变化 -> version+1", e4["version"] == 2, str(e4["version"]))
        check("B7 ★★ 资产路径可累积更新",
              "base" in (e4.get("assets") or {}) and "front_wide" in (e4.get("assets") or {}), "")
        check("B8 ★★ episodes 去重累积",
              e4["episodes"] == ["E01", "E02", "E03", "E04"], str(e4["episodes"]))
        check("B9 ★★ tierHistory 记录升档",
              any(h.get("tier") == 2 for h in (e4.get("tierHistory") or [])), "")

        print(); print("C. 复用判定")
        same = SB.decide_reuse(data, scene_key="地铁站台",
                               card={"space": "站台（改）"}, desired_tier=2)
        check("C1 ★★★ 同卡+同档 -> 可复用", same["reuse"] is True, str(same))
        up = SB.decide_reuse(data, scene_key="地铁站台",
                             card={"space": "站台（改）"}, desired_tier=3)
        check("C2 ★★★ 目标档更高 -> 需升档", up["reuse"] is False and up.get("upgrade") is True, str(up))
        first = SB.decide_reuse(data, scene_key="新地点", card={"space": "x"}, desired_tier=2)
        check("C3 ★★ 台账没有 -> 首次生成", first["reuse"] is False, str(first))
        changed = SB.decide_reuse(data, scene_key="地铁站台",
                                  card={"space": "完全不同"}, desired_tier=1)
        check("C4 ★★★ 卡变了 -> 版本更新（不复用旧图）",
              changed["reuse"] is False and "变化" in str(changed.get("why")), str(changed))

        print(); print("D. 角色台账")
        c1 = SB.upsert_character(data, character_id="少女", name="少女",
                                 appearance="银灰高马尾", outfit="素色长袍",
                                 episode_id="E01", anchor="/tmp/anchor.png")
        check("D1 ★★★ 角色首次登记 version=1",
              c1["version"] == 1 and c1["episodes"] == ["E01"], str(c1))
        c2 = SB.upsert_character(data, character_id="少女", appearance="银灰高马尾",
                                 outfit="深灰卫衣", episode_id="E02")
        check("D2 ★★★ 外观不变 -> 版本不变", c2["version"] == 1, str(c2["version"]))
        check("D3 ★★ 新造型进 outfits 集合",
              set(c2["outfits"]) == {"素色长袍", "深灰卫衣"}, str(c2["outfits"]))
        c3 = SB.upsert_character(data, character_id="少女", appearance="黑长直",
                                 episode_id="E03")
        check("D4 ★★★ 外观变化 -> 版本+1", c3["version"] == 2, str(c3["version"]))

        _assets_before = dict((SB.scene_entry(data, "地铁站台") or {}).get("assets") or {})
        SB.upsert_scene(data, scene_key="地铁站台", card={"space": "站台（改）"},
                        tier=2, episode_id="E05", assets={"only_new": "/tmp/n.png"})
        _after = dict((SB.scene_entry(data, "地铁站台") or {}).get("assets") or {})
        check("D5 ★★★ 台账 assets 只增不覆盖（旧视图不丢）",
              "base" in _after and "only_new" in _after, str(_after))

        print(); print("E. 使用记录 / 汇总 / 持久化")
        SB.record_usage(data, episode_id="E02", scene_key="地铁站台", tier=2,
                        action="reused", source="llm-series")
        check("E1 ★★ usage 记录追加，场景计数 +1",
              len(data["usage"]) == 1
          and (SB.scene_entry(data, "地铁站台") or {}).get("usage") == 1,
          str(data["usage"]))
        ep = SB.upsert_episode(data, episode_id="E01", title="第一集",
                               scenes=["地铁站台"], shots=6)
        SB.upsert_episode(data, episode_id="E01", shots=8)
        check("E2 ★★ 集信息 upsert（不重复）",
              len(data["episodes"]) == 1 and data["episodes"][0]["shots"] == 8, str(data["episodes"]))
        s = SB.summary(data)
        check("E3 ★★★ 汇总含集数/场景/角色/档位分布",
              s["episodes"] == 1 and s["scenes"] == 1 and s["cast"] == 1
              and s["tiers"].get(2) == 1, str(s))
        path = SB.save(td, data)
        loaded = SB.load(td)
        check("E4 ★★★ 落盘/读回一致", path.is_file()
              and loaded["scenes"]["地铁站台"]["tier"] == 2
              and loaded["cast"]["少女"]["version"] == 2, str(path))
        check("E5 ★★ 台账 schema 版本写清", loaded.get("schema") == SB.SCHEMA, "")

    # ==================================================================
    # #8（2026-11-18）：并行写台账锁 —— 锁 / 原子写 / 合并 / 并发
    # ==================================================================
    print(); print("F. 跨进程写锁 / 原子写（#8）")
    with tempfile.TemporaryDirectory() as td:
        _lp = Path(td) / SB.LOCK_FILE
        _ep_row = {"episodeId": "LOCK1", "title": "", "scenes": [],
                   "shots": 0, "status": "done"}
        r = SB.locked_merge(
            td, lambda d: d.setdefault("episodes", []).append(dict(_ep_row)),
            timeout_s=5.0)
        check("F1 ★★★ locked_merge：锁内读最新→改→写回，返回 locked/waitMs/summary",
              r.get("locked") is True and isinstance(r.get("waitMs"), int)
              and r.get("staleBroke") is False
              and (r.get("summary") or {}).get("episodes") == 1
              and ((r.get("data") or {}).get("episodes") or [{}])[0].get("episodeId") == "LOCK1"
              and not _lp.exists(),
              str({k: r.get(k) for k in ("locked", "waitMs", "staleBroke")}))

        _lp.write_text("held", encoding="utf-8")
        _raised, _err = False, ""
        _t0 = time.monotonic()
        try:
            SB.locked_merge(td, lambda d: None, timeout_s=0.25)
        except SB.LedgerLockTimeout as e:
            _raised, _err = True, str(e)
        finally:
            try:
                _lp.unlink()
            except OSError:
                pass
        check("F2 ★★ 锁被占超时 → LedgerLockTimeout（报错含超时/锁路径）",
              _raised and "超时" in _err and SB.LOCK_FILE in _err,
              f"raised={_raised} elapsed={time.monotonic() - _t0:.2f}s {_err[:90]}")

        _lp.write_text("stale", encoding="utf-8")
        _old = time.time() - SB.LOCK_STALE_S - 60
        os.utime(str(_lp), (_old, _old))
        _t0 = time.monotonic()
        r2 = SB.locked_merge(
            td, lambda d: d.setdefault("episodes", []).append(
                {"episodeId": "LOCK2", "title": "", "scenes": [],
                 "shots": 0, "status": "done"}), timeout_s=5.0)
        check("F3 ★★★ 陈旧锁（mtime > 300s）可破 + staleBroke=True",
              r2.get("staleBroke") is True and not _lp.exists()
              and (r2.get("summary") or {}).get("episodes") == 2,
              f"staleBroke={r2.get('staleBroke')} "
              f"elapsed={time.monotonic() - _t0:.2f}s")

        _before = (Path(td) / "series.json").read_text(encoding="utf-8")

        def _boom(_d):
            _d["episodes"] = []
            raise ValueError("mutate 故意炸")

        _raised2 = False
        try:
            SB.locked_merge(td, _boom, timeout_s=5.0)
        except ValueError:
            _raised2 = True
        _after = (Path(td) / "series.json").read_text(encoding="utf-8")
        r3 = SB.locked_merge(td, lambda d: d.setdefault("x", 1), timeout_s=5.0)
        check("F4 ★★★ mutate 抛异常 → 锁仍释放、数据未被破坏、随后可再取锁",
              _raised2 and not _lp.exists() and _before == _after
              and r3.get("locked") is True,
              f"raised={_raised2} lockGone={not _lp.exists()} "
              f"dataIntact={_before == _after}")

        _residue = sorted(p.name for p in Path(td).iterdir()
                          if p.name != "series.json")
        _parsed = True
        try:
            json.loads((Path(td) / "series.json").read_text(encoding="utf-8"))
        except Exception:                                    # noqa: BLE001
            _parsed = False
        check("F5 ★★ 原子写：无 .tmp / 锁文件残留，JSON 完整可解析",
              _residue == [] and _parsed, str(_residue))

    print(); print("G. merge_episode 按集合并语义（#8）")
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "series"
        _land = {"sceneKey": "站台", "heading": "SC-E01-001",
                 "card": {"space": "站台"}, "tier": 1,
                 "decisionSource": "llm", "reason": "首集站台",
                 "assets": {"base": "/a.png"}, "action": "generated"}
        _land_usage = [{"sceneKey": "站台", "tier": 1, "action": "generated",
                        "source": "llm", "assets": {"base": "/a.png"}}]
        _girl = {"characterId": "少女", "name": "少女",
                 "appearance": "银灰高马尾", "outfit": "素色长袍",
                 "anchor": "/anchor.png"}
        r1 = SB.merge_episode(
            base, episode_id="E01", title="第一集", scenes=[_land],
            usage=_land_usage, cast=[_girl], shots=2,
            episode_scene_ids=["SC-E01-001"])
        d = SB.load(base)
        check("G1 ★★★ 首次合并：集/场景/usage/角色一次写入且返回形状正确",
              r1.get("ok") is True and r1.get("episodeId") == "E01"
              and len(d["episodes"]) == 1
              and d["episodes"][0]["scenes"] == ["SC-E01-001"]
              and d["scenes"]["站台"]["tier"] == 1
              and d["scenes"]["站台"]["episodes"] == ["E01"]
              and d["scenes"]["站台"]["assets"] == {"base": "/a.png"}
              and len(d["usage"]) == 1 and "少女" in d["cast"]
              and r1["added"] == {"scenes": 1, "usage": 1, "cast": 1},
              f"added={r1.get('added')} summary={r1.get('summary')}")

        r1b = SB.merge_episode(
            base, episode_id="E01", title="第一集", scenes=[_land],
            usage=_land_usage, cast=[_girl], shots=2,
            episode_scene_ids=["SC-E01-001"])
        d1b = SB.load(base)
        check("G2 ★★★ 同集重复 merge 幂等：episodes 1 条 / usage 不重复 / version 不虚增",
              len(d1b["episodes"]) == 1 and len(d1b["usage"]) == 1
              and d1b["scenes"]["站台"]["version"] == 1
              and d1b["scenes"]["站台"]["usage"] == 1
              and d1b["cast"]["少女"]["episodes"] == ["E01"]
              and d1b["cast"]["少女"]["version"] == 1
              and r1b["added"] == {"scenes": 0, "usage": 0, "cast": 0},
              f"added={r1b.get('added')} summary={r1b.get('summary')}")

        _hall = {"sceneKey": "医院走廊", "heading": "SC-E02-001",
                 "card": {"space": "医院走廊"}, "tier": 2,
                 "decisionSource": "llm-series", "reason": "系列第二集",
                 "assets": {"front_wide": "/b.png"}, "action": "generated"}
        _hall_usage = [{"sceneKey": "医院走廊", "tier": 2, "action": "generated",
                        "source": "llm-series",
                        "assets": {"front_wide": "/b.png"}}]
        _girl2 = {"characterId": "少女", "name": "少女",
                  "appearance": "银灰高马尾", "outfit": "夜行衣", "anchor": ""}
        _doc = {"characterId": "医生", "name": "医生",
                "appearance": "白大褂", "anchor": "/doc.png"}
        SB.merge_episode(base, episode_id="E02", title="第二集",
                         scenes=[_hall], usage=_hall_usage,
                         cast=[_girl2, _doc], shots=2,
                         episode_scene_ids=["SC-E02-001"])
        d2 = SB.load(base)
        check("G3 ★★★ 合并 B 集不丢 A 集数据（A 场景/A 角色仍在，集数=2）",
              "站台" in d2["scenes"] and "少女" in d2["cast"]
              and "医院走廊" in d2["scenes"] and "医生" in d2["cast"]
              and sorted(e["episodeId"] for e in d2["episodes"]) == ["E01", "E02"]
              and len(d2["usage"]) == 2,
              str(sorted(d2["scenes"])))
        check("G4 ★★★ merge 路径 cast episodes/outfits 去重（不重复追加）",
              d2["cast"]["少女"]["episodes"] == ["E01", "E02"]
              and d2["cast"]["少女"]["outfits"] == ["素色长袍", "夜行衣"],
              str(d2["cast"]["少女"]))
        SB.merge_episode(base, episode_id="E02", title="第二集",
                         scenes=[], usage=[], shots=2, cast=[_girl2])
        d2b = SB.load(base)
        check("G5 ★★ 同集 cast 重复合并仍幂等（episodes/outfits 不重复）",
              d2b["cast"]["少女"]["episodes"] == ["E01", "E02"]
              and d2b["cast"]["少女"]["outfits"] == ["素色长袍", "夜行衣"],
              str(d2b["cast"]["少女"]["outfits"]))

        SB.merge_episode(base, episode_id="E03", title="第三集",
                         scenes=[{"sceneKey": "站台", "card": {"space": "站台"},
                                  "tier": 2, "decisionSource": "llm-series",
                                  "reason": "升档",
                                  "assets": {"front_wide": "/c.png"}}],
                         usage=[{"sceneKey": "站台", "tier": 2,
                                 "action": "upgraded", "source": "llm-series"}],
                         shots=2)
        SB.merge_episode(base, episode_id="E04", title="第四集",
                         scenes=[{"sceneKey": "站台", "card": {"space": "站台"},
                                  "tier": 1, "decisionSource": "heuristic",
                                  "reason": "低档", "assets": {}}],
                         usage=[{"sceneKey": "站台", "tier": 1,
                                 "action": "generated", "source": "heuristic"}],
                         shots=2)
        _st = SB.scene_entry(SB.load(base), "站台") or {}
        check("G6 ★★★ merge 路径档位只升不降（T1→T2→仍 T2）+ 同卡不升版本",
              _st.get("tier") == 2 and _st.get("version") == 1,
              f"tier={_st.get('tier')} version={_st.get('version')}")
        check("G7 ★★★ merge 路径 assets 视图合并（旧视图不丢）",
              {"base", "front_wide"} <= set(_st.get("assets") or {}),
              str(_st.get("assets")))

        SB.merge_episode(base, episode_id="E05", title="第五集",
                         scenes=[{"sceneKey": "站台",
                                  "card": {"space": "站台-改"}, "tier": 2,
                                  "decisionSource": "llm", "reason": "卡变化",
                                  "assets": {"detail": "/d.png"}}],
                         usage=[{"sceneKey": "站台", "tier": 2,
                                 "action": "upgraded", "source": "llm"}],
                         shots=2)
        _st2 = SB.scene_entry(SB.load(base), "站台") or {}
        check("G8 ★★★ merge 路径 cardHash 变化才升 version（同卡不变，变卡 +1）",
              _st2.get("version") == 2 and _st2.get("tier") == 2
              and {"base", "front_wide", "detail"} <= set(_st2.get("assets") or {}),
              f"version={_st2.get('version')} assets={_st2.get('assets')}")

    print(); print("H. 多进程并发合并（#8：4 进程同时抢锁写不同集）")
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "series"
        worker = Path(td) / "_merge_worker.py"
        worker.write_text(_WORKER_SRC, encoding="utf-8")
        go_flag = Path(td) / "go.flag"
        env = dict(os.environ)
        _src_dir = str(Path(SB.__file__).resolve().parents[2])
        _pp = [str(_src_dir)]
        if env.get("PYTHONPATH"):
            _pp.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(_pp)
        env["PYTHONIOENCODING"] = "utf-8"
        eps = ["P1", "P2", "P3", "P4"]
        procs = []
        for ep0 in eps:
            ready = Path(td) / f"ready_{ep0}.flag"
            procs.append((ep0, ready, subprocess.Popen(
                [sys.executable, "-X", "utf8", str(worker), str(base), ep0,
                 str(go_flag), str(ready)],
                cwd=str(td), env=env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace")))
        _t0 = time.monotonic()
        while (not all(rd.is_file() for _ep, rd, _pr in procs)
               and time.monotonic() - _t0 < 60):
            time.sleep(0.01)
        go_flag.write_text("go", encoding="utf-8")
        results = []
        for ep0, _ready, pr in procs:
            try:
                _o, _e = pr.communicate(timeout=120)
                results.append((ep0, pr.returncode, _o, _e))
            except subprocess.TimeoutExpired:
                pr.kill()
                _o, _e = pr.communicate()
                results.append((ep0, "TIMEOUT", _o, _e))
        d = SB.load(base)
        eps_got = sorted(str(e.get("episodeId")) for e in d.get("episodes") or ())
        check("H1 ★★★ 4 进程同时 merge 不同集 → 4 集全在（无丢集）",
              eps_got == eps and all(rc == 0 for _e, rc, _o, _r in results),
              f"eps={eps_got} rcs={[rc for _e, rc, _o, _r in results]} "
              f"stderr={[str(_r)[:80] for _e, _rc, _o, _r in results if _r]}")
        _want = {f"{ep0}-场景{i}" for ep0 in eps for i in (1, 2, 3)}
        _usage_by_ep: dict[str, int] = {}
        for u in d.get("usage") or ():
            if isinstance(u, dict):
                _k = str(u.get("episodeId"))
                _usage_by_ep[_k] = _usage_by_ep.get(_k, 0) + 1
        check("H2 ★★★ 并发无丢更新：4×3 场 / 4×3 usage / 4 角色 全部保留",
              set(d.get("scenes") or {}) == _want
              and all(_usage_by_ep.get(ep0) == 3 for ep0 in eps)
              and len(d.get("cast") or {}) == 4
              and len(d.get("usage") or ()) == 12
              and len(d.get("episodes") or ()) == 4,
              f"scenes={len(d.get('scenes') or {})} "
              f"usage={len(d.get('usage') or ())} "
              f"cast={len(d.get('cast') or {})} usageByEp={_usage_by_ep}")
        _files = sorted(p.name for p in base.iterdir())
        _ok_json = True
        try:
            _raw = json.loads((base / "series.json").read_text(encoding="utf-8"))
            _ok_json = (isinstance(_raw, dict)
                        and len(_raw.get("episodes") or ()) == 4)
        except Exception:                                    # noqa: BLE001
            _ok_json = False
        check("H3 ★★★ 并发后 JSON 完整可解析、无 .tmp / 锁文件残留",
              _ok_json and _files == ["series.json"], str(_files))
        _per_ep_ok = True
        for ep0 in eps:
            _keys = {f"{ep0}-场景{i}" for i in (1, 2, 3)}
            _ep = next((e for e in d["episodes"]
                        if str(e.get("episodeId")) == ep0), None)
            if not (_keys <= set(d["scenes"]) and _ep
                    and f"角色{ep0}" in (d.get("cast") or {})):
                _per_ep_ok = False
        check("H4 ★★ 并发后逐集校验：每集 episode/scenes/角色 均齐",
              _per_ep_ok and len(d["episodes"]) == 4, str(eps_got))

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
