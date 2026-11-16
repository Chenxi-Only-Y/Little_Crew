"""cameraDefaults 进导演计划 + runner 合并 —— 自检（2026-11-16）。

放在 `runtime/`：这里要测 EpisodeRunner 的合并逻辑，
而 `agent -> runtime` 是反向依赖（架构守卫 R1 会拦）。runtime -> agent 合法。
"""
from __future__ import annotations

import sys

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


class _Res:
    ok = True
    error = ""


class _BB:
    def __init__(self, plan: dict) -> None:
        self._plan = plan

    def value(self, key: str):
        return self._plan if key in ("episode.plan", "director.plan") else None


class _Orch:
    def __init__(self, plan: dict, sb=None) -> None:
        self.blackboard = _BB(plan)
        self._impls = {"director": object(),
                       "storyboard_artist": sb if sb is not None else object()}

    def run_stage(self, *a, **k):
        return _Res()


class _SB:
    plan = None


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("cameraDefaults（导演默认机位）—— 自检")
    print("=" * 66)

    from little_crew.agent.director_llm import DirectorLlm, normalize_plan

    print(); print("A. 计划规整：cameraDefaults 进 plan 且严格校验")
    plan = normalize_plan({
        "tone": "夜", "pacing": "慢",
        "cameraDefaults": {"focal": "35", "aperture": "F2.8",
                           "body": "s35", "lens": "vintage", "angle": "eye"},
        "scenes": [{"sceneId": "S01", "shotBudget": 4}],
    }, total_shots=12)
    check("A1 ★★★ cameraDefaults 只留合法字段并归一",
          plan["cameraDefaults"] == {"focal": "35mm", "aperture": "f2.8",
                                     "body": "s35", "lens": "vintage",
                                     "angle": "eye_level"},
          str(plan.get("cameraDefaults")))
    check("A2 ★★★ 缺省时为 {}（不塞空字段）",
          normalize_plan({"scenes": [{"sceneId": "S01", "shotBudget": 4}]},
                         total_shots=8)["cameraDefaults"] == {}, "")
    bad_ok = False
    try:
        normalize_plan({"cameraDefaults": {"focal": "500mm"},
                        "scenes": [{"sceneId": "S01", "shotBudget": 3}]})
    except ValueError as e:
        bad_ok = "cameraDefaults" in str(e)
    check("A3 ★★★ 非法 cameraDefaults 直接报错（不静默）", bad_ok, "")

    print(); print("B. 导演提示词包含 cameraDefaults 契约")
    llm = DirectorLlm(total_shots=12)
    prompt, _sys = llm._render({"script.draft": "[场1] 地铁站台·夜\n@少女 等人。"})
    check("B1 ★★★ 提示词要求 cameraDefaults", "cameraDefaults" in prompt, "")
    check("B2 ★★ 提示词点名合法取值（focal/angle）",
          "focal" in prompt and "eye_level" in prompt, "")
    check("B3 ★★ 提示词仍禁止越权写景别", "不要写景别" in prompt or "归分镜师" in prompt, "")

    print(); print("C. runner 合并：导演默认 -> camera_default（CLI 优先）")
    from little_crew.runtime.episode import EpisodeRunner

    sb = _SB()
    r = EpisodeRunner.__new__(EpisodeRunner)
    r.orch = _Orch({**plan, "cameraDefaults": {"focal": "35mm", "angle": "low"}}, sb)
    r.camera_default = None
    out = r._run_director("script")
    check("C1 ★★★ 导演计划默认值写进 runner.camera_default",
          getattr(r, "camera_default", None) == {"focal": "35mm", "angle": "low"},
          str(getattr(r, "camera_default", None)))
    check("C2 ★★★ 记录来源 director.plan",
          getattr(r, "_camera_default_source", "") == "director.plan", "")
    check("C3 ★★ 分镜师拿到 plan", isinstance(getattr(sb, "plan", None), dict), "")
    check("C4 ★★★ 返回 ok 且 plan 带 cameraDefaults",
          out.get("ok") and out["plan"].get("cameraDefaults"), "")

    r2 = EpisodeRunner.__new__(EpisodeRunner)
    r2.orch = _Orch({**plan, "cameraDefaults": {"focal": "35mm"}}, _SB())
    r2.camera_default = {"focal": "85mm"}       # CLI 显式
    r2._run_director("script")
    check("C5 ★★★ CLI 显式优先于导演默认",
          r2.camera_default == {"focal": "85mm"}, str(r2.camera_default))
    check("C6 ★★ 来源记录 cli", getattr(r2, "_camera_default_source", "") == "cli", "")

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
