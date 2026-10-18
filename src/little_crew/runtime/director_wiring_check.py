"""`EpisodeRunner._run_director` 的单测 —— 假实现，不吃 API。

## 为什么放独立文件

`advisory_check.py` 的教训：往 `episode_check.py` 里加段落，
反复与既有 G/H 段标签冲突、两次切坏 `def main`。
**单开文件**与那个大文件解耦，标签也不会撞。

## 守什么

| 断言 | 守什么 |
|---|---|
| D1 | 没绑导演 -> 空结果（不是失败，是"没启用"） |
| D2 | 导演失败 -> `ok=False` **且有 reason**（"没有预算"与"预算为空"要分得清） |
| D3 | 导演成功 -> `ok=True` 且带回 plan |
| D4 | **plan 真的注入到分镜师的 `plan` 属性**（这是整件事的目的） |
| D5 | 分镜师**没有** `plan` 属性时 -> 不崩（不硬塞陌生属性） |
| D6 | 导演产出不是 Mapping -> 判失败而不是硬塞 |
| D7 | **调用顺序**：`_run_director` 在分镜之前（结构检查） |

## D7 为什么也算

导演的产物是"每场几个镜头"。**晚于分镜就毫无意义** ——
而这类"顺序写反"的错误不会报异常，只会让预算悄悄不生效。
"""
from __future__ import annotations

import inspect
import sys
from typing import Any

from little_crew.agent.messages import ShotList
from little_crew.agent.protocol import CallResult
from little_crew.runtime.episode import EpisodeRunner

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


PLAN = {"tone": "克制", "pacing": "前松后紧",
        "scenes": [{"sceneId": "S01", "shotBudget": 4, "focus": "犹豫"}],
        "motifs": ["雨"]}


class _OkDirector:
    """成功产出 episode.plan 的假导演。"""

    agent_id = "director"
    calls = 0

    def __call__(self, *, envelope: Any, runtime: Any) -> CallResult:
        type(self).calls += 1
        return CallResult.success({"episode.plan": dict(PLAN)})


class _BadDirector:
    agent_id = "director"

    def __call__(self, *, envelope: Any, runtime: Any) -> CallResult:
        return CallResult.fail("F-LLM", "模拟导演失败")


class _WeirdDirector:
    """产出不是 Mapping —— 应该判失败，而不是硬塞进分镜师。"""

    agent_id = "director"

    def __call__(self, *, envelope: Any, runtime: Any) -> CallResult:
        return CallResult.success({"episode.plan": "这不是一个 plan"})


class _StoryboardWithPlan:
    agent_id = "storyboard_artist"
    plan: Any = None

    def __call__(self, *, envelope: Any, runtime: Any) -> CallResult:
        return CallResult.success({"shot.list": ShotList(
            frm="storyboard_artist", to="*", stage="storyboard",
            episode_id="E1", shots=(), total_seconds=0.0)})


class _StoryboardNoPlan:
    """**没有 `plan` 属性**的分镜师 —— 不能硬塞，也不能崩。"""
    agent_id = "storyboard_artist"

    def __call__(self, *, envelope: Any, runtime: Any) -> CallResult:
        return CallResult.success({"shot.list": ShotList(
            frm="storyboard_artist", to="*", stage="storyboard",
            episode_id="E1", shots=(), total_seconds=0.0)})


def _orch(director: Any = None, storyboard: Any = None):
    from little_crew.agent.crew_stubs import default_crew_stubs
    from little_crew.agent.orchestrator import CrewOrchestrator

    o = CrewOrchestrator(model_families={})
    o.bind_many(default_crew_stubs())
    if director is not None:
        o.bind("director", director)
    if storyboard is not None:
        o.bind("storyboard_artist", storyboard)
    return o


class _Runner:
    """只带 `orch` 的替身 —— `_run_director` 只用到它。"""

    def __init__(self, orch: Any) -> None:
        self.orch = orch


def _behavior() -> None:
    print()
    print("A. 行为")

    # 没绑导演
    r0 = _Runner(_orch())
    check("D1 没绑导演 -> 空结果（是「没启用」，不是失败）",
          EpisodeRunner._run_director(r0, "剧本") == {}, "")

    # 导演失败
    r1 = _Runner(_orch(director=_BadDirector()))
    got1 = EpisodeRunner._run_director(r1, "剧本")
    check("D2 ★ 导演失败 -> ok=False 且有 reason",
          got1.get("ok") is False and bool(got1.get("reason")),
          str(got1))

    # 导演成功 + 分镜师有 plan 属性
    sb = _StoryboardWithPlan()
    _OkDirector.calls = 0
    r2 = _Runner(_orch(director=_OkDirector(), storyboard=sb))
    got2 = EpisodeRunner._run_director(r2, "剧本")
    check("D3 导演成功 -> ok=True 且带回 plan",
          got2.get("ok") is True and got2.get("plan") == PLAN, str(got2)[:120])
    check("D4 ★★ plan 真的注入到分镜师的 `plan` 属性（这是整件事的目的）",
          sb.plan == PLAN, str(sb.plan)[:120])
    check("D5 导演确实被调用了一次", _OkDirector.calls == 1,
          str(_OkDirector.calls))

    # 分镜师没有 plan 属性 -> 不崩
    r3 = _Runner(_orch(director=_OkDirector(),
                       storyboard=_StoryboardNoPlan()))
    got3 = EpisodeRunner._run_director(r3, "剧本")
    check("D6 ★ 分镜师没有 `plan` 属性 -> 不崩，也不硬塞",
          got3.get("ok") is True, str(got3)[:120])

    # 导演产出不是 Mapping
    r4 = _Runner(_orch(director=_WeirdDirector(),
                       storyboard=_StoryboardWithPlan()))
    got4 = EpisodeRunner._run_director(r4, "剧本")
    check("D7 ★ 导演产出不是 Mapping -> 判失败（不硬塞垃圾）",
          got4.get("ok") is False, str(got4)[:120])


def _structure() -> None:
    print()
    print("B. 结构约束")

    src = inspect.getsource(EpisodeRunner.run)
    i_dir = src.find("_run_director")
    i_sb = src.find('"storyboard_artist"')
    check("D8 ★★ 导演在分镜**之前**被调用（晚于分镜就毫无意义）",
          0 <= i_dir < i_sb, f"director@{i_dir} storyboard@{i_sb}")

    fn = inspect.getsource(EpisodeRunner._run_director)
    check("D9 用 `hasattr` 判分镜师有没有 `plan`（不硬塞陌生属性）",
          "hasattr" in fn)
    check("D10 失败路径不抛异常（导演挂了不该让整集停下）",
          "except Exception" in fn)


# ---------- 灯光师接线（与导演同一套模式）----------

class _OkGaffer:
    agent_id = "gaffer"

    def __call__(self, *, envelope: Any, runtime: Any) -> CallResult:
        return CallResult.success({"lighting.plan": {
            "scenes": [{"sceneId": "S01", "contrast": "高"}],
            "palette": ["靛蓝"]}})


class _BadGaffer:
    agent_id = "gaffer"

    def __call__(self, *, envelope: Any, runtime: Any) -> CallResult:
        return CallResult.fail("F-LLM", "模拟灯光师失败")


class _StoryboardWithLighting:
    agent_id = "storyboard_artist"
    lighting: Any = None

    def __call__(self, *, envelope: Any, runtime: Any) -> CallResult:
        return CallResult.success({"shot.list": ShotList(
            frm="storyboard_artist", to="*", stage="storyboard",
            episode_id="E1", shots=(), total_seconds=0.0)})


def _gaffer_wiring() -> None:
    print()
    print("C. 灯光师接线")

    r0 = _Runner(_orch())
    check("G1 没绑灯光师 -> 空结果（「没启用」，不是失败）",
          EpisodeRunner._run_gaffer(r0, "剧本") == {})

    r1 = _Runner(_orch())
    r1.orch.bind("gaffer", _BadGaffer())
    g1 = EpisodeRunner._run_gaffer(r1, "剧本")
    check("G2 灯光师失败 -> ok=False 且有 reason",
          g1.get("ok") is False and bool(g1.get("reason")), str(g1))

    sb = _StoryboardWithLighting()
    r2 = _Runner(_orch(storyboard=sb))
    r2.orch.bind("gaffer", _OkGaffer())
    g2 = EpisodeRunner._run_gaffer(r2, "剧本")
    check("G3 成功 -> ok=True 且带回 plan", g2.get("ok") is True)
    check("G4 ★★ lighting 真的注入到分镜师（与导演同一套机制）",
          isinstance(sb.lighting, dict)
          and sb.lighting.get("palette") == ["靛蓝"],
          str(sb.lighting)[:100])


def _order_both() -> None:
    """**顺序**：导演 -> 灯光 -> 分镜。三者写反任何一个，预算/光就不生效。"""
    src = inspect.getsource(EpisodeRunner.run)
    i_d = src.find("_run_director")
    i_g = src.find("_run_gaffer")
    i_sb = src.find('"storyboard_artist"')
    check("G5 ★★ 顺序正确：导演 -> 灯光 -> 分镜",
          0 <= i_d < i_g < i_sb, f"dir@{i_d} gaffer@{i_g} sb@{i_sb}")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")            # type: ignore[attr-defined]
    except Exception:                                        # noqa: BLE001
        pass
    print("=" * 74)
    print("导演接线（_run_director）—— 自检（假实现）")
    print("=" * 74)

    _behavior()
    _structure()
    _gaffer_wiring()
    _order_both()

    print()
    npass = sum(1 for _, ok, _ in _results if ok)
    for name, ok, detail in _results:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 74)
    print(f"结果：{npass}/{len(_results)} 通过")
    return 0 if npass == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
