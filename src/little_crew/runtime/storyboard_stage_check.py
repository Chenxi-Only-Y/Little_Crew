"""逐场分镜合并自检（假 orch，不调模型）。"""
from __future__ import annotations

import sys

from little_crew.runtime import storyboard_stage as SS

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def _dump():
    return None


class _Shot:
    def __init__(self, sid, scene, secs=5.0):
        self.shot_id, self.scene_id, self.duration_sec = sid, scene, secs


class _SL:
    def __init__(self, shots):
        self.shots = tuple(shots)


class _BB:
    def __init__(self):
        self.v = None
        self.puts = 0

    def value(self, k):
        return self.v

    def put(self, k, val, owner=""):
        self.v = val
        self.puts += 1


class _Out:
    def __init__(self, ok=True, err=""):
        self.ok, self.error = ok, err


class _Orch:
    """按 payload 里的场次生成对应镜头（模拟真实 agent）。"""

    def __init__(self, per=2, fail_scenes=(), zero_scenes=()):
        self.blackboard = _BB()
        self.per = per
        self.fail_scenes = set(fail_scenes)
        self.zero_scenes = set(zero_scenes)
        self.calls = 0

    def run_stage(self, rid, cap, payload):
        self.calls += 1
        draft = payload["script.final"]
        scene = (getattr(draft, "scenes", ()) or [{}])[0]
        sid = scene.get("scene_id") or scene.get("id")
        if sid in self.fail_scenes:
            return _Out(False, "bad-json")
        if sid in self.zero_scenes:
            self.blackboard.v = _SL([])
            return _Out(True, "")
        self.blackboard.v = _SL([_Shot(f"{sid}-{i}", sid) for i in range(self.per)])
        return _Out(True, "")


class _D:
    def __init__(self, scenes):
        self.scenes = tuple(scenes)
        self.episode_id = "E1"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("逐场分镜合并 —— 自检")
    print("=" * 66)
    scenes = [{"scene_id": f"S{i:02d}"} for i in range(1, 5)]
    draft = _D(scenes)

    print(); print("A. 正常合并")
    o = _Orch(per=2)
    r = SS.per_scene(o, draft)
    check("A1 ★★★ 合并成功", r["ok"], r["reason"])
    check("A2 ★★★ 每场都收到（2×4=8 镜）", len(r["shots"]) == 8, str(len(r["shots"])))
    check("A3 ★★★ 场号都覆盖", r["scenesCovered"] == 4, str(r["scenesCovered"]))
    check("A4 ★★★ **不再只剩最后一场**",
          set(r["shotsByScene"]) == {"S01", "S02", "S03", "S04"}, str(r["shotsByScene"]))
    check("A5 ★★ 合并结果写回黑板", o.blackboard.puts == 1 and o.blackboard.v is not None, "")
    check("A6 ★★ 总时长=各镜之和", abs(o.blackboard.v.total_seconds - 40.0) < 0.01,
          str(o.blackboard.v.total_seconds))

    print(); print("B. 失败/空场")
    o2 = _Orch(per=2, zero_scenes=("S03",))
    r2 = SS.per_scene(o2, draft, retries=0)
    check("B1 ★★★ 某场 0 镜 -> 整体失败", r2["ok"] is False, r2["reason"])
    check("B2 ★★★ 说明是哪一场", "S03" in r2["reason"], r2["reason"])
    check("B3 ★★ 不丢已成功的场", r2["scenesCovered"] == 3, str(r2["scenesCovered"]))
    o3 = _Orch(per=2, fail_scenes=("S02",))
    r3 = SS.per_scene(o3, draft, retries=1)
    check("B4 ★★ 失败会重试（多打一次）", o3.calls == 5, str(o3.calls))
    check("B5 ★★★ 重试仍失败 -> 整体失败", r3["ok"] is False, r3["reason"])

    print(); print("C. 边界")
    r4 = SS.per_scene(_Orch(), _D([]))
    check("C1 ★★ 没有场次 -> 明确失败", r4["ok"] is False and "场次" in r4["reason"],
          r4["reason"])
    o5 = _Orch(per=1)
    SS.per_scene(o5, draft)
    check("C2 ★★ 一场一镜也能合并", len(o5.blackboard.v.shots) == 4,
          str(len(o5.blackboard.v.shots)))

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
