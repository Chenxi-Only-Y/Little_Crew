"""白模帧序列 → **mp4 + 首帧**（零第三方依赖，只调 ffmpeg）。

## 为什么单独一个脚本，而不是塞进 Blender 里

Blender 的 `--python` 脚本运行在 Blender 的 Python 环境里，**不是系统 Python**，
调不通项目代码；而编码这件事**只需要 ffmpeg**，与 Blender 无关。

**拆开的好处**：
- 编码可以单独改（改码率、改分辨率、加字幕）不用碰 Blender
- 失败可单独重试 —— 帧已经渲好了，重编码几秒钟，不用重渲 28 秒

## 产物

| 文件 | 用途 |
|---|---|
| `<tag>.mp4` | **白模视频**（当前无人消费，待接入可收控制视频的模型） |
| `<tag>_first.png` | **首帧**（今天就能用：Z-Image 控构图 / H3 当 `image_path`） |

**两者来自同一份帧序列**，所以不可能不一致。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

__all__ = ["encode_frames", "first_frame", "find_ffmpeg"]

#: 帧文件名的匹配前缀（Blender 输出 `f00001.png`）
FRAME_GLOB = "f*.png"


def find_ffmpeg() -> str:
    """找 ffmpeg。**优先环境变量，其次 PATH**，都不行就报错 —— 不猜固定路径。"""
    import os

    cand = (os.environ.get("FFMPEG") or "").strip()
    if cand and (Path(cand).is_file() or shutil.which(cand)):
        return cand
    got = shutil.which("ffmpeg")
    if got:
        return got
    raise RuntimeError(
        "找不到 ffmpeg —— 设 `FFMPEG` 环境变量，或把它加进 PATH。"
        "**这条要在编码前就过，不要等到跑完才发现。**")


def _frames(frames_dir: Path) -> list[Path]:
    return sorted(frames_dir.glob(FRAME_GLOB))


def first_frame(frames_dir: Path, out_png: Path) -> Path:
    """把第一帧复制成 `_first.png`。

    ⚠️ **必须是同一份帧序列里的第一帧**，不能另外"画一张首帧" ——
    否则白模视频与首帧可能对不上，下游两个都拿到就矛盾了。
    """
    fs = _frames(frames_dir)
    if not fs:
        raise RuntimeError(f"{frames_dir} 里没有帧（{FRAME_GLOB}）")
    out_png.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(fs[0], out_png)
    return out_png


def encode_frames(frames_dir: Path, out_mp4: Path, *, fps: int = 24,
                  ffmpeg: str | None = None) -> Path:
    """帧序列 → mp4（h264，`yuv420p` 保证兼容）。

    **不裁分辨率** —— 帧是多大就是多大，避免"白模分辨率与出图不一致"导致
    首帧做 ControlNet 时被缩放、构图偏移。
    """
    fs = _frames(frames_dir)
    if not fs:
        raise RuntimeError(f"{frames_dir} 里没有帧（{FRAME_GLOB}）")
    exe = ffmpeg or find_ffmpeg()
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    # ⚠️ **不能用 `-pattern_type glob`**（2026-10-19 实测）：
    # Windows 版 ffmpeg 对 glob 输入报
    #   `Error opening input file ... \f*.png` / `Function not implemented`
    # 改用 image2 序列模式 `f%05d.png` —— 这是 ffmpeg 的原生序列写法，跨平台可用。
    #
    # `start_number` 必须取**实际首帧编号**（Blender 从 00001 开始），
    # 写死 1 会在帧号不从 1 开始时静默丢帧。
    first = fs[0].stem          # 例如 "f00062"
    digits = "".join(ch for ch in first if ch.isdigit())
    start = int(digits) if digits else 1
    width = len(digits) or 5
    pattern = frames_dir / f"f%0{width}d.png"
    cmd = [
        exe, "-y",
        "-framerate", str(fps),
        "-start_number", str(start), "-i", str(pattern),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
        str(out_mp4),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(
            f"ffmpeg 编码失败（{r.returncode}）：{r.stderr[-400:]}")
    return out_mp4


def main(argv: list[str]) -> int:
    """`python previs_encode.py <frames_dir> <out_tag> [fps]`"""
    if len(argv) < 3:
        print("用法: python previs_encode.py <frames_dir> <out_tag> [fps]")
        return 2
    frames_dir = Path(argv[1])
    tag = Path(argv[2])
    fps = int(argv[3]) if len(argv) > 3 else 24
    try:
        mp4 = encode_frames(frames_dir, tag.with_suffix(".mp4"), fps=fps)
        png = first_frame(frames_dir, tag.parent / f"{tag.name}_first.png")
    except Exception as e:                                    # noqa: BLE001
        print(f"❌ {type(e).__name__}: {e}")
        return 1
    print(f"✅ mp4   {mp4}  {mp4.stat().st_size / 1024:.0f} KB")
    print(f"✅ 首帧  {png}  {png.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
