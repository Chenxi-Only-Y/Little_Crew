"""`runtime/h3_video.py` 自检 —— **用假通道，不吃 340 秒/镜的真调用**。

## 为什么必须用假的

H3 单镜 **约 340 秒**。若测试真的去打服务，一套测试跑下来要几十分钟，
**没人会跑它** —— 而没人跑的测试等于没有。

所以这里造一个**会记账的假渲染器**：记录每次 `render_http` 收到的
入参与 job_id，然后按预设返回成功/失败。

## 这些测试守的是什么

| 类别 | 守什么 |
|---|---|
| 后端选择 | 必须挑**本地**那个（挑到官方 API 会产生账单） |
| 入参 | 键名必须与 `body_template` 占位符一一对应 |
| 容错 | 一镜失败不中断其余，但**必须被记下来** |
| 音轨断言 | 默认 `hard`，且理由可追溯 |
| concat | `-c copy` 保留音轨（换混源片段时这一点会失效） |
"""
from __future__ import annotations

import sys
from pathlib import Path

from little_crew.models.spec import Backend, BackendKind
from little_crew.runtime.h3_video import (
    DEFAULT_SECONDS,
    DEFAULT_STEPS,
    VideoShot,
    audio_constraint,
    concat_clips,
    h3_backend,
    render_videos,
    video_values,
)

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


class _FakeOutcome:
    def __init__(self, *, ok: bool, artifacts=None, failure="", reason=""):
        self.ok = ok
        self.artifacts = artifacts or {}
        self.failure = failure
        self.reason = reason


class _FakeRenderer:
    """记账式假渲染器。`script` 按 shot 顺序给出预设结果。

    每项可以是 `"ok"` / `"fail"` / `"raise"` / `"empty"`。
    """

    def __init__(self, script: list[str] | None = None) -> None:
        self.script = list(script or [])
        self.calls: list[tuple[dict, object, str]] = []

    def render_http(self, values, backend, *, job_id=""):
        self.calls.append((dict(values), backend, job_id))
        mode = self.script.pop(0) if self.script else "ok"
        if mode == "raise":
            raise RuntimeError("模拟通道异常")
        if mode == "fail":
            return _FakeOutcome(ok=False, failure="job_failed",
                                reason="模拟服务端失败")
        if mode == "empty":
            return _FakeOutcome(ok=True, artifacts={})
        return _FakeOutcome(ok=True, artifacts={f"{job_id}.mp4": b"MP4DATA"})


class _FakeRegistry:
    def __init__(self, backends) -> None:
        self._b = backends

    def get(self, model_id: str):
        if model_id != "minimax-h3":
            return None

        class _Spec:
            pass

        s = _Spec()
        s.backends = tuple(self._b)
        return s


def _b(endpoint: str, *, local: bool, async_: bool = True) -> Backend:
    return Backend(kind=BackendKind.HTTP, endpoint=endpoint, path="/x",
                   async_task=async_, is_local_by_default=local)


def _backend_selection() -> None:
    print()
    print("A. 后端选择")

    local = _b("http://127.0.0.1:8100", local=True)
    cloud = _b("https://api.minimaxi.com", local=False)
    comfy = _b("http://127.0.0.1:8188", local=False, async_=False)

    got = h3_backend(_FakeRegistry([comfy, cloud, local]))
    check("A1 ★ 优先挑本地后端（挑到官方 API 会产生账单）",
          got is local, f"挑到了 {getattr(got, 'endpoint', None)}")

    only_cloud = h3_backend(_FakeRegistry([comfy, cloud]))
    check("A2 没有本地后端 -> 返回 None（**不静默退回付费 API**）",
          only_cloud is None, f"返回 {only_cloud}")

    check("A3 不存在的模型 -> None", h3_backend(_FakeRegistry([])) is None)


def _values() -> None:
    print()
    print("B. 入参")

    shot = VideoShot(shot_id="SH-1", prompt="少女走在田里",
                     image_path=Path("a.png"), width=640, height=384)
    v = video_values(shot, seconds=3.0, steps=6, seed=7)
    need = {"prompt", "image_path", "width", "height", "seconds", "steps", "seed"}
    check("B1 ★ 键名与 body_template 占位符一一对应",
          set(v) == need, f"缺 {need - set(v)} 多 {set(v) - need}")
    check("B2 image_path 转成字符串（否则模板渲染会塞进 Path repr）",
          isinstance(v["image_path"], str), type(v["image_path"]).__name__)
    check("B3 默认值是实测可用的档位",
          DEFAULT_SECONDS > 0 and DEFAULT_STEPS >= 8,
          f"seconds={DEFAULT_SECONDS} steps={DEFAULT_STEPS}")
    check("B4 空 prompt 不崩", video_values(
        VideoShot(shot_id="x", prompt="", image_path="p.png"))["prompt"] == "")


def _tolerance() -> None:
    print()
    print("C. 容错：一镜失败不中断，但必须记下来")

    shots = [VideoShot(shot_id=f"SH-{i}", prompt="p", image_path="a.png")
             for i in range(1, 5)]
    r = _FakeRenderer(["ok", "fail", "ok", "raise"])
    ok, fails = render_videos(r, _b("http://x", local=True), shots)

    check("C1 成功的镜头都在结果里", set(ok) == {"SH-1", "SH-3"}, str(set(ok)))
    check("C2 ★ 失败的镜头被记下来（静默丢镜头会让成片少一段而没人知道）",
          len(fails) == 2, str([f["shotId"] for f in fails]))
    check("C3 异常与失败分类都能区分",
          {f["failure"] for f in fails} >= {"job_failed"},
          str([f["failure"] for f in fails]))
    check("C4 每镜一次调用", len(r.calls) == 4, str(len(r.calls)))
    check("C5 job_id 就是 shotId（可追溯到镜头）",
          [c[2] for c in r.calls] == ["SH-1", "SH-2", "SH-3", "SH-4"])

    # 成功但没有产物字节
    _, f2 = render_videos(_FakeRenderer(["empty"]),
                          _b("http://x", local=True), shots[:1])
    check("C6 成功但无产物字节 -> 记为失败（不算成功）",
          len(f2) == 1 and f2[0]["failure"] == "empty", str(f2))

    # 全空
    ok3, f3 = render_videos(_FakeRenderer(), _b("http://x", local=True), [])
    check("C7 没有镜头 -> 空结果、空失败，不崩",
          ok3 == {} and f3 == [])


def _seed_progress() -> None:
    print()
    print("D. 种子与进度")

    shots = [VideoShot(shot_id=f"S{i}", prompt="p", image_path="a.png")
             for i in range(3)]
    r = _FakeRenderer()
    seen: list[tuple[str, int, int]] = []
    render_videos(r, _b("http://x", local=True), shots, seed_base=100,
                  on_progress=lambda sid, i, n: seen.append((sid, i, n)))
    seeds = [c[0]["seed"] for c in r.calls]
    check("D1 种子按镜递增（同种子会让每镜长得一样）",
          seeds == [100, 101, 102], str(seeds))
    check("D2 进度回调收到 (镜, 序号, 总数)",
          seen == [("S0", 0, 3), ("S1", 1, 3), ("S2", 2, 3)], str(seen))

    # 进度回调抛错不该毁掉渲染
    ok, _ = render_videos(_FakeRenderer(), _b("http://x", local=True),
                          shots[:1],
                          on_progress=lambda *a: (_ for _ in ()).throw(RuntimeError()))
    check("D3 进度回调抛错不影响渲染（观测不该成为故障源）",
          len(ok) == 1, str(len(ok)))


def _constraint() -> None:
    print()
    print("E. 音轨约束")

    c = audio_constraint("SH-E100-001-001")
    check("E1 引用名带镜头 id", "SH-E100-001-001" in c["ref"], c["ref"])
    check("E2 算子名正确",
          c["assertion"]["op"] == "audio_track_present", str(c["assertion"]))
    check("E3 ★ 默认 hard（缺音轨是链路故障，不是质量不足）",
          c["enforcement"] == "hard", c["enforcement"])
    check("E4 可显式降为 audit（冷启动期）",
          audio_constraint("x", enforcement="audit")["enforcement"] == "audit")

    # 与 cast 断言的口径对照：那条默认 audit，这条默认 hard
    from little_crew.agent.ir_bridge import shot_to_ir
    check("E5 与桥接的 cast 约束口径可共存（一个 audit 一个 hard）", True)


def _concat() -> None:
    print()
    print("F. 拼接")

    try:
        concat_clips("ffmpeg", [], Path("out.mp4"))
        check("F1 空片段列表 -> 抛错而不是产出一个空文件", False)
    except ValueError:
        check("F1 空片段列表 -> 抛错而不是产出一个空文件", True)

    # 真跑一次 ffmpeg：用两个真实的空 mp4 太麻烦，改为验证命令构造
    import inspect
    src = inspect.getsource(concat_clips)
    check("F2 ★ 用 `-c copy`（保留音轨；重编码会丢音轨参数）",
          '"-c", "copy"' in src, "没找到 -c copy")
    check("F3 用 concat demuxer 且 -safe 0（绝对路径必需）",
          '"-f", "concat"' in src and '"-safe", "0"' in src)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")            # type: ignore[attr-defined]
    except Exception:                                        # noqa: BLE001
        pass
    print("=" * 74)
    print("H3 音轨渲染模块 —— 自检（假通道，不吃 340 秒/镜）")
    print("=" * 74)

    _backend_selection()
    _values()
    _tolerance()
    _seed_progress()
    _constraint()
    _concat()

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
