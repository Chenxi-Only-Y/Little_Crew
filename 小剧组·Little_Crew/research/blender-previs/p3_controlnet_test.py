"""P3：白模深度序列 → Z-Image ControlNet → 受控生成。

**这是 P3 的实测脚本**（策划书 §3.8.9）。

它回答的问题
------------
> 白模的 Depth / Gray 通道，能不能真的把**机位与站位**约束到生成图上？

若能，则"空间/机位一致性"这条轴成立；若不能，则白模的意义只剩"给人看的预演"。

用法
----
    python p3_controlnet_test.py --frames 3 --control depth
    python p3_controlnet_test.py --control gray
    python p3_controlnet_test.py --control both

⚠️ 显存
------
8 GB 卡上必须用 `model_inference_low_vram` 的 offload 策略（权重驻 CPU、按需换入），
并先卸载 local-gen 的模型（`POST /v1/models/unload`），否则 OOM。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import torch

# ⚠️ **必须在导入 diffsynth 之前打补丁。**
#
# 本机（Windows + torch 2.9.1 + safetensors）下 `safe_open(...).get_tensor()`
# 对**大于约 4 GB** 的 safetensors 文件必然失败：
#     device="cpu"  -> 进程段错误退出（0xC0000005）
#     device="cuda" -> RuntimeError: invalid python storage
#
# 而本用例的两个文件都超限：ControlNet 6.25 GB、transformer 22.93 GB。
# 不打卡就只会看到一个光秃秃的段错误，极难定位（本次已踩）。
#
# 补丁把 DiskMap 的句柄换成纯 Python 的 seek/read 读取器，完全不用 mmap。
sys.path.insert(0, r"D:\AI\local-gen")
try:
    from app.diffsynth_patch import apply_patch as _apply_patch

    _apply_patch(verbose=True)
except Exception as _exc:                                     # noqa: BLE001
    print(f"  ⚠️ DiffSynth 补丁未打上：{type(_exc).__name__}: {_exc}")

# 与官方 model_inference_low_vram 示例**完全一致的 offload 策略**。
# 这是低显存能跑起来的关键：权重平时在 CPU，只在计算时换入。
VRAM_CONFIG = {
    "offload_dtype": torch.bfloat16,
    "offload_device": "cpu",
    "onload_dtype": torch.bfloat16,
    "onload_device": "cpu",
    "preparing_dtype": torch.bfloat16,
    "preparing_device": "cuda",
    "computation_dtype": torch.bfloat16,
    "computation_device": "cuda",
}

CN_MODEL = "PAI/Z-Image-Turbo-Fun-Controlnet-Union-2.1"
CN_FILE = "Z-Image-Turbo-Fun-Controlnet-Union-2.1.safetensors"
BASE_MODEL = "Tongyi-MAI/Z-Image-Turbo"

#: 默认的白模深度序列目录
DEFAULT_DEPTH = r"D:\AI\_previs_out\depthfix_walk\depth"
DEFAULT_GRAY = r"D:\AI\_previs_out\depthfix_walk\gray"


def build_pipeline(cache_dir: str):
    """装载 Z-Image + ControlNet 管线（低显存策略）。

    ⚠️ **`ModelConfig` 没有 `cache_dir` 参数。** 它按
    ``local_model_path / model_id / origin_file_pattern`` 定位文件
    （见 `ModelConfig.download`）。所以这里把基线路径显式传进去，
    而不是指望它自己猜到缓存位置 —— 猜错会静默重新下载几十 GB。
    """
    from diffsynth.pipelines.z_image import ModelConfig, ZImagePipeline

    kw = {"local_model_path": cache_dir}
    t0 = time.time()
    pipe = ZImagePipeline.from_pretrained(
        torch_dtype=torch.bfloat16,
        device="cuda",
        model_configs=[
            ModelConfig(model_id=CN_MODEL, origin_file_pattern=CN_FILE,
                        **kw, **VRAM_CONFIG),
            ModelConfig(model_id=BASE_MODEL,
                        origin_file_pattern="transformer/*.safetensors",
                        **kw, **VRAM_CONFIG),
            ModelConfig(model_id=BASE_MODEL,
                        origin_file_pattern="text_encoder/*.safetensors",
                        **kw, **VRAM_CONFIG),
            ModelConfig(model_id=BASE_MODEL,
                        origin_file_pattern="vae/diffusion_pytorch_model.safetensors",
                        **kw, **VRAM_CONFIG),
        ],
        tokenizer_config=ModelConfig(model_id=BASE_MODEL,
                                     origin_file_pattern="tokenizer/", **kw),
        vram_limit=torch.cuda.mem_get_info("cuda")[1] / (1024 ** 3) - 0.5,
    )
    print(f"  管线加载耗时 {time.time() - t0:.0f}s", flush=True)
    return pipe


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P3 白模 ControlNet 实测")
    ap.add_argument("--control", choices=("depth", "gray", "both"), default="depth")
    ap.add_argument("--frames", type=int, default=3, help="取几帧测试")
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--scale", type=float, default=0.7)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--depth-dir", default=DEFAULT_DEPTH)
    ap.add_argument("--gray-dir", default=DEFAULT_GRAY)
    ap.add_argument("--out", default=r"D:\AI\_previs_out\p3_controlnet")
    ap.add_argument("--cache-dir", default=r"D:\AI\local-gen\models")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    from PIL import Image

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not torch.cuda.is_available():
        print("  ❌ CUDA 不可用")
        return 1
    free_gb = torch.cuda.mem_get_info("cuda")[0] / (1024 ** 3)
    print("=" * 78)
    print("P3 白模 ControlNet 实测")
    print("=" * 78)
    print(f"  控制通道 {args.control}   帧数 {args.frames}   "
          f"步数 {args.steps}   控制强度 {args.scale}")
    print(f"  可用显存 {free_gb:.2f} GB")
    if free_gb < 3.0:
        print("  ⚠️ 显存偏少，可能 OOM。先执行：")
        print("     curl -X POST http://127.0.0.1:8100/v1/models/unload")
    print()

    from diffsynth.pipelines.z_image import ControlNetInput

    pipe = build_pipeline(args.cache_dir)

    prompt = ("全身镜头，一位穿米色风衣的少女站在山路上，"
              "吉卜力动画风格，赛璐璐上色，电影感构图，细致背景，戏剧性光影")
    negative = "多手,多脚,畸形,模糊,低质量,水印,文字"

    report: dict[str, object] = {"control": args.control, "results": []}

    jobs: list[tuple[str, Path]] = []
    if args.control in ("depth", "both"):
        jobs.append(("depth", Path(args.depth_dir)))
    if args.control in ("gray", "both"):
        jobs.append(("gray", Path(args.gray_dir)))

    for tag, src in jobs:
        if not src.is_dir():
            print(f"  ⚠️ {tag}: 目录不存在 {src}")
            continue
        files = sorted(src.glob("*.png"))
        if not files:
            print(f"  ⚠️ {tag}: 无 PNG")
            continue
        # 均匀取帧（避开首帧的起始站姿）
        picks = [files[int((i + 1) * len(files) / (args.frames + 1))]
                 for i in range(args.frames)]

        for f in picks:
            ctrl = Image.open(f).convert("RGB")
            print(f"  [{tag}] {f.name}  {ctrl.size} 生成中 …", flush=True)
            t0 = time.time()
            try:
                img = pipe(
                    prompt=prompt, negative_prompt=negative,
                    seed=args.seed,
                    height=ctrl.height, width=ctrl.width,
                    controlnet_inputs=[ControlNetInput(image=ctrl,
                                                       scale=args.scale)],
                    num_inference_steps=args.steps,
                )
            except torch.cuda.OutOfMemoryError as e:
                print(f"    ❌ OOM: {str(e)[:100]}")
                report["results"].append({"control": tag, "frame": f.name,
                                          "ok": False, "error": "OOM"})
                torch.cuda.empty_cache()
                continue
            except Exception as e:                            # noqa: BLE001
                print(f"    ❌ {type(e).__name__}: {str(e)[:140]}")
                report["results"].append({"control": tag, "frame": f.name,
                                          "ok": False,
                                          "error": f"{type(e).__name__}: {e}"})
                continue
            dt = time.time() - t0
            dst = out_dir / f"{tag}_{f.stem}.png"
            img.save(dst)
            print(f"    ✅ {dst.name}  {dt:.1f}s")
            report["results"].append({"control": tag, "frame": f.name,
                                      "ok": True, "seconds": round(dt, 2),
                                      "out": str(dst)})

    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = [r for r in report["results"] if r.get("ok")]
    print()
    print("=" * 78)
    print(f"结果：{len(ok)}/{len(report['results'])} 成功")
    for r in report["results"]:
        mark = "✅" if r.get("ok") else "❌"
        detail = (f"{r.get('seconds')}s → {Path(str(r.get('out'))).name}"
                  if r.get("ok") else str(r.get("error"))[:70])
        print(f"  {mark} [{r['control']}] {r['frame']}  {detail}")
    print(f"  产物目录 {out_dir}")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
