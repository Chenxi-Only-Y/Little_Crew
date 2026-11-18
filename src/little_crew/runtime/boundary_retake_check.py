"""`runtime/boundary_retake.py` 自检（2026-11-18，0.8.x，B2 自动循环）。

覆盖：
- A. `hint_from_diffs`：三类画面 diff 映射 / judge 一句差异 / 契约 diff / 通用 hint /
     去重 / 长度上限 / 确定性 / 未知 diff 保留 / 重试后「仍被判」
- B. `run_retake_loop`：红->绿（2 次）/ 一直红（严格上限 2）/ 契约红不重渲 /
     无判官诚实升级 / 无边界镜 / 无 shot.json / renderer 失败 / 判官异常 /
     max_retries 生效 / 非红不补拍
- C. 证据文件：retake-loop.json / retake-escalation.json 字段
- D. 源码接线守卫：tools/series_run.py 用 run_retake_loop / --retake-max / --retake-red
- E. 向后兼容：plan/write/run_retake 签名与证据文件不变
"""
from __future__ import annotations

import inspect
import json
import sys
import tempfile
from pathlib import Path

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


ROOT = Path(__file__).resolve().parents[3]


def _png(path: Path, color=(30, 30, 30)) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (320, 180), color).save(path)
    return path


def _workdir(root: Path, n: int = 3, *, manifest: bool = True):
    """造 temp workdir：assets/shots/SH-001..n（可选 shot.json + 小 PNG）。"""
    from little_crew.runtime import shot_manifest as SM

    sids: list[str] = []
    _png(root / "assets" / "prev.png", (8, 8, 8))
    for i in range(1, int(n) + 1):
        sid = f"SH-CH01-001-{i:03d}"
        if manifest:
            out = _png(root / "assets" / "shots" / sid / f"{sid}.png",
                       (17 * i % 256, 30, 40))
            SM.upsert(root, shot_id=sid, prompt=f"站台接戏 第{i}镜", model="m",
                      out=str(out), status="ok")
        else:
            (root / "assets" / "shots" / sid).mkdir(parents=True, exist_ok=True)
        sids.append(sid)
    return sids


class _Renderer:
    """假渲染器：记录调用，可模拟失败。"""

    def __init__(self, root: Path, *, ok: bool = True, error: str = "云端渲染失败"):
        self.root = root
        self.ok = ok
        self.error = error
        self.calls: list[dict] = []

    def __call__(self, sid: str, prompt: str) -> dict:
        self.calls.append({"sid": sid, "prompt": prompt,
                           "n": len(self.calls) + 1})
        if not self.ok:
            return {"ok": False, "error": self.error}
        out = self.root / "assets" / "shots" / sid / f"retake-{len(self.calls)}.png"
        return {"ok": True, "out": str(out), "model": "m", "anchor": "",
                "reused": False}


def _visual_red(diffs=("空间不一致",), judge="换空间了"):
    return {"kind": "boundary.audit", "level": "red", "ok": False,
            "contract": {"kind": "boundary.contract", "level": "green",
                         "ok": True, "diffs": []},
            "visual": {"kind": "boundary.visual", "level": "red", "ok": False,
                       "diffs": list(diffs), "judge": judge},
            "retakeScope": "boundary_shots_only"}


def _green_boundary(level="green"):
    return {"kind": "boundary.audit", "level": level, "ok": level == "green",
            "contract": {"level": "green", "ok": True, "diffs": []},
            "visual": {"level": "green", "ok": True, "diffs": [], "judge": ""}}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("边界重拍 B2 自动循环（hint / 重试 / 升级 / 证据）—— 自检")
    print("=" * 66)

    from little_crew.runtime import boundary_retake as BR

    # ------------------------------------------------------------------ #
    print(); print("A. hint_from_diffs：diff -> 针对性中文 hint")
    h_space = BR.hint_from_diffs(_visual_red(("空间不一致",), "站台变医院"))
    check("A1 ★★★ 空间不一致 -> 同一空间/背景约束",
          "沿用上一章同一空间/背景/陈设，不要换景" in h_space, h_space)
    h_cost = BR.hint_from_diffs(_visual_red(("人物造型不一致",), "外套换了"))
    check("A2 ★★★ 人物造型不一致 -> 同一服装/发型/妆造约束",
          "人物沿用同一服装/发型/妆造，不要改造型" in h_cost, h_cost)
    h_light = BR.hint_from_diffs(_visual_red(("光线/时段不连续",), "白天变夜里"))
    check("A3 ★★★ 光线/时段不连续 -> 同一光线/时段/色温约束",
          "保持与上一章同一光线/时段/色温" in h_light, h_light)
    h_all = BR.hint_from_diffs(
        _visual_red(("空间不一致", "人物造型不一致", "光线/时段不连续"), "三项都变了"))
    check("A4 ★★★ 三类画面 diff 同时出现 -> 三条约束都在",
          all(x in h_all for x in ("同一空间/背景/陈设", "同一服装/发型/妆造",
                                   "同一光线/时段/色温")), h_all)
    check("A5 ★★★ judge 一句差异 -> 「上一版被判：…」",
          "上一版被判：三项都变了" in h_all, h_all)
    _hd = BR.hint_from_diffs({
        "level": "red",
        "contract": {"level": "red",
                     "diffs": ["placeKey: 上一章 exit='站台' ≠ 本章 entry='医院'",
                               "characters: 上一章有 ['旅人']，本章 entry 消失"]},
        "visual": {"level": "green", "diffs": [], "judge": ""}})
    check("A6 ★★★ 契约 diffs 逐条 -> 「契约要求：…」",
          _hd.count("契约要求：") == 2 and "placeKey" in _hd and "旅人" in _hd, _hd)
    h_empty = BR.hint_from_diffs({})
    check("A7 ★★ 空输入 -> 通用 hint（仍可执行）",
          h_empty == BR.GENERIC_HINT and "同一空间" in h_empty
          and "只补拍接戏镜" in h_empty, h_empty)
    h_dup = BR.hint_from_diffs(_visual_red(("空间不一致", "空间不一致", "空间不一致"),
                                           "还是空间"))
    check("A8 ★★★ 重复 diff 去重（同一约束只拼一次）",
          h_dup.count("沿用上一章同一空间/背景/陈设，不要换景") == 1, h_dup)
    _many = {"level": "red",
             "contract": {"level": "red",
                          "diffs": [f"契约项{i}：" + "很长" * 60 for i in range(8)]},
             "visual": {"level": "red",
                        "diffs": ["空间不一致", "人物造型不一致", "光线/时段不连续"],
                        "judge": "差异" * 120}}
    h_many = BR.hint_from_diffs(_many)
    check("A9 ★★★ hint 总长有上限（≤400 字）",
          len(h_many) <= BR.MAX_HINT_CHARS, f"len={len(h_many)}")
    check("A10 ★★★ 确定性：同一输入两次生成完全相同",
          BR.hint_from_diffs(_many) == h_many, "")
    _jseg = h_many.split("上一版被判：", 1)[-1].split("；", 1)[0]
    check("A11 ★★ judge 长句被截断（<=80 字）",
          "上一版被判：" in h_many and len(_jseg) <= BR.JUDGE_CHARS,
          f"len={len(_jseg)}")
    h_unknown = BR.hint_from_diffs(_visual_red(("门把手方向反了",), ""))
    check("A12 ★★ 未知画面 diff 不丢信息（原样保留）",
          "门把手方向反了" in h_unknown, h_unknown)
    _still = _visual_red(("空间不一致",), "还是空间")
    _still["visual"]["stillRedDiffs"] = ["空间不一致"]
    _still["visual"]["retryCount"] = 1
    h_still = BR.hint_from_diffs(_still)
    check("A13 ★★★ 复审仍红 -> hint 出现「上一版仍被判：…」",
          "上一版仍被判：" in h_still and "必须严格" in h_still, h_still)
    check("A14 ★★ 非 Mapping 输入不崩（按空输入处理）",
          BR.hint_from_diffs(None) == BR.GENERIC_HINT, "")

    # ------------------------------------------------------------------ #
    print(); print("B. run_retake_loop：红 -> 绿（第 1 次红、第 2 次绿）")
    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        sids = _workdir(wd)
        r = _Renderer(wd)
        ctxs: list[dict] = []

        def judge(ctx):
            ctxs.append(dict(ctx))
            if len(ctxs) == 1:
                return {"level": "red",
                        "visual": {"level": "red",
                                   "diffs": ["人物造型不一致"],
                                   "judge": "造型也变了"}}
            return {"level": "green",
                    "visual": {"level": "green", "diffs": [], "judge": ""}}

        out = BR.run_retake_loop(wd, _visual_red(), renderer=r, judge=judge,
                                 episode_id="E01")
        check("B1 ★★★ 第 1 次红、第 2 次绿 -> attempts=2",
              out["attempts"] == 2, str(out.get("attempts")))
        check("B2 ★★★ 最终 green：ok=True / escalated=False / level=green",
              out["ok"] is True and out["escalated"] is False
              and out["level"] == "green", str(out))
        check("B3 ★★★ cost = 2 次 × 3 镜 × ¥0.05",
              abs(float(out["costCny"]) - 0.30) < 1e-6, str(out.get("costCny")))
        check("B4 ★★★ history 长度 2 且每次成本 ¥0.15",
              len(out["history"]) == 2
              and all(abs(float(h["costCny"]) - 0.15) < 1e-6
                      for h in out["history"]), str(out.get("history")))
        h1 = out["history"][0]["hint"]
        h2 = out["history"][1]["hint"]
        check("B5 ★★★ 第二次 hint 与第一次不同且含「仍被判」",
              h2 != h1 and "仍被判" in h2, h2)
        check("B6 ★★★ 第二次 hint 合并了复审新 diff（造型）",
              "人物沿用同一服装/发型/妆造" in h2
              and "人物沿用同一服装/发型/妆造" not in h1, f"h1={h1} | h2={h2}")
        check("B7 ★★★ shot.json version 递增（初版 1 -> 3）",
              all(int(json.loads(
                  (wd / "assets" / "shots" / sid / "shot.json")
                  .read_text(encoding="utf-8"))["version"]) == 3 for sid in sids),
              "")
        _lp = wd / "delivery" / "retake-loop.json"
        _ldoc = json.loads(_lp.read_text(encoding="utf-8")) if _lp.is_file() else {}
        check("B8 ★★★ retake-loop.json 落盘且字段齐全",
              _ldoc.get("kind") == "boundary.retake.loop"
              and _ldoc.get("attempts") == 2 and _ldoc.get("maxRetries") == 2
              and _ldoc.get("episodeId") == "E01"
              and isinstance(_ldoc.get("history"), list), str(_ldoc)[:300])
        check("B9 ★★ 非升级 -> 不写 retake-escalation.json",
              not (wd / "delivery" / "retake-escalation.json").is_file(), "")
        check("B10 ★★ judge ctx 含 attempt/hint/shots/result/workdir",
              all(k in ctxs[0] for k in ("attempt", "hint", "shots",
                                         "result", "workdir")), str(list(ctxs[0])))
        check("B11 ★★ renderer 调用 = 2 轮 × 3 镜 = 6 次（没多跑）",
              len(r.calls) == 2 * 3, str(len(r.calls)))

    # ------------------------------------------------------------------ #
    print(); print("C. run_retake_loop：一直红 -> 严格 max_retries 后升级人工")
    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        _workdir(wd)
        r = _Renderer(wd)

        def judge_red(ctx):
            return {"level": "red",
                    "visual": {"level": "red",
                               "diffs": ["光线/时段不连续"],
                               "judge": "光线还是不对"}}

        out = BR.run_retake_loop(wd, _visual_red(("光线/时段不连续",), "光线不对"),
                                 renderer=r, judge=judge_red, episode_id="E02")
        check("C1 ★★★ 一直红 -> attempts == max_retries(2)",
              out["attempts"] == 2, str(out.get("attempts")))
        check("C2 ★★★ 一直红 -> escalated=True / needsHuman=True / ok=False",
              out["escalated"] is True and out["needsHuman"] is True
              and out["ok"] is False and out["level"] == "red", str(out)[:300])
        check("C3 ★★★ 不会第 3 次补拍（2 轮 × 3 镜 = 6 次）",
              len(r.calls) == 2 * 3, str(len(r.calls)))
        _ep = wd / "delivery" / "retake-escalation.json"
        esc = json.loads(_ep.read_text(encoding="utf-8")) if _ep.is_file() else {}
        check("C4 ★★★ 升级证据 retake-escalation.json 落盘",
              esc.get("kind") == "boundary.escalation", str(esc.get("kind")))
        check("C5 ★★★ escalation.needsHuman=true", esc.get("needsHuman") is True, "")
        check("C6 ★★★ escalation.suggestedActions 恰好 3 条",
              isinstance(esc.get("suggestedActions"), list)
              and len(esc["suggestedActions"]) == 3, str(esc.get("suggestedActions")))
        check("C7 ★★★ escalation 字段齐全",
              all(k in esc for k in ("reason", "level", "attempts", "costCny",
                                     "lastHint", "lastDiffs", "history",
                                     "createdAt")) and "T" in str(esc.get("createdAt")),
              str(sorted(esc)))
        check("C8 ★★ escalation.lastHint / lastDiffs 非空（人工可定位）",
              bool(esc.get("lastHint")) and bool(esc.get("lastDiffs")), str(esc)[:300])
        check("C9 ★★ escalation 成本 = 2 次 × 3 镜 × ¥0.05",
              abs(float(esc.get("costCny") or 0) - 0.30) < 1e-6,
              str(esc.get("costCny")))
        check("C10 ★★ 升级原因说明重试耗尽（含「仍红」）",
              "仍红" in str(esc.get("reason")), str(esc.get("reason")))

    # ------------------------------------------------------------------ #
    print(); print("D. 契约红：补拍图片修不了契约 -> 不重渲，直接升级")
    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        _workdir(wd)
        calls: list[str] = []

        def sentinel(sid, prompt):          # 被调用即失败：契约红不该重渲
            calls.append(sid)
            raise AssertionError("契约红不该调用 renderer")

        contract_red = {"level": "red",
                        "contract": {"level": "red",
                                     "diffs": ["placeKey: 上一章 exit='站台' ≠ 本章 entry='医院'"]},
                        "visual": {"level": "green", "diffs": [], "judge": ""}}
        crashed = ""
        out: dict = {}
        try:
            out = BR.run_retake_loop(wd, contract_red, renderer=sentinel,
                                     judge=lambda ctx: {"level": "green"},
                                     episode_id="E03")
        except Exception as e:                                # noqa: BLE001
            crashed = f"{type(e).__name__}: {e}"
        check("D1 ★★★ 契约红 -> renderer 一次都不调用（哨兵未触发）",
              calls == [] and not crashed, f"calls={calls} crash={crashed}")
        check("D2 ★★★ 契约红 -> attempts=0 / escalated=True",
              out.get("attempts") == 0 and out.get("escalated") is True, str(out)[:300])
        check("D3 ★★★ 契约红升级原因含「契约」",
              "契约" in str(out.get("reason")), str(out.get("reason")))
        check("D4 ★★ 契约红也写 escalation（needsHuman=true）",
              (wd / "delivery" / "retake-escalation.json").is_file(), "")
        _ce = json.loads((wd / "delivery" / "retake-escalation.json")
                         .read_text(encoding="utf-8")) if (
            wd / "delivery" / "retake-escalation.json").is_file() else {}
        check("D5 ★★ 契约红升级建议 3 条且提到章卡/契约",
              len(_ce.get("suggestedActions") or []) == 3
              and "章卡" in str(_ce.get("suggestedActions")), str(_ce.get("suggestedActions")))

    # ------------------------------------------------------------------ #
    print(); print("E. 诚实边界：无判官 / 无边界镜 / 无 shot.json / 失败可见")
    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        _workdir(wd)
        r = _Renderer(wd)
        out = BR.run_retake_loop(wd, _visual_red(), renderer=r, judge=None)
        check("E1 ★★★ 画面红但 judge=None -> escalated=True 且原因含「无复审」",
              out["escalated"] is True and "无复审" in str(out["reason"]),
              str(out.get("reason")))
        check("E2 ★★★ judge=None 不假装补拍（renderer 0 次 / attempts=0）",
              len(r.calls) == 0 and out["attempts"] == 0,
              f"calls={len(r.calls)} attempts={out['attempts']}")
        check("E3 ★★ judge=None 也写升级证据",
              (wd / "delivery" / "retake-escalation.json").is_file(), "")

    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        wd.mkdir(parents=True, exist_ok=True)
        out = BR.run_retake_loop(wd, _visual_red(), renderer=_Renderer(wd),
                                 judge=lambda ctx: {"level": "red"})
        check("E4 ★★★ 没有边界镜（retakeShots 空）-> 诚实升级不重渲",
              out["escalated"] is True and out["attempts"] == 0
              and ("边界镜" in str(out["reason"]) or "shot.json" in str(out["reason"])),
              str(out.get("reason")))

    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        _workdir(wd, manifest=False)        # 有目录但没有 shot.json
        out = BR.run_retake_loop(wd, _visual_red(), renderer=_Renderer(wd),
                                 judge=lambda ctx: {"level": "red"})
        check("E5 ★★★ 缺 shot.json -> 进程不崩、升级原因可见",
              out["escalated"] is True and out["attempts"] == 1
              and ("失败" in str(out["reason"]) or "shot.json" in str(out["reason"])),
              str(out.get("reason")))

    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        _workdir(wd)
        out = BR.run_retake_loop(wd, _visual_red(), renderer=_Renderer(wd, ok=False),
                                 judge=lambda ctx: {"level": "green"})
        check("E6 ★★ renderer 失败 -> 升级人工、成本 ¥0、不假装绿",
              out["escalated"] is True and abs(float(out["costCny"])) < 1e-9
              and "失败" in str(out["reason"]), str(out)[:300])

    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        _workdir(wd)

        def judge_boom(ctx):
            raise RuntimeError("VLM 超时")

        out = BR.run_retake_loop(wd, _visual_red(), renderer=_Renderer(wd),
                                 judge=judge_boom)
        check("E7 ★★ 判官异常 -> 升级人工（不假装复审通过）",
              out["escalated"] is True and "判官异常" in str(out["reason"])
              and out["ok"] is False, str(out.get("reason")))

    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        _workdir(wd)
        r = _Renderer(wd)

        def judge_red(ctx):
            return {"level": "red", "diffs": ["空间不一致"], "judge": "还是换景"}

        out = BR.run_retake_loop(wd, _visual_red(), renderer=r, judge=judge_red,
                                 max_retries=1)
        check("E8 ★★★ max_retries=1 生效（attempts=1 / 1 轮 3 镜 / 升级）",
              out["attempts"] == 1 and len(r.calls) == 3
              and out["escalated"] is True and out["maxRetries"] == 1,
              str(out)[:300])
        check("E9 ★★ max_retries=1 成本 = 1 次 × 3 镜 × ¥0.05",
              abs(float(out["costCny"]) - 0.15) < 1e-6, str(out.get("costCny")))

    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        _workdir(wd)
        r = _Renderer(wd)
        out = BR.run_retake_loop(wd, _visual_red(), renderer=r,
                                 judge=lambda ctx: {"level": "green"},
                                 max_retries=0)
        check("E10 ★★ max_retries=0 -> 不重渲直接升级",
              out["escalated"] is True and out["attempts"] == 0
              and len(r.calls) == 0 and "上限为 0" in str(out["reason"]),
              str(out.get("reason")))

    # ------------------------------------------------------------------ #
    print(); print("F. 非红：green / yellow 不补拍、不写升级")
    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        _workdir(wd)
        r = _Renderer(wd)
        out = BR.run_retake_loop(wd, _green_boundary("green"), renderer=r,
                                 judge=lambda ctx: {"level": "green"})
        check("F1 ★★★ green -> needRetake=False / attempts=0 / ok=True",
              out["needRetake"] is False and out["attempts"] == 0
              and out["ok"] is True and out["escalated"] is False, str(out)[:300])
        check("F2 ★★ green -> 不调用 renderer、不写 escalation",
              len(r.calls) == 0
              and not (wd / "delivery" / "retake-escalation.json").is_file(), "")
        out2 = BR.run_retake_loop(wd, _green_boundary("yellow"), renderer=r,
                                  judge=lambda ctx: {"level": "green"})
        check("F3 ★★ yellow -> 同样不补拍不升级",
              out2["attempts"] == 0 and out2["escalated"] is False
              and out2["ok"] is True, str(out2)[:300])
        check("F4 ★★ 非红也落 retake-loop.json（总账可查）",
              (wd / "delivery" / "retake-loop.json").is_file(), "")

    # ------------------------------------------------------------------ #
    print(); print("G. retake_shots 显式指定 + 向后兼容旧接口")
    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        sids = _workdir(wd)
        r = _Renderer(wd)
        out = BR.run_retake_loop(wd, _visual_red(), retake_shots=[sids[1]],
                                 renderer=r,
                                 judge=lambda ctx: {"level": "green"})
        check("G1 ★★★ retake_shots 显式指定时只补拍这些镜",
              out["retakeShots"] == [sids[1]] and len(r.calls) == 1
              and r.calls[0]["sid"] == sids[1] and abs(float(out["costCny"]) - 0.05) < 1e-6,
              str(r.calls))
        rp = BR.plan({"level": "red"}, wd)
        check("G2 ★★★ plan 签名/字段向后兼容",
              rp["kind"] == "boundary.retake" and rp["needRetake"] is True
              and rp["scope"] == "boundary_shots_only"
              and len(rp["retakeShots"]) == 3, str(rp)[:200])
        pth = BR.write(wd, rp)
        check("G3 ★★★ retake-plan.json 证据文件保持不变",
              pth.name == "retake-plan.json" and pth.is_file(), str(pth))
        rr = BR.run_retake(wd, [sids[0]], "同空间同光线", renderer=_Renderer(wd))
        check("G4 ★★★ run_retake 证据 retake-result.json 保持不变",
              rr["kind"] == "boundary.retake.result" and rr["ok"] is True
              and (wd / "delivery" / "retake-result.json").is_file(), "")
        sig_plan = list(inspect.signature(BR.plan).parameters)
        sig_run = list(inspect.signature(BR.run_retake).parameters)
        check("G5 ★★ plan/run_retake 参数列表未变（调用方不受影响）",
              sig_plan == ["boundary", "workdir", "max_shots"]
              and sig_run == ["workdir", "shots", "hint", "renderer"],
              f"{sig_plan} / {sig_run}")
        check("G6 ★★ __all__ 导出新接口（hint_from_diffs / run_retake_loop）",
              "hint_from_diffs" in BR.__all__ and "run_retake_loop" in BR.__all__, "")

    # ------------------------------------------------------------------ #
    print(); print("H. 源码接线守卫：tools/series_run.py")
    _src_path = ROOT / "tools" / "series_run.py"
    src = _src_path.read_text(encoding="utf-8") if _src_path.is_file() else ""
    check("H1 ★★★ series_run 接线 run_retake_loop",
          "run_retake_loop" in src, "")
    check("H2 ★★★ 新增 --retake-max（默认 2 传入 max_retries）",
          "--retake-max" in src and "max_retries=a.retake_max" in src, "")
    check("H3 ★★★ --retake-red 参数保留",
          "--retake-red" in src and "a.retake_red" in src, "")
    check("H4 ★★★ 旧硬编码 hint 已移除（不再直接 run_retake）",
          "保持与上一章同一空间/同一背景/同一光线；只补拍接戏镜" not in src
          and "_brt.run_retake(" not in src and "_brt2.run_retake(" not in src, "")
    check("H5 ★★★ 主循环 + 续跑分支都走 run_retake_loop（>=2 处）",
          src.count("run_retake_loop(") >= 2, str(src.count("run_retake_loop(")))
    check("H6 ★★★ 行留痕字段齐全",
          all(k in src for k in ('"retakeAttempts"', '"retakeEscalated"',
                                 '"retakeLevel"', '"retakeResult"')), "")
    check("H7 ★★★ 汇总留痕 retakeEscalated / retakeCostCny",
          "retakeEscalated" in src and "retakeCostCny" in src, "")
    check("H8 ★★★ run-summary.md 表格新增「重拍」列",
          "重拍 |" in src or "| 重拍 |" in src, "")
    check("H9 ★★ 画面红 judge 闭包接线（audit_visual + 边界帧选择）",
          "audit_visual" in src and "prev_frames" in src
          and "shot_manifest" in src, "")
    check("H10 ★★ stdout 打印 hint / attempts / 是否升级",
          "边界重拍：attempts=" in src and "升级人工" in src
          and "hint=" in src, "")

    # ------------------------------------------------------------------ #
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
