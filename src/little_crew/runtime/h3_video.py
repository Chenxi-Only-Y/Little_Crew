"""**H3 音轨渲染**：把一个镜头渲染成"带音轨的视频片段"。

## 这个模块在 M3 收口里的位置

`EpisodeRunner` 目前只有一条渲染路：**静帧**（Z-Image）→ ffmpeg 补运动 → 拼。
本模块提供第二条路：**静帧当首帧 → H3 i2v → 自带音轨的 mp4 片段**。

```
静帧（已有）  ->  H3 i2v（本模块）  ->  片段 mp4（含 AAC 音轨）  ->  concat
```

## 为什么走 `CachingRenderer` 而不是直连 urllib

`research/audio-track/make_audio_demo.py` 证明了 H3 出音轨，但它**绕过项目的通道层**。
那时可以（只为验证可行性），**进主流程不行** —— 绕过去就等于丢掉了：

| 丢掉的东西 | 后果 |
|---|---|
| 内容缓存（`render_http` 的入参哈希） | 同样的镜头重跑要再等 340 秒 |
| 检查点（提交前记意图） | 中断后不知道服务端还在跑 → **双份 GPU** |
| 失败分类（`RenderOutcome.failure`） | 分不清"服务挂了"与"参数写错了" |
| 异步轮询 | 只能同步干等 |

所以本模块**只做"参数与编排"**，真正的提交/轮询/取字节全部交给
`CachingRenderer.render_http(values, backend)` —— 与静帧那条路**同一套基础设施**。

## 成本（必须先看这个数）

H3 单镜实测 **约 340 秒**纯生成（640×384 / 8 步 / 107 帧）。
**这决定了它只能用于短片或关键镜，不能整集铺开。**
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = [
    "DEFAULT_SECONDS",
    "DEFAULT_STEPS",
    "VideoShot",
    "audio_constraint",
    "concat_clips",
    "h3_backend",
    "render_videos",
    "video_values",
]

#: 请求的秒数。**实测 H3 出的帧数是离散的** —— 请求 2 秒会得到 4.46 秒
#: （107 帧 / 24fps）。所以这个数是"尽量靠近"，不是"精确"。
#: 真正的时长以产物容器为准，别拿它去算账。
DEFAULT_SECONDS: float = 2.0

#: 采样步数。8 步是实测能出可用画面的最低档 —— 往上是线性变慢。
DEFAULT_STEPS: int = 8


class VideoShot:
    """一个要渲染成视频的镜头。**只放必要信息，不塞整个 Shot。**"""

    __slots__ = ("shot_id", "prompt", "image_path", "width", "height")

    def __init__(self, *, shot_id: str, prompt: str, image_path: Any,
                 width: int = 640, height: int = 384) -> None:
        self.shot_id = shot_id
        self.prompt = prompt
        self.image_path = image_path
        self.width = width
        self.height = height

    def __repr__(self) -> str:                                # pragma: no cover
        return f"VideoShot({self.shot_id!r}, {self.width}x{self.height})"


def h3_backend(registry: Any = None) -> Any:
    """取 `minimax-h3` 的**本地** HTTP 后端。取不到返回 ``None``。

    为什么要挑"本地"那个：`minimax-h3` 有三个后端 ——
    ComfyUI（本机没起）、MiniMax 官方 API（**没有 key**）、local-gen（零成本）。
    只有最后一条现在能走通。

    ⚠️ **不静默退回官方 API**：那会产生账单。
    本地后端不可用时宁可返回 ``None`` 让调用方明确失败。
    """
    if registry is None:
        from little_crew.models.registry import build_default_registry
        registry = build_default_registry()
    spec = registry.get("minimax-h3")
    if spec is None:
        return None
    for b in spec.backends:
        if getattr(b, "is_local_by_default", False) and b.async_task:
            return b
    return None


def video_values(shot: VideoShot, *, seconds: float = DEFAULT_SECONDS,
                 steps: int = DEFAULT_STEPS, seed: int = 0) -> dict[str, Any]:
    """构造 `render_http` 的入参。

    **键名必须与 `minimax-h3` 的 `body_template` 占位符一一对应** ——
    少一个就会在渲染模板时抛错，多一个会被忽略（后者更危险，
    表现是"改了参数但不生效"）。
    """
    return {
        "prompt": str(shot.prompt or ""),
        "image_path": str(shot.image_path),
        "width": int(shot.width),
        "height": int(shot.height),
        "seconds": float(seconds),
        "steps": int(steps),
        "seed": int(seed),
    }


def render_videos(renderer: Any, backend: Any, shots: Sequence[VideoShot], *,
                  seconds: float = DEFAULT_SECONDS, steps: int = DEFAULT_STEPS,
                  seed_base: int = 0,
                  on_progress: Any = None) -> tuple[dict[str, bytes], list[dict]]:
    """逐镜渲染，返回 ``({shotId: 片段字节}, [失败记录])``。

    **一镜失败不中断其余** —— 与整集渲染的语义一致（部分成功是常态）。
    但失败**必须被记下来**：静默丢镜头会让成片少一段而没人知道。
    """
    ok: dict[str, bytes] = {}
    failures: list[dict] = []
    for i, shot in enumerate(shots):
        values = video_values(shot, seconds=seconds, steps=steps,
                              seed=seed_base + i)
        if on_progress is not None:
            try:
                on_progress(shot.shot_id, i, len(shots))
            except Exception:                                 # noqa: BLE001
                pass
        try:
            out = renderer.render_http(values, backend, job_id=shot.shot_id)
        except Exception as e:                                # noqa: BLE001
            failures.append({"shotId": shot.shot_id,
                             "failure": type(e).__name__,
                             "reason": str(e)[:200]})
            continue
        if not getattr(out, "ok", False):
            failures.append({"shotId": shot.shot_id,
                             "failure": str(getattr(out, "failure", "unknown")),
                             "reason": str(getattr(out, "reason", ""))[:200]})
            continue
        arts = getattr(out, "artifacts", None) or {}
        # `artifacts` 是 `{文件名: bytes}`；取第一个（一镜一片段）
        blob = next(iter(arts.values()), None)
        if not blob:
            failures.append({"shotId": shot.shot_id, "failure": "empty",
                             "reason": "渲染成功但没有产物字节"})
            continue
        ok[shot.shot_id] = blob
    return ok, failures


def concat_clips(ffmpeg: Any, clips: Sequence[Path], out: Path, *,
                 workdir: Path | None = None) -> Path:
    """ffmpeg concat 拼接片段。**`-c copy` 保留音轨。**

    ⚠️ `-c copy` 要求各段编码参数一致。本次是同一模型、同一分辨率、
    同一采样率，所以安全。**换成混源片段就必须重编码** —— 否则
    ffmpeg 会给出一个只有第一段有声音的文件，而这**不会报错**。
    """
    if not clips:
        raise ValueError("没有可拼接的片段")
    wd = Path(workdir or clips[0].parent)
    lst = wd / "concat.txt"
    lst.write_text("".join(f"file '{Path(c).as_posix()}'\n" for c in clips),
                   encoding="utf-8")
    cmd = [str(ffmpeg), "-y", "-f", "concat", "-safe", "0",
           "-i", str(lst), "-c", "copy", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg concat 失败：{(r.stderr or '')[-600:]}")
    return out


def audio_constraint(shot_id: str, *, enforcement: str = "hard") -> dict[str, Any]:
    """给一镜生成"必须有音轨"的约束（C1：约束边 → 可执行断言）。

    **默认 `hard`**，与 `cast_consistent_with_text` 的 `audit` 相反 ——
    理由不同：

    | | 为什么这档 |
    |---|---|
    | `cast_consistent_with_text` | 真实内容命中 0%，**拦不到东西**，升 `hard` 是空操作 |
    | `audio_track_present` | H3 的音频是**模型固有输出**（不是可选参数）。**缺了就说明请求或落盘环节丢了东西** —— 那是真故障，该拦 |

    换句话说：这条断言失败**不表示"质量不够"，而表示"链路断了"**。
    """
    return {
        "ref": f"cr_audio_{shot_id}",
        "assertion": {"op": "audio_track_present"},
        "enforcement": enforcement,
    }
