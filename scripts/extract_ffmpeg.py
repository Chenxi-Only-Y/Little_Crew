"""把 `imageio-ffmpeg` 里那份静态 ffmpeg **取出来**，放到外部工具目录。

为什么要有这个脚本（而不是 `pip install imageio-ffmpeg` 了事）：

    那 84 MB 里真正有用的只有 `ffmpeg.exe` **一个文件**，其余是 Python
    包装代码。项目里留一个包、只为拿一个可执行文件，既臃肿也没必要。

    而且本项目有一条边界：**项目目录只放代码，不放模型与工具**
    （模型权重动辄几 GB，装进项目会污染仓库、也让项目无法搬迁）。
    所以 FFmpeg 放 `<ToolsDir>\\bin\\`，项目里只留"去哪儿找"的逻辑。

用法（一般由 `scripts\\setup_tools.ps1` 调用，不必手动跑）::

    python scripts/extract_ffmpeg.py --out D:\\AI\\little-crew-tools\\bin

取完会把临时下载目录删掉，不在任何 venv 里留下 imageio-ffmpeg。
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys
import tempfile


def main() -> int:
    ap = argparse.ArgumentParser(description="从 imageio-ffmpeg 取出 ffmpeg.exe")
    ap.add_argument("--out", required=True, help="输出目录（会创建）")
    ap.add_argument("--python", default=sys.executable,
                    help="用哪个 python 去下载（默认当前解释器）")
    args = ap.parse_args()

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / "ffmpeg.exe"
    if dst.is_file():
        print(f"  已存在，跳过：{dst}")
        return 0

    # 装到**临时目录**：它只是下载来源，用完即删。
    # 不装进项目 venv —— 那正是用户不想要的"污染"。
    with tempfile.TemporaryDirectory(prefix="lc-ffmpeg-") as tmp:
        r = subprocess.run(
            [args.python, "-m", "pip", "install", "--quiet", "--target", tmp,
             "imageio-ffmpeg"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            print("  [ERROR] 下载 imageio-ffmpeg 失败：", file=sys.stderr)
            print((r.stderr or r.stdout or "")[-800:], file=sys.stderr)
            return 1

        sys.path.insert(0, tmp)
        try:
            import imageio_ffmpeg  # type: ignore
        except Exception as exc:  # noqa: BLE001
            print(f"  [ERROR] 导入失败：{exc}", file=sys.stderr)
            return 1

        src = pathlib.Path(imageio_ffmpeg.get_ffmpeg_exe())
        if not src.is_file():
            print(f"  [ERROR] 没找到内置 ffmpeg：{src}", file=sys.stderr)
            return 1
        shutil.copy2(src, dst)

    print(f"  取出来了：{src.name} -> {dst}（{dst.stat().st_size // 1024 // 1024} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
