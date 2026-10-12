r"""视频合成 —— 把成片清单变成真正的 MP4。

它在链路里的位置
----------------
```
每镜一张静图（已跑通，见 §5.7）
      ↓  compile_clip_plan()    ← 运镜语义 -> 实际镜头运动
每镜一小段视频
      ↓  concat
episode.master.mp4
```

**为什么它是"跑通一集"的最后一环**：在此之前，"成片"是一份 JSON 清单。
清单能被审片、能被验收，但**不能被播放**。而"一集"最朴素的含义就是"能连着看的一段"。

⚠️ 与本模块的边界
----------------
本模块**只做合成**（图 → 视频）。它不做：

| 不做 | 理由 |
|---|---|
| 生成视频（图 → 动态视频） | 那是 H3/视频模型的事，受 10 秒硬上限约束 |
| 配音与混音 | 需要 TTS，是另一条链 |
| 转场特效的"创意" | 只实现 IR 已声明的运镜语义，不发明新语义 |

**运镜语义从哪来**：IR 的 `camera_movement` 字段（5 种）。
本模块把它翻译成实际的画面运动 —— 这是"语义 → 执行"的一次真实闭环，
而不是把静图硬拼成幻灯片。

⚠️ FFmpeg 是外部依赖
------------------
按 `STRUCTURE.md` §1.1，FFmpeg 在**项目外**（`D:\AI\FFmpeg`），本模块只调用它。
路径通过 `FFMPEG_BIN` 环境变量或 `config/.env` 的 `FFMPEG_BIN` 提供。
**找不到时给出可执行的安装提示，而不是抛一个裸的 FileNotFoundError。**
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = [
    "VideoFailure",
    "FFmpegTools",
    "find_ffmpeg",
    "ZoomPan",
    "ken_burns_for",
    "ShotClip",
    "compile_clip_plan",
    "AssemblyOutcome",
    "VideoAssembler",
]


# ==========================================================================
# 失败分类 —— 与运行时其它层一致的思路：**分类决定恢复动作**
# ==========================================================================

class VideoFailure(str, Enum):
    """失败分类。**不同的分类对应不同的恢复动作。**"""

    NONE = "none"
    #: 找不到 ffmpeg —— 装它，或设 FFMPEG_BIN
    TOOL_MISSING = "tool_missing"
    #: 输入图不存在 —— 修清单，不是修 ffmpeg
    INPUT_MISSING = "input_missing"
    #: ffmpeg 拒绝执行 —— 看 stderr 改命令
    COMMAND_FAILED = "command_failed"
    #: 命令合法但超时 —— 加超时或降分辨率
    TIMEOUT = "timeout"
    #: 产出为空或损坏 —— 重跑
    BAD_OUTPUT = "bad_output"
    #: 能力不足（缺编码器/滤镜）—— 换编码器或换 ffmpeg 构建
    UNSUPPORTED = "unsupported"

    @property
    def hint(self) -> str:
        return {
            VideoFailure.TOOL_MISSING:
                "设置 FFMPEG_BIN 指向 ffmpeg.exe，或把 ffmpeg 加入 PATH",
            VideoFailure.INPUT_MISSING: "检查成片清单里的产物路径是否仍然有效",
            VideoFailure.COMMAND_FAILED: "看 stderr 的实际报错，通常是滤镜参数问题",
            VideoFailure.TIMEOUT: "提高 timeout_s，或降低分辨率/帧率",
            VideoFailure.BAD_OUTPUT: "重跑；若反复出现，检查磁盘空间",
            VideoFailure.UNSUPPORTED: "换用 libx264；或改用完整版 ffmpeg 构建",
        }.get(self, "")


# ==========================================================================
# FFmpeg 探测
# ==========================================================================

#: 常见安装位置 —— 用于"找不到时去哪儿找"的提示，也用于自动探测
COMMON_PATHS: tuple[str, ...] = (
    r"D:\AI\FFmpeg\bin\ffmpeg.exe",
    r"C:\ffmpeg\bin\ffmpeg.exe",
    "/usr/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/opt/homebrew/bin/ffmpeg",
)


def find_ffmpeg(explicit: str | None = None) -> str | None:
    """定位 ffmpeg 可执行文件。**找不到时返回 None，由调用方给提示。**

    查找顺序：显式参数 → 环境变量 → PATH → 常见安装位置。
    """
    if explicit:
        p = Path(explicit)
        return str(p) if p.is_file() else None

    env = os.environ.get("FFMPEG_BIN")
    if env and Path(env).is_file():
        return env

    which = shutil.which("ffmpeg")
    if which:
        return which

    for c in COMMON_PATHS:
        if Path(c).is_file():
            return c
    return None


@dataclass
class Capabilities:
    """探测到的 ffmpeg 能力。**只探测我们真正会用到的东西。**"""

    version: str = ""
    path: str = ""
    encoders: frozenset[str] = frozenset()
    filters: frozenset[str] = frozenset()

    def has_encoder(self, name: str) -> bool:
        return name in self.encoders

    def has_filter(self, name: str) -> bool:
        return name in self.filters

    def as_dict(self) -> dict[str, Any]:
        return {"version": self.version, "path": self.path,
                "encoders": sorted(self.encoders),
                "filters": sorted(self.filters)}


class FFmpegTools:
    """ffmpeg 的薄封装。**只负责"能不能用"与"怎么调"。**"""

    #: 我们真正会用到的编码器与滤镜。探测范围刻意收窄 ——
    #: 全量探测要解析几千行输出，而没用的能力探测了也不会用。
    NEEDED_ENCODERS = ("libx264", "png")
    NEEDED_FILTERS = ("zoompan", "scale", "fade", "concat")

    def __init__(self, binary: str | None = None, *,
                 timeout_s: float = 30.0) -> None:
        self.binary = find_ffmpeg(binary)
        self.timeout_s = timeout_s
        self._caps: Capabilities | None = None

    # ---- 探测 ----------------------------------------------------------

    @property
    def available(self) -> bool:
        return self.binary is not None

    def capabilities(self) -> Capabilities:
        """探测并缓存。**探测失败不抛异常** —— 返回空能力，由调用方判。"""
        if self._caps is not None:
            return self._caps
        if not self.binary:
            self._caps = Capabilities()
            return self._caps

        version = ""
        encoders: set[str] = set()
        filters: set[str] = set()
        try:
            out = self._run([self.binary, "-version"], timeout=15)
            version = (out.splitlines() or [""])[0].strip()
            eout = self._run([self.binary, "-hide_banner", "-encoders"],
                             timeout=self.timeout_s)
            for line in eout.splitlines():
                parts = line.split()
                # 编码器表每行形如：` V....D libx264  H.264 ...`
                if len(parts) >= 2 and parts[0].startswith("V"):
                    encoders.add(parts[1])
            fout = self._run([self.binary, "-hide_banner", "-filters"],
                             timeout=self.timeout_s)
            for line in fout.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0].startswith(("T", ".")):
                    filters.add(parts[1])
        except Exception:                                     # noqa: BLE001
            pass

        self._caps = Capabilities(version=version, path=self.binary,
                                  encoders=frozenset(encoders),
                                  filters=frozenset(filters))
        return self._caps

    def missing_requirements(self) -> tuple[str, ...]:
        """返回缺失的编码器/滤镜名。空元组表示够用。"""
        if not self.binary:
            return ("ffmpeg",)
        caps = self.capabilities()
        miss = [e for e in self.NEEDED_ENCODERS if not caps.has_encoder(e)]
        miss += [f for f in self.NEEDED_FILTERS if not caps.has_filter(f)]
        return tuple(miss)

    # ---- 执行 ----------------------------------------------------------

    def _run(self, cmd: Sequence[str], *, timeout: float | None = None,
             cwd: str | None = None) -> str:
        """执行 ffmpeg 并返回 stdout。

        ⚠️ **`stdin=subprocess.DEVNULL` 不是可有可无的。**

        ffmpeg 在非交互环境下会去读标准输入（它接受 `-` 作为输入源，
        也用它做交互式控制）。若 stdin 被继承而永不 EOF
        （后台作业、CI、被管道包住的进程都会这样），
        **ffmpeg 会永久阻塞** —— 而它连一行输出都不产生，
        看起来就像"卡住了"，极难定位（本次已踩）。

        加上 DEVNULL 后 ffmpeg 立刻拿到 EOF，行为变为纯批处理。
        """
        proc = subprocess.run(
            list(cmd), capture_output=True, cwd=cwd,
            stdin=subprocess.DEVNULL,
            timeout=timeout or self.timeout_s)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace")[-1200:]
            raise VideoCommandError(proc.returncode, err)
        return proc.stdout.decode("utf-8", errors="replace")

    def run(self, cmd: Sequence[str], *, timeout: float | None = None,
            cwd: str | None = None) -> str:
        return self._run(cmd, timeout=timeout, cwd=cwd)


class VideoCommandError(RuntimeError):
    """ffmpeg 返回非零。**保留 stderr 尾部** —— 那是唯一有用的诊断信息。"""

    def __init__(self, returncode: int, stderr_tail: str) -> None:
        super().__init__(f"ffmpeg 退出码 {returncode}：{stderr_tail[-300:]}")
        self.returncode = returncode
        self.stderr_tail = stderr_tail


# ==========================================================================
# 运镜语义 → 画面运动
# ==========================================================================

@dataclass(frozen=True)
class ZoomPan:
    """Ken Burns 运动参数。

    用 ``zoompan`` 滤镜实现。表达式里的 ``on`` 是**输出帧序号**，
    ``zoom`` 随时间线性变化即得到匀速推拉。
    """

    #: 起始缩放
    z_start: float = 1.0
    #: 结束缩放
    z_end: float = 1.0
    #: 水平平移速度（画面宽度的比例 / 秒），正为右移
    x_speed: float = 0.0
    #: 垂直平移速度，正为下移
    y_speed: float = 0.0

    @property
    def is_static(self) -> bool:
        return (abs(self.z_end - self.z_start) < 1e-6
                and abs(self.x_speed) < 1e-6 and abs(self.y_speed) < 1e-6)

    def zoom_expr(self, frames: int) -> str:
        """给出 zoom 表达式。"""
        if frames <= 1 or abs(self.z_end - self.z_start) < 1e-6:
            return f"{self.z_start:.4f}"
        dz = (self.z_end - self.z_start) / (frames - 1)
        return f"{self.z_start:.4f}+{dz:.6f}*on"

    def x_expr(self, frames: int) -> str:
        """给出 x 表达式（居中基准 + 平移）。

        ``iw``/``ow`` 是输入/输出宽度；``(iw-ow/zoom)/2`` 是让当前缩放窗口居中的偏移。
        平移量按 ``on`` 线性增长。
        """
        base = "(iw-ow/zoom)/2"
        if abs(self.x_speed) < 1e-6:
            return base
        # 用 iw 作尺度基准，使速度与分辨率无关
        return f"{base}+{self.x_speed:.6f}*iw*on/{max(frames - 1, 1)}"

    def y_expr(self, frames: int) -> str:
        base = "(ih-oh/zoom)/2"
        if abs(self.y_speed) < 1e-6:
            return base
        return f"{base}+{self.y_speed:.6f}*ih*on/{max(frames - 1, 1)}"


#: 运镜语义 → 画面运动。**键必须是 IR 里 camera_movement 的合法取值。**
#:
#: 这张表是本模块与 IR 的**唯一耦合点** ——
#: 有了它，"IR 里写了 dolly_in"与"画面上真的在推进"才是同一件事。
#: 写错或漏一个，就会静默地退化成静止画面（所以有自检）。
KEN_BURNS: Mapping[str, ZoomPan] = {
    "static":      ZoomPan(1.0, 1.0),
    "dolly_in":    ZoomPan(1.0, 1.14),          # 推进：画面收紧
    "dolly_out":   ZoomPan(1.14, 1.0),          # 拉远：展现环境
    "truck_left":  ZoomPan(1.06, 1.06, x_speed=+0.05),
    "truck_right": ZoomPan(1.06, 1.06, x_speed=-0.05),
    "pan_left":    ZoomPan(1.06, 1.06, x_speed=+0.05),
    "pan_right":   ZoomPan(1.06, 1.06, x_speed=-0.05),
    "tilt_up":     ZoomPan(1.06, 1.06, y_speed=-0.05),
    "tilt_down":   ZoomPan(1.06, 1.06, y_speed=+0.05),
}


def ken_burns_for(camera_movement: str) -> ZoomPan:
    """按运镜语义取运动参数。**未知运镜显式退回静止，不猜。**"""
    return KEN_BURNS.get(str(camera_movement or "static").strip(),
                         KEN_BURNS["static"])


# ==========================================================================
# 镜头片段
# ==========================================================================

@dataclass
class ShotClip:
    """一镜要合成的一段视频。"""

    shot_id: str
    image: Path
    duration_sec: float
    camera_movement: str = "static"
    width: int = 832
    height: int = 480

    @property
    def frames(self) -> int:
        return max(1, int(round(self.duration_sec * FPS)))

    @property
    def motion(self) -> ZoomPan:
        return ken_burns_for(self.camera_movement)

    def filter_chain(self, *, with_fade: bool = True) -> str:
        """构造 ffmpeg 的 ``-vf`` 链。

        ⚠️ **缩放与补边的顺序有讲究**：
        `zoompan` 的输出尺寸由 ``s=`` 指定，若输入尺寸与目标不符会**拉伸变形**。
        所以先 `scale` 到目标尺寸（保持比例 + 补边），再 `zoompan`。
        """
        f = self.frames
        z = self.motion
        parts = [
            # 先统一到目标画幅，缺的部分补黑边（不拉伸）
            f"scale={self.width}:{self.height}:force_original_aspect_ratio=decrease",
            f"pad={self.width}:{self.height}:(ow-iw)/2:(oh-ih)/2:color=black",
            f"zoompan=z='{z.zoom_expr(f)}':x='{z.x_expr(f)}':y='{z.y_expr(f)}'"
            f":d={f}:s={self.width}x{self.height}:fps={FPS}",
            "setsar=1",
            "format=yuv420p",
        ]
        if with_fade and self.duration_sec > 0.6:
            # 淡入淡出各 0.25s，避免镜头切换时的硬切突兀
            parts.insert(-2, f"fade=t=in:st=0:d=0.25")
            parts.insert(-2, f"fade=t=out:st={max(self.duration_sec - 0.25, 0):.3f}:d=0.25")
        return ",".join(parts)

    def command(self, ffmpeg: str, out: Path) -> list[str]:
        """生成合成这一镜的命令。**纯函数，可用测试断言。**"""
        return [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-loop", "1", "-i", str(self.image),
            "-vf", self.filter_chain(),
            "-t", f"{self.duration_sec:.3f}",
            "-r", str(FPS),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p",
            str(out),
        ]


#: 成片帧率。与 `config/project.toml` 的 `fps` 保持一致。
FPS = 24


def make_test_png(path: str | os.PathLike[str], *, w: int = 64, h: int = 48) -> Path:
    """造一张**真的能被 ffmpeg 解码**的 PNG（零第三方依赖）。

    ⚠️ 这个函数的存在理由值得写下来，因为它对应一次很难定位的故障：

    测试夹具曾用「PNG 魔数 + 一堆零字节」冒充图片。那**不是合法 PNG** ——
    ffmpeg 拿到它之后，`scale` 与 `zoompan` 会在一个退化的输入上
    陷入**病态计算**：进程不报错、不退出，只是**一路烧满 CPU**
    （实测 187 秒墙钟 / 225 秒 CPU，而正常的同样工作只要 3.5 秒）。

    表现是"测试卡住了"，而真相是"夹具不是合法输入"。
    **测试夹具本身必须合法**，否则测的是空气 —— 或者更糟，测的是幻觉。
    """
    import struct
    import zlib

    raw = b""
    for y in range(h):
        raw += b"\x00" + bytes(
            v for x in range(w)
            for v in ((x * 4) % 256, (y * 4) % 256, 128))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    blob = (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(blob)
    return p


@dataclass
class ClipPlan:
    """整集的合成计划。**这是可审查、可测试的中间产物。**"""

    clips: tuple[ShotClip, ...] = ()
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.clips) and not self.problems

    @property
    def total_seconds(self) -> float:
        return sum(c.duration_sec for c in self.clips)

    @property
    def total_frames(self) -> int:
        return sum(c.frames for c in self.clips)

    def as_dict(self) -> dict[str, Any]:
        return {
            "clipCount": len(self.clips),
            "totalSeconds": round(self.total_seconds, 3),
            "totalFrames": self.total_frames,
            "fps": FPS,
            "problems": list(self.problems),
            "clips": [{"shotId": c.shot_id, "image": str(c.image),
                       "durationSec": c.duration_sec,
                       "cameraMovement": c.camera_movement,
                       "frames": c.frames,
                       "motion": {"zStart": c.motion.z_start,
                                  "zEnd": c.motion.z_end,
                                  "xSpeed": c.motion.x_speed,
                                  "ySpeed": c.motion.y_speed}}
                      for c in self.clips],
        }


def compile_clip_plan(
    manifest: Mapping[str, Any],
    *,
    resolve: Any,
    width: int = 832,
    height: int = 480,
    limit: int | None = None,
) -> ClipPlan:
    """把成片清单编译成合成计划。

    ``resolve`` 是 ``name -> Path`` 的解析函数（产物名 → 实际文件）。
    产物散落在缓存的分片目录里，所以**必须由调用方提供解析方式** ——
    本模块不去猜目录结构。
    """
    clips: list[ShotClip] = []
    problems: list[str] = []
    shots = list(manifest.get("shots") or ())
    if limit is not None:
        shots = shots[:limit]

    for s in shots:
        sid = str(s.get("shotId", ""))
        names = list(s.get("artifacts") or ())
        if not names:
            problems.append(f"{sid} 没有产物")
            continue
        p = resolve(names[0])
        if p is None or not Path(p).is_file():
            problems.append(f"{sid} 产物找不到：{names[0]}")
            continue
        dur = float(s.get("durationSec") or 0.0)
        if dur <= 0:
            problems.append(f"{sid} 时长非法：{dur}")
            continue
        clips.append(ShotClip(
            shot_id=sid, image=Path(p), duration_sec=dur,
            camera_movement=str(s.get("cameraMovement") or "static"),
            width=width, height=height))

    return ClipPlan(clips=tuple(clips), problems=tuple(problems))


# ==========================================================================
# 合成器
# ==========================================================================

@dataclass
class AssemblyOutcome:
    """合成结果。**部分成功也要如实报告** —— 缺几镜不该丢掉整集。"""

    ok: bool = False
    failure: VideoFailure = VideoFailure.NONE
    reason: str = ""
    out_path: Path | None = None
    clips_built: int = 0
    clips_total: int = 0
    seconds: float = 0.0
    wall_seconds: float = 0.0
    bytes: int = 0
    stderr_tail: str = ""

    @property
    def coverage(self) -> float:
        return self.clips_built / self.clips_total if self.clips_total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "failure": self.failure.value, "reason": self.reason,
            "out": str(self.out_path) if self.out_path else "",
            "clipsBuilt": self.clips_built, "clipsTotal": self.clips_total,
            "coverage": round(self.coverage, 4),
            "seconds": round(self.seconds, 2),
            "wallSeconds": round(self.wall_seconds, 2),
            "bytes": self.bytes,
        }


class VideoAssembler:
    """把成片清单合成为 MP4。

    分两步：**逐镜编码 → 拼接**。

    为什么不一条命令搞定：一条命令意味着一个巨大的 filtergraph，
    出错时无法定位是哪一镜的问题；而逐镜编码后，
    失败的镜头可以被单独指出并跳过，**其余镜头仍然出片**。
    """

    def __init__(self, tools: FFmpegTools | None = None, *,
                 workdir: str | os.PathLike[str] | None = None,
                 timeout_s: float = 600.0) -> None:
        self.tools = tools or FFmpegTools()
        self.workdir = Path(workdir) if workdir else None
        self.timeout_s = timeout_s

    # ---- 主流程 --------------------------------------------------------

    def assemble(self, plan: ClipPlan, out_path: str | os.PathLike[str],
                 *, cleanup: bool = True) -> AssemblyOutcome:
        t0 = time.perf_counter()
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        # ---- 前置检查：**分类失败，给可执行的恢复动作** ----
        if not self.tools.available:
            return AssemblyOutcome(
                failure=VideoFailure.TOOL_MISSING,
                reason=f"找不到 ffmpeg。{VideoFailure.TOOL_MISSING.hint}",
                clips_total=len(plan.clips),
                wall_seconds=time.perf_counter() - t0)

        miss = self.tools.missing_requirements()
        if miss:
            return AssemblyOutcome(
                failure=VideoFailure.UNSUPPORTED,
                reason=f"ffmpeg 缺少 {', '.join(miss)}。{VideoFailure.UNSUPPORTED.hint}",
                clips_total=len(plan.clips),
                wall_seconds=time.perf_counter() - t0)

        if not plan.clips:
            return AssemblyOutcome(
                failure=VideoFailure.INPUT_MISSING,
                reason=f"合成计划为空。{'; '.join(plan.problems[:3])}",
                clips_total=0, wall_seconds=time.perf_counter() - t0)

        seg_dir = (self.workdir or out.parent) / f"_seg_{out.stem}"
        seg_dir.mkdir(parents=True, exist_ok=True)

        # ---- 第一步：逐镜编码 ----
        built: list[Path] = []
        errors: list[str] = []
        for i, clip in enumerate(plan.clips):
            seg = seg_dir / f"{i:04d}_{clip.shot_id}.mp4"
            try:
                self.tools.run(clip.command(self.tools.binary or "ffmpeg", seg),
                               timeout=self.timeout_s)
                if not seg.is_file() or seg.stat().st_size == 0:
                    errors.append(f"{clip.shot_id}: 产出为空")
                    continue
                built.append(seg)
            except subprocess.TimeoutExpired:
                errors.append(f"{clip.shot_id}: 超时")
            except VideoCommandError as e:
                errors.append(f"{clip.shot_id}: {e.stderr_tail[-160:]}")
            except Exception as e:                            # noqa: BLE001
                errors.append(f"{clip.shot_id}: {type(e).__name__}: {e}")

        if not built:
            return AssemblyOutcome(
                failure=VideoFailure.COMMAND_FAILED,
                reason=f"全部 {len(plan.clips)} 镜编码失败；首条错误："
                       f"{errors[0] if errors else '未知'}",
                clips_total=len(plan.clips),
                stderr_tail=errors[0] if errors else "",
                wall_seconds=time.perf_counter() - t0)

        # ---- 第二步：拼接 ----
        # 用 concat demuxer：所有片段同编码参数，可直接流拷贝，无需重编码。
        # **这是逐镜编码的另一个好处** —— 拼接几乎零成本。
        #
        # ⚠️ 列表里**必须是绝对路径**。写相对路径时，concat demuxer 会把它
        # 相对于**列表文件所在目录**再拼一次，得到
        # `...\_seg_x\...\_seg_x\0000.mp4` 这种重复路径 —— 报错信息里能清楚看到。
        # 而这个报错不会在编码阶段暴露（那 20 段都成功了），只在最后一步炸。
        listfile = seg_dir / "concat.txt"
        listfile.write_text(
            "".join(f"file '{p.resolve().as_posix()}'\n" for p in built),
            encoding="utf-8")

        try:
            self.tools.run([
                self.tools.binary or "ffmpeg", "-y", "-hide_banner",
                "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", str(listfile),
                "-c", "copy", str(out),
            ], timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            return AssemblyOutcome(
                failure=VideoFailure.TIMEOUT,
                reason=f"拼接超时。{VideoFailure.TIMEOUT.hint}",
                clips_built=len(built), clips_total=len(plan.clips),
                wall_seconds=time.perf_counter() - t0)
        except VideoCommandError as e:
            return AssemblyOutcome(
                failure=VideoFailure.COMMAND_FAILED,
                reason="拼接失败",
                clips_built=len(built), clips_total=len(plan.clips),
                stderr_tail=e.stderr_tail, wall_seconds=time.perf_counter() - t0)

        if not out.is_file() or out.stat().st_size == 0:
            return AssemblyOutcome(
                failure=VideoFailure.BAD_OUTPUT,
                reason=f"产出为空或不存在。{VideoFailure.BAD_OUTPUT.hint}",
                clips_built=len(built), clips_total=len(plan.clips),
                wall_seconds=time.perf_counter() - t0)

        if cleanup:
            shutil.rmtree(seg_dir, ignore_errors=True)

        total = sum(c.duration_sec for c in plan.clips[:len(built)])
        return AssemblyOutcome(
            ok=not errors, failure=(VideoFailure.NONE if not errors
                                    else VideoFailure.COMMAND_FAILED),
            reason=("全部镜头合成成功" if not errors
                    else f"{len(errors)} 镜失败，其余 {len(built)} 镜已合成"),
            out_path=out, clips_built=len(built), clips_total=len(plan.clips),
            seconds=total, bytes=out.stat().st_size,
            stderr_tail="; ".join(errors[:3]),
            wall_seconds=time.perf_counter() - t0)

    # ---- 探测 ----------------------------------------------------------

    def probe(self) -> dict[str, Any]:
        """环境自检。**给用户看的**，所以要说清"能不能用、缺什么"。"""
        caps = self.tools.capabilities()
        miss = self.tools.missing_requirements()
        return {
            "available": self.tools.available,
            "binary": self.tools.binary or "",
            "version": caps.version,
            "missing": list(miss),
            "ready": self.tools.available and not miss,
            "hint": ("" if (self.tools.available and not miss)
                     else VideoFailure.TOOL_MISSING.hint if not self.tools.available
                     else VideoFailure.UNSUPPORTED.hint),
        }
