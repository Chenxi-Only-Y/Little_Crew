"""#8 集成验证：episode.py 的 `_run_scene_views` 回写段（离线，假生图）。

不联网、不出图：monkeypatch `scene_views.generate/write_report`，用两个"集"
依次跑同一 series_dir，验证：
  1) 回写走 merge_episode 一次（不再整份 save 覆盖）
  2) 两集数据都在：episodes/scenes/usage 累积、共享场景 episodes 合并
  3) delivery/series.json 形状不变（kind/seriesId/episodeId/summary/actions）
  4) 无锁文件/临时文件残留
  5) merge 抛异常时"不阻断"：仍返回 rep 并写 delivery/series.json
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from little_crew.runtime import series_bible as SB
from little_crew.runtime import scene_views as SV
from little_crew.runtime.episode import EpisodeRunner

_R = []


def check(name, cond, detail=""):
    _R.append((name, bool(cond), detail))


def _make_runner(wd: Path, series_dir: Path, episode_id: str, title: str):
    r = EpisodeRunner.__new__(EpisodeRunner)
    r._workdir = str(wd)
    r.workdir = wd
    r.episode_id = episode_id
    r.title = title
    r.series_dir = str(series_dir)
    r.scene_views_enabled = True
    r.scene_views_hero_max = 2
    r.scene_views_tiers = {}
    r.scene_views_llm = False          # 不建 LLM client → 系列规划走 heuristic（离线）
    r.characters = SimpleNamespace(sheets=[])
    r._scene_card_for = lambda s: {"space": str(getattr(s, "scene_id", ""))}
    return r


def _fake_generate(**kw):
    tiers = kw.get("tiers") or {}
    out = {}
    for sid, row in tiers.items():
        tier = int(row.get("tier") or 1)
        out[sid] = {"sceneId": sid, "tier": tier, "reused": False,
                    "images": [{"view": f"v{tier}", "out": f"/{sid}-T{tier}.png",
                                "ok": True}],
                    "board": ""}
    return {"scenes": out, "generated": len(out), "costCny": 0.0, "failed": 0}


def _script(*pairs):
    return {"scenes": [{"id": sid, "heading": head} for sid, head in pairs]}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 70)
    print("#8 episode.py 场景视图回写段 —— 集成自检（离线）")
    print("=" * 70)
    SV.generate = _fake_generate
    SV.write_report = lambda wd, rep: None

    tmp = Path(tempfile.mkdtemp(prefix="lc_ledger_integ_"))
    series_dir = tmp / "series"
    # 预置他集数据（模拟串行已跑过的集）：E00
    SB.merge_episode(series_dir, episode_id="E00", title="前集",
                     scenes=[{"sceneKey": "河堤", "heading": "SC-E00-001",
                              "card": {"space": "河堤"}, "tier": 2,
                              "decisionSource": "llm", "assets": {"base": "/e00.png"},
                              "action": "generated"}],
                     usage=[{"sceneKey": "河堤", "tier": 2, "action": "generated"}],
                     cast=[{"characterId": "少女", "name": "少女",
                            "appearance": "银灰", "anchor": "/girl.png"}],
                     shots=1, episode_scene_ids=["SC-E00-001"])

    # ---- 第 1 集：站台 + 河堤 ----
    wd1 = tmp / "out" / "E01"
    (wd1 / "delivery").mkdir(parents=True)
    r1 = _make_runner(wd1, series_dir, "E01", "第一集")
    rep1 = r1._run_scene_views(
        picked=[SimpleNamespace(scene_id="SC-E01-001"),
                SimpleNamespace(scene_id="SC-E01-002")],
        director_info={},
        script=_script(("SC-E01-001", "地铁站台·夜"), ("SC-E01-002", "河堤·夜")))
    d1 = SB.load(series_dir)
    dev1 = json.loads((wd1 / "delivery" / "series.json").read_text(encoding="utf-8"))
    check("I1 ★★★ 第 1 集回写：series.json 有 E01、2 场、usage 2",
          "E01" in [e["episodeId"] for e in d1["episodes"]]
          and len(d1.get("usage") or ()) == 3   # E00 的 1 条 + E01 的 2 条
          and "地铁站台" in d1["scenes"] and "河堤" in d1["scenes"],
          f"eps={[e['episodeId'] for e in d1['episodes']]} "
          f"scenes={list(d1['scenes'])} usage={len(d1.get('usage') or ())}")
    check("I2 ★★★ 预置他集 E00 数据未丢（河堤 episodes 含 E00）",
          d1["scenes"]["河堤"].get("episodes") == ["E00", "E01"],
          str(d1["scenes"]["河堤"].get("episodes")))
    check("I3 ★★ delivery/series.json 形状不变（kind/seriesId/episodeId/summary/actions）",
          isinstance(dev1, dict)
          and dev1.get("kind") == "series.state"
          and dev1.get("seriesId") == "series"
          and dev1.get("episodeId") == "E01"
          and isinstance(dev1.get("summary"), dict)
          and dev1["summary"].get("episodes") == 2
          and isinstance(dev1.get("actions"), dict)
          and sum(dev1["actions"].values()) == 2,
          str(dev1)[:180])
    check("I4 ★★ 回写返回 rep 且 rep.seriesDir 指向系列目录",
          isinstance(rep1, dict) and rep1.get("seriesDir") == str(series_dir)
          and len(rep1.get("scenes") or {}) == 2, str(rep1.get("seriesDir")))

    # ---- 第 2 集：站台 + 医院走廊（共享场景跨集合并） ----
    wd2 = tmp / "out" / "E02"
    (wd2 / "delivery").mkdir(parents=True)
    r2 = _make_runner(wd2, series_dir, "E02", "第二集")
    rep2 = r2._run_scene_views(
        picked=[SimpleNamespace(scene_id="SC-E02-001"),
                SimpleNamespace(scene_id="SC-E02-002")],
        director_info={},
        script=_script(("SC-E02-001", "地铁站台·夜"), ("SC-E02-002", "医院走廊·夜")))
    d2 = SB.load(series_dir)
    check("I5 ★★★ 两集累计：episodes=3 / scenes=3 / usage=5（无丢集/丢场）",
          sorted(e["episodeId"] for e in d2["episodes"]) == ["E00", "E01", "E02"]
          and len(d2["scenes"]) == 3
          and len(d2.get("usage") or ()) == 5,
          f"eps={sorted(e['episodeId'] for e in d2['episodes'])} "
          f"scenes={len(d2['scenes'])} usage={len(d2.get('usage') or ())}")
    check("I6 ★★★ 共享场景跨集合并：地铁站台 episodes=[E01,E02] 且 usage=2",
          d2["scenes"]["地铁站台"].get("episodes") == ["E01", "E02"]
          and d2["scenes"]["地铁站台"].get("usage") == 2,
          str(d2["scenes"]["地铁站台"].get("episodes")))
    check("I7 ★★ 每集 episode.scenes 存的是场景 id（沿用旧行为）",
          (d2["episodes"][1].get("scenes") == ["SC-E01-001", "SC-E01-002"]
           and d2["episodes"][2].get("scenes") == ["SC-E02-001", "SC-E02-002"]),
          str([e.get("scenes") for e in d2["episodes"]]))
    _files = sorted(p.name for p in series_dir.iterdir())
    check("I8 ★★ 无锁文件 / 无 .tmp 残留",
          _files == ["series.json"], str(_files))

    # ---- 失败不阻断：merge_episode 抛异常仍然返回 rep + 写 delivery ----
    _orig = SB.merge_episode

    def _boom(*a, **k):
        raise RuntimeError("模拟写台账失败")

    SB.merge_episode = _boom
    wd3 = tmp / "out" / "E03"
    (wd3 / "delivery").mkdir(parents=True)
    r3 = _make_runner(wd3, series_dir, "E03", "第三集")
    try:
        rep3 = r3._run_scene_views(
            picked=[SimpleNamespace(scene_id="SC-E03-001")],
            director_info={},
            script=_script(("SC-E03-001", "天台·夜")))
        _raised = False
    except Exception as e:                                    # noqa: BLE001
        rep3, _raised = None, f"{type(e).__name__}: {e}"
    SB.merge_episode = _orig
    dev3 = json.loads((wd3 / "delivery" / "series.json").read_text(encoding="utf-8"))
    d3 = SB.load(series_dir)
    check("I9 ★★★ 合并失败不阻断：仍返回 rep + 写 delivery/series.json，台账不被破坏",
          _raised is False and isinstance(rep3, dict)
          and dev3.get("episodeId") == "E03"
          and "E03" not in [e["episodeId"] for e in d3["episodes"]]
          and len(d3["scenes"]) == 3,
          f"raised={_raised} eps={[e['episodeId'] for e in d3['episodes']]}")

    n = sum(1 for _, ok, _ in _R if ok)
    print()
    for name, ok, detail in _R:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 70)
    print(f"结果：{n}/{len(_R)} 通过")
    return 0 if n == len(_R) else 1


if __name__ == "__main__":
    raise SystemExit(main())

