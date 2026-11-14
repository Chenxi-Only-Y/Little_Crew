"""云端整集渲染的回归检查（2026-11-13，A1 真机跑出来的三个洞）。

## 三个洞（都是"本地能跑、云端就挂"）

| 洞 | 现象 | 修法 |
|---|---|---|
| 云端并行返回 **list** 而非 CachedBatch | 成片阶段 `AttributeError: 'list' object has no attribute 'cache_hits'` | `_render` 云端并发分支包成 `CachedBatch`；`_outcomes_of` 两种形态都认 |
| 云端 b64 产物落在**系统临时目录** | 可数审片 32 镜全 `missing-artifact` → 门禁全要返工 | `_materialize_batch` 把字节按镜头写进 `workdir/assets/shots/<sid>/` |
| 云端输出尺寸由服务商决定 | IR 约束/审片期望 832x480，实际 1024x1536 → `size-mismatch` 满屏 | `_normalize_cloud_image` 落盘时统一到 `engine_resolution`（cover + 裁切） |
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


FF = r"D:\AI\FFmpeg\bin\ffmpeg.exe"


def _mk(path: Path, w: int, h: int) -> bool:
    import subprocess

    try:
        subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:d=1,format=rgb24",
                        "-frames:v", "1", str(path)], check=True,
                       capture_output=True, timeout=60)
        return path.is_file()
    except Exception:                                         # noqa: BLE001
        return False


class _BE:
    def __init__(self, local: bool):
        self.is_local_by_default = local


class _O:
    def __init__(self, gid: str, arts: dict | None = None):
        self.graph_id = gid
        self.artifacts = arts or {}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("云端整集渲染（回归）")
    print("=" * 66)

    from little_crew.runtime.cached_render import CachedBatch
    from little_crew.runtime.episode import EpisodeRunner
    from little_crew.review.image_review import png_size

    print(); print("A. 批结果形态（list 与 CachedBatch 都要认）")
    check("A1 ★★★ _outcomes_of 认 CachedBatch",
          len(EpisodeRunner._outcomes_of(CachedBatch(outcomes=tuple()))) == 0
          and len(EpisodeRunner._outcomes_of(CachedBatch(outcomes=(_O("a"),)))) == 1, "")
    check("A2 ★★★ _outcomes_of 认纯列表（云端并行分支）",
          len(EpisodeRunner._outcomes_of([_O("a"), _O("b")])) == 2, "")
    check("A3 ★★ 空输入不崩", EpisodeRunner._outcomes_of([]) == [], "")

    print(); print("B. 产物落盘（云端 b64 在临时目录，必须进 workdir）")
    runner = EpisodeRunner.__new__(EpisodeRunner)
    runner.engine_resolution = (832, 480)
    runner.backend = None
    runner.episode_id = "E-T"
    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        runner._workdir = str(wd)
        if _mk(wd / "raw.png", 320, 180):
            blob = (wd / "raw.png").read_bytes()
            o = _O("G-E-T-SH-001", {"C:\\Users\\x\\Temp\\abc123.png": blob})
            runner._materialize_batch(CachedBatch(outcomes=(o,)), [])
            names = list(o.artifacts)
            files = list((wd / "assets" / "shots").rglob("*.png"))
            check("B1 ★★★ 产物按镜头落到 workdir/assets/shots/<sid>/",
                  bool(files) and files[0].parent.name == "SH-001", str(files[:1]))
            check("B2 ★★★ 产物名去掉盘符/反斜杠（稳定且唯一）",
                  len(names) == 1 and "/" not in names[0] and "\\" not in names[0]
                  and names[0].endswith(".png"), str(names))
        else:
            check("B1 ★★★ 产物按镜头落到 workdir（造图失败）", False, "")
            check("B2 ★★★ 产物名规范化（造图失败）", False, "")

    print(); print("C. 云端产物归一（输出尺寸由服务商决定）")
    with tempfile.TemporaryDirectory() as td2:
        d2 = Path(td2)
        img = d2 / "cloud.png"
        if _mk(img, 320, 180):
            runner2 = EpisodeRunner.__new__(EpisodeRunner)
            runner2.engine_resolution = (832, 480)
            runner2.backend = _BE(local=False)       # 云端
            runner2.ffmpeg = FF
            out = runner2._normalize_cloud_image(img)
            got = png_size(img)
            check("C1 ★★★ 云端产物被归一为引擎分辨率 832x480",
                  out is not None and got == (832, 480), str(got))
            check("C2 ★★★ 归一返回的是**归一后**字节（证据链用同一份）",
                  isinstance(out, (bytes, bytearray)) and len(out) > 1000, str(len(out or b"")))
            runner2.backend = _BE(local=True)        # 本地不动
            img2 = d2 / "local.png"
            _mk(img2, 832, 480)
            check("C3 ★★★ 本地后端不做归一（避免多一次转码）",
                  runner2._normalize_cloud_image(img2) is None, "")
        else:
            check("C1 ★★★ 云端产物归一（造图失败）", False, "")
            check("C2 ★★★ 归一返回字节（造图失败）", False, "")
            check("C3 ★★★ 本地不归一（造图失败）", False, "")

    print(); print("D. 成片产物名兜底（2026-11-14：11/12 镜）")
    with tempfile.TemporaryDirectory() as td4:
        d4 = Path(td4)
        shot_dir = d4 / "assets" / "shots" / "S1"
        shot_dir.mkdir(parents=True)
        _mk(shot_dir / "a.png", 64, 64)
        (d4 / "delivery").mkdir()
        from little_crew.runtime.episode import EpisodeRunner as _ER2, artifact_resolver as _ar2

        r2 = _ER2.__new__(_ER2)
        r2._workdir = str(d4)
        r2.workdir = str(d4)
        r2.resolve_artifact = _ar2(d4)
        r2.video_width = 64
        r2.video_height = 64
        r2.video_out_dir = lambda: d4 / "delivery"
        r2.video_filename = "out.mp4"

        class _Man:
            def as_dict(self):
                return {"shots": [{"shotId": "S1", "artifacts": ["不存在的名字.png"],
                                   "durationSec": 1.0, "cameraMovement": "static"}]}

        res, path = r2._assemble_video(_Man(), {})
        built = getattr(res, "clips_built", 0)
        check("D1 ★★★ 产物名找不到时按 shotId 兜底（不再静默少一镜）",
              built == 1, f"clips={built}")
        check("D2 ★★ 兜底后成片真的产出", bool(path) and Path(str(path)).is_file(), str(path))

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
