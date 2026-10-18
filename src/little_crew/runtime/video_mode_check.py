"""`video_mode` **第二段**（`EpisodeRunner.render_video_stage`）—— 假通道，不吃 API。

## 为什么第二段要单独测

`minimax-h3` 的 `body_template` **强制要 `image_path`** —— 它是图生视频。
所以 `video_mode` 是两段式（先静帧、再动画化），而第二段**至今没被调用过**。

而第二段的反馈循环很贵：**2 镜 ≈ 12 分钟**，整集 ≈ 2.4 小时。
**用假通道把接线验完，再花那 12 分钟** —— 这是 `alpha.65` 定下的顺序。

## 守什么（重点是失败语义）

| 断言 | 守什么 |
|---|---|
| **V1** | **拿不到本地后端 -> 显式失败，绝不退回付费 API** |
| V2 | 有后端 -> 调 `render_videos`，产出片段 |
| **V3** | **部分成功 -> `ok=False` 但 `clips` 保留成功的**（半成品有用） |
| V4 | 缺提示词 / 缺静帧 -> 记进 `failures`（**不静默丢**） |
| V5 | 一个都不可动画化 -> 失败，且**不是"成功但空"** |
| V6 | 结构：源码里没有"退回付费 API"的路径 |
"""
from __future__ import annotations

import inspect
import sys
from typing import Any

from little_crew.runtime.episode import EpisodeRunner

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


class _Outcome:
    def __init__(self, ok: bool, artifacts: dict[str, bytes] | None = None,
                 failure: Any = None, reason: str = "") -> None:
        self.ok = ok
        self.artifacts = artifacts or {}
        self.failure = failure
        self.reason = reason
        self.from_cache = False


class _FakeRenderer:
    """假的 `CachingRenderer` —— 只实现 `render_http`。"""

    def __init__(self, *, fail_for: tuple[str, ...] = ()) -> None:
        self.fail_for = fail_for
        self.seen: list[dict[str, Any]] = []

    def render_http(self, values: Any, backend: Any, *, job_id: str = "",
                    **kw: Any) -> _Outcome:
        self.seen.append({"jobId": job_id, "values": dict(values)})
        if job_id in self.fail_for:
            return _Outcome(False, failure="job-failed", reason="模拟失败")
        return _Outcome(True, {f"{job_id}.mp4": b"\x00\x00\x00\x18ftypmp42"})


class _Runner:
    """只带 `renderer` 的替身。"""

    def __init__(self, renderer: Any) -> None:
        self.renderer = renderer


def _fake_backend():
    class _B:
        endpoint = "http://127.0.0.1:8100"
        path = "/v1/videos/generations"
        async_task = True
        status_path = "/v1/videos/generations/{task_id}"
        status_success_value = "succeeded"
        response_path = "result.url"
        body_template = '{"prompt": "{prompt}", "image_path": "{image_path}"}'
    return _B()


STILLS = {"S01-001": "C:/tmp/a.png", "S02-001": "C:/tmp/b.png"}
PROMPTS = {"S01-001": "镜头缓慢横移", "S02-001": "风吹过麦田"}


def _no_backend(monkeypatch_holder: dict) -> None:
    print()
    print("A. 拿不到本地后端")


def _behavior() -> None:
    from little_crew.runtime import h3_video

    print()
    print("B. 行为")

    # ---- V1：拿不到本地后端 ----
    orig = h3_video.h3_backend
    try:
        h3_video.h3_backend = lambda registry=None: None   # type: ignore[assignment]
        r = _Runner(_FakeRenderer())
        got = EpisodeRunner.render_video_stage(
            r, STILLS, PROMPTS, seconds=1.0)
        check("V1 ★★ 拿不到本地后端 -> 显式失败（绝不退回付费 API）",
              got["ok"] is False and got["clips"] == {} and got["backend"] is None,
              str(got)[:150])
        check("V2 理由点明「不退回付费 API」",
              "不退回付费 API" in str(got.get("reason", "")),
              str(got.get("reason"))[:100])
        check("V3 此时**没有**调用过渲染器（没白跑）",
              r.renderer.seen == [], str(len(r.renderer.seen)))
    finally:
        h3_video.h3_backend = orig          # type: ignore[assignment]

    # ---- V4：有后端，两镜都成功 ----
    try:
        h3_video.h3_backend = lambda registry=None: _fake_backend()  # type: ignore
        r = _Runner(_FakeRenderer())
        got = EpisodeRunner.render_video_stage(r, STILLS, PROMPTS,
                                              seconds=1.5)
        check("V4 ★ 有后端 -> 调 render_videos 并产出片段",
              got["ok"] is True and len(got["clips"]) == 2,
              f"ok={got['ok']} clips={list(got['clips'])}")
        check("V5 送进渲染器的 jobId 与镜头对应",
              {s["jobId"] for s in r.renderer.seen} == set(STILLS),
              str([s["jobId"] for s in r.renderer.seen]))
        vals = (r.renderer.seen or [{}])[0].get("values") or {}
        check("V6 入参里有 image_path 与 seconds（两段式的关键）",
              "image_path" in vals and "seconds" in vals, str(sorted(vals)))
        check("V7 seconds 透传（调用方改了就生效）",
              float(vals.get("seconds", 0)) == 1.5, str(vals.get("seconds")))

        # ---- V8：部分失败 ----
        r2 = _Runner(_FakeRenderer(fail_for=("S02-001",)))
        got2 = EpisodeRunner.render_video_stage(r2, STILLS, PROMPTS)
        check("V8 ★ 部分成功 -> ok=False，**但成功的那一镜保留**",
              got2["ok"] is False and len(got2["clips"]) == 1,
              f"ok={got2['ok']} clips={list(got2['clips'])}")
        check("V9 失败信息在 failures 里（可追溯）",
              bool(got2["failures"]), str(got2["failures"])[:100])

        # ---- V10：缺提示词 / 缺静帧 ----
        r3 = _Runner(_FakeRenderer())
        got3 = EpisodeRunner.render_video_stage(
            r3, {"S01-001": "C:/tmp/a.png", "S02-001": None},
            {"S01-001": "有词", "S02-001": "有词"})
        check("V10 ★ 缺静帧 -> 记进 failures（不静默丢）",
              any(f.get("why") for f in got3["failures"]),
              str(got3["failures"])[:100])
        check("V11 缺静帧的那镜**不会**被送进渲染器",
              [s["jobId"] for s in r3.renderer.seen] == ["S01-001"],
              str([s["jobId"] for s in r3.renderer.seen]))

        r4 = _Runner(_FakeRenderer())
        got4 = EpisodeRunner.render_video_stage(
            r4, {"S01-001": "C:/tmp/a.png"}, {"S01-001": "  "})
        check("V12 ★ 缺提示词 -> 记进 failures",
              any("提示词" in str(f.get("why")) for f in got4["failures"]),
              str(got4["failures"])[:100])

        # ---- V13：一个都不可动画化 ----
        r5 = _Runner(_FakeRenderer())
        got5 = EpisodeRunner.render_video_stage(r5, {}, {})
        check("V13 ★ 一个都不可动画化 -> 失败（不是「成功但空」）",
              got5["ok"] is False and got5["clips"] == {}, str(got5)[:120])
        check("V14 此时也没调用过渲染器", r5.renderer.seen == [])
    finally:
        h3_video.h3_backend = orig          # type: ignore[assignment]


def _structure() -> None:
    print()
    print("C. 结构约束")
    src = inspect.getsource(EpisodeRunner.render_video_stage)
    check("V15 ★ 有显式的「不退回付费 API」守卫",
          "不退回付费 API" in src, "没找到守卫说明")
    check("V16 守卫在**调用渲染器之前**（先判后端再动手）",
          src.find("h3_backend()") < src.find("render_videos("),
          "顺序不对")
    check("V17 用 `h3_backend()` 选后端（不在本地硬编码 URL）",
          "h3_backend()" in src and "8100" not in src, "源码里有硬编码端口")

    # 全局：整个模块不该出现"退回云端"这类路径
    mod = inspect.getsource(sys.modules["little_crew.runtime.episode"])
    check("V18 模块里没有「拿不到本地就换云端」的兜底",
          "minimax_api" not in mod and "fallback_to_paid" not in mod)


# ---------- D. V2：接进 run() 的接线 ----------
#
# V1 只验了"第二段本身能用"。V2 验的是"`run()` 会不会自动叫它、
# 顺序对不对、失败会不会污染前面的结论"。
#
# **顺序这条最容易写错**：H3 要拿静帧当首帧 —— 若第二段跑在 `_render`
# 之前，它拿到的就是空。而这类错误**不报异常**（`alpha.59` 被同样的坑坑过）。

def _run_wiring() -> None:
    print()
    print("D. V2：接进 run()")

    check("W1 ★ video_mode **默认关闭**（打开它是决定，不是顺手）",
          EpisodeRunner.video_mode is False, str(EpisodeRunner.video_mode))

    src = inspect.getsource(EpisodeRunner.run)
    i_render = src.find("self._render(")
    i_clip = src.find("_render_clip_stage(")
    i_assemble = src.find("self.assembler.assemble(")
    check("W2 ★★ 第二段在**静帧渲染之后**（否则拿不到首帧）",
          0 <= i_render < i_clip, f"render@{i_render} clip@{i_clip}")
    check("W3 第二段在**成片清单之后**（清单先定，再谈呈现）",
          0 <= i_assemble < i_clip, f"assemble@{i_assemble} clip@{i_clip}")
    check("W4 由 `video_mode` 闸住（默认不走）",
          "self.video_mode" in src[:i_clip + 200] if i_clip > 0 else False,
          "没看到 video_mode 判断")

    fn = inspect.getsource(EpisodeRunner._render_clip_stage)
    check("W5 ★ 失败不抛异常（片段没出来，静帧成片仍然有效）",
          "except Exception" in fn and "return {" in fn)

    # 结果字段
    import dataclasses
    names = [f.name for f in dataclasses.fields(
        __import__("little_crew.runtime.episode", fromlist=["EpisodeResult"])
        .EpisodeResult)]
    check("W6 ★ 结果里区分得开两种成片（clip_video vs video_path）",
          "clip_video" in names and "video_path" in names, str(names[-6:]))
    d = __import__("little_crew.runtime.episode", fromlist=["EpisodeResult"]) \
        .EpisodeResult(episode_id="E1").as_dict()
    check("W7 as_dict 导出 clipVideo（报告能看出是不是真片段拼的）",
          "clipVideo" in d, str([k for k in d if "ideo" in k]))



# ---------- E. V3：音轨门禁 ----------
#
# M3 的定义是"成片**含音轨**"。所以缺音轨必须**让交付失败**，
# 而不是只留个 warning —— 否则 M3 会以"看起来完成了"的样子通过。
#
# 这里只用**确定性**判据（不依赖仓库里的媒体文件）：
#   · 取不到观测 -> 不通过（铁律①：缺观测 ≠ 通过）
#   · 结构上缺音轨会不会让 `ok` 变 False

def _audio_gate() -> None:
    print()
    print("E. V3：音轨门禁")

    class D:
        _check_clip_audio = EpisodeRunner._check_clip_audio

    # 不存在的文件 -> 必须**记为失败**，不能当通过
    rep = D()._check_clip_audio(["/nonexistent/zzz.mp4"])
    check("X1 ★★ 取不到观测 -> **不通过**（缺观测 ≠ 通过）",
          rep["ok"] is False and rep["failed"] == 1, str(rep)[:120])
    check("X2 失败原因可读（不是空字符串）",
          bool((rep.get("reason") or "").strip()), str(rep.get("reason"))[:80])

    # 空列表 -> 没有可检查的，不算失败
    rep2 = D()._check_clip_audio([])
    check("X3 空列表 -> ok=True 且 passed=0（没有可检查的不等于失败）",
          rep2["ok"] is True and rep2["passed"] == 0, str(rep2)[:100])

    # ---- 结构：缺音轨要让交付失败 ----
    src = inspect.getsource(EpisodeRunner.run)
    i_audio = src.find("_clip_audio")
    i_ok = src.find("ok = False", i_audio) if i_audio > 0 else -1
    check("X4 ★★ 缺音轨时 `run()` 把 `ok` 置 False（M3 要求含音轨）",
          0 <= i_audio < i_ok, f"audio@{i_audio} ok=False@{i_ok}")

    # ---- 结果字段 ----
    from little_crew.runtime.episode import EpisodeResult
    import dataclasses
    names = [f.name for f in dataclasses.fields(EpisodeResult)]
    check("X5 ★ 音轨结果单独一个字段（与「有没有拼出成片」分开看得见）",
          "clip_audio" in names and "clip_video" in names, str(names[-8:]))
    d = EpisodeResult(episode_id="E1").as_dict()
    check("X6 as_dict 导出 clipAudio", "clipAudio" in d,
          str([k for k in d if "lip" in k]))



# ---------- F. `_render_clip_stage` 整体（外层）----------
#
# ## 这一段的来历（2026-10-18，代价 1.75 小时）
#
# 我写了 31 项测试，全都绕着 `render_video_stage`（**内层**）转。
# 而 bug 在 `_render_clip_stage`（**外层**）：
#
#     audio = self._check_clip_audio(paths + [full])   # `full` 还没赋值
#     full = clipdir / "full_clips.mp4"
#     → UnboundLocalError
#
# 整集 20 镜渲染完（6308 秒 = 1.75 小时）之后，才在最后一步炸掉。
#
# > **测了内层不等于测了外层。** 包装函数里的顺序/变量错误，
# > 只有真的调用那个包装函数才会暴露。
#
# 所以这一段**真的调 `_render_clip_stage`**（假渲染器 + 1×1 假 png），
# 断言它至少能走到"返回一个 dict"这一步。

def _outer_wrapper() -> None:
    print()
    print("F. 外层 `_render_clip_stage`")

    import os as _os
    import pathlib

    class _D:
        _render_clip_stage = EpisodeRunner._render_clip_stage
        _still_path = EpisodeRunner._still_path
        _ffmpeg_status = EpisodeRunner._ffmpeg_status
        _ffmpeg = EpisodeRunner._ffmpeg
        _check_clip_audio = EpisodeRunner._check_clip_audio
        render_video_stage = EpisodeRunner.render_video_stage
        video_seconds = 1.0
        _prompts_seen = {"S1": "p1"}
        resolve_artifact = staticmethod(lambda n: None)
        render_video_stage_calls = 0

    # 1) 缺 ffmpeg -> 必须**在渲染前**失败（不浪费）
    _os.environ.pop("FFMPEG", None)
    d1 = _D()
    d1._workdir = pathlib.Path(".")
    got = d1._render_clip_stage({"S1": ["C:/x/a.png"]})
    check("Y1 ★★ 缺 ffmpeg -> **开跑前**就失败（不浪费渲染时间）",
          got["ok"] is False and "ffmpeg" in str(got.get("reason", "")).lower(),
          str(got)[:140])

    # 2) 有 ffmpeg、没有可用静帧 -> 走到"没有可用的静帧路径"，不抛
    _os.environ["FFMPEG"] = sys.executable          # 任何存在的文件都行
    d2 = _D()
    d2._workdir = pathlib.Path(".")
    got2 = d2._render_clip_stage({})
    check("Y2 ★ 空输入 -> 返回 dict（**不抛 UnboundLocalError / NameError**）",
          isinstance(got2, dict) and got2.get("ok") is False, str(got2)[:140])

    # 3) 关键：**函数能被完整执行到返回**，不因变量顺序而崩
    d3 = _D()
    d3._workdir = pathlib.Path(".")
    try:
        got3 = d3._render_clip_stage({"S1": ["C:/definitely/not/here.png"]})
        ok3 = isinstance(got3, dict)
        detail3 = str(got3)[:140]
    except Exception as e:                                  # noqa: BLE001
        ok3 = False
        detail3 = f"{type(e).__name__}: {e}"
    check("Y3 ★★ 走到返回处不抛（**这条就是当初该有的那条**）",
          ok3, detail3)



def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")            # type: ignore[attr-defined]
    except Exception:                                        # noqa: BLE001
        pass
    print("=" * 74)
    print("video_mode 第二段（render_video_stage）—— 自检（假通道）")
    print("=" * 74)

    _behavior()
    _structure()
    _run_wiring()
    _audio_gate()
    _outer_wrapper()

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
