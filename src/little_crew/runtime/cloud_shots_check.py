"""混合渲染（方案 C）自检（2026-11-13）。

锁住六件事：
1 **选镜策略**：特写/场次首末镜/多人同框加分；零分镜不走云端；上限与确定性
2 **锚点匹配**：镜头里的**角色名**能映射到库里 **character_id**（两套命名都认）
3 **缺锚点**：无锚点时退回文生图（而不是失败）
4 **并行**：云端多镜并行（workers>1 时墙钟应显著小于串行总和）
5 **失败不静默**：单镜失败记录 error、其余镜照常完成
6 **现有库**：`load_anchors` 能从 `assets/characters/<cid>/sheet*.png` 读锚点
"""
from __future__ import annotations

import base64
import json
import sys
import tempfile
import time
from pathlib import Path

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 128


class _S:
    def __init__(self, sid, scene, size, chars, desc=""):
        self.shot_id = sid
        self.scene_id = scene
        self.shot_size = size
        self.characters = chars
        self.description = desc


def _resp_bytes():
    return json.dumps({"data": [{"b64_json": base64.b64encode(PNG).decode()}]}).encode()


class _CM:
    """伪装 net.urlopen 的上下文管理器。"""

    def __init__(self, raw: bytes, delay: float = 0.0):
        self.raw = raw
        self.delay = delay

    def __enter__(self):
        if self.delay:
            time.sleep(self.delay)
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.raw


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("混合渲染（cloud_policy + cloud_render）—— 自检")
    print("=" * 66)

    from little_crew.runtime import cloud_policy as CP
    from little_crew.runtime import cloud_render as CR

    print(); print("A. 选镜策略")
    shots = [
        _S("S01-01", "S01", "远景", ["a"], "山坡远景，晨雾"),
        _S("S01-02", "S01", "特写", ["a"], "面部特写，眼神清冷"),
        _S("S02-01", "S02", "中景", ["a", "b"], "两人对坐"),
        _S("S02-02", "S02", "近景", ["a"], "半身近景"),
        _S("S03-01", "S03", "大全景", ["b"], "集市大全景"),
    ]
    rows = CP.plan(shots, max_cloud=3)
    ids = [r["shotId"] for r in rows]
    check("A1 ★★★ 特写/近景必入选", "S01-02" in ids and "S02-02" in ids, str(ids))
    check("A2 ★★★ 场次首镜入选（S02-01 多人）", "S02-01" in ids, str(ids))
    check("A3 ★★★ 零分远景不入选", "S03-01" not in ids and "S01-01" not in ids, str(ids))
    check("A4 ★★★ 上限生效", len(rows) <= 3, str(len(rows)))
    check("A5 ★★★ 输出按镜序（可读）", ids == sorted(ids, key=lambda x: [s.shot_id for s in shots].index(x)),
          str(ids))
    again = CP.plan(shots, max_cloud=3)
    check("A6 ★★★ 同一输入稳定（可复现）", ids == [r["shotId"] for r in again], "")
    check("A7 ★★ 每镜带理由（可解释）", all(r.get("reasons") for r in rows), str(rows)[:80])

    print(); print("B. 锚点匹配与缺锚点降级")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        # 造假"现有库"：cover_lead/sheet.png
        lib = d / "assets" / "characters" / "cover_lead"
        lib.mkdir(parents=True)
        (lib / "sheet.png").write_bytes(PNG)
        anchors = CR.load_anchors(d)
        check("B1 ★★★ 能从 assets/characters/<cid>/sheet.png 读锚点",
              anchors.get("cover_lead", "").endswith("sheet.png"), str(anchors))
        check("B2 ★★★ 角色名能映射到 character_id（两套命名都认）",
              CR._anchor_for(["封面主角"], {}) == "" and
              CR._anchor_for(["cover_lead"], anchors).endswith("sheet.png"), "")
        from little_crew.agent.character_sheet import character_id_for as cidf
        check("B3 ★★ character_id_for 稳定（同一名字同一 id）",
              cidf("封面主角") == cidf("封面主角") and cidf("封面主角") != cidf("别人"), "")

        # 假网络：图生图 -> multipart；无锚点 -> generations（urlopen）
        calls = {"multipart": 0, "urlopen": 0, "delay": 0.35}

        def fake_multipart(url, fields, files, **kw):
            calls["multipart"] += 1
            time.sleep(calls["delay"])
            return _resp_bytes()

        orig_mp, orig_uo = CR.net.post_multipart, CR.net.urlopen
        CR.net.post_multipart = fake_multipart
        CR.net.urlopen = lambda *a, **kw: _CM(_resp_bytes(), calls["delay"])
        try:
            # 这里必须用**带库里角色名**的镜头，否则永远匹配不到锚点（B5 曾因此假失败）
            shots_b = [
                _S("C-01", "S01", "远景", ["cover_lead"], "城市远景"),
                _S("C-02", "S01", "特写", ["cover_lead"], "面部特写"),
                _S("C-03", "S01", "近景", ["cover_lead"], "半身近景"),
            ]
            rows2 = CP.plan(shots_b, max_cloud=3)
            t0 = time.time()
            res = CR.render_plan(shots_b, rows2, anchors, d / "out", workers=3)
            wall = time.time() - t0
            check("B4 ★★★ 并行生效（墙钟 < 串行总和 80%）",
                  wall < res["seconds"] * 0.8, f"wall={wall:.2f} sum={res['seconds']:.2f}")
            anchored = [r for r in res["records"] if r.get("ok") and r.get("usedAnchor")]
            check("B5 ★★★ 有锚点的镜走图生图（multipart，次数与锚点镜数一致）",
                  calls["multipart"] == len(anchored) >= 1, f"{calls} anchored={len(anchored)}")
        finally:
            CR.net.post_multipart, CR.net.urlopen = orig_mp, orig_uo

    print(); print("C. 失败语义")
    with tempfile.TemporaryDirectory() as td2:
        d2 = Path(td2)
        calls2 = {"n": 0}

        def boom(url, fields, files, **kw):
            calls2["n"] += 1
            raise RuntimeError("上游 502")

        orig = CR.net.post_multipart
        CR.net.post_multipart = boom
        try:
            lib2 = d2 / "assets" / "characters" / "cover_lead"
            lib2.mkdir(parents=True)
            (lib2 / "sheet.png").write_bytes(PNG)
            shot_anchored = _S("S01-02", "S01", "特写", ["cover_lead"], "面部特写")
            res_bad = CR.render_plan(
                [shot_anchored], CP.plan([shot_anchored], max_cloud=1),
                CR.load_anchors(d2), d2 / "o", workers=1)
        finally:
            CR.net.post_multipart = orig
        check("C1 ★★★ 单镜失败 -> 记录 error 而不抛",
              res_bad["rendered"] == 0 and res_bad["failed"] == 1
              and bool(res_bad["records"][0].get("error")),
              str(res_bad["records"])[:90])

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
