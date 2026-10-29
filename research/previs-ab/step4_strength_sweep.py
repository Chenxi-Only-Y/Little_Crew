"""P0-2 第二步：**控制强度扫描**（0.5 / 0.35）。

A 组（无控制）与 strength=1.0 已在 `step3_render_ab.py` 里跑过，
这里只补中间两档，并统一和 A 组比像素差 —— 看"降到多少，花田还能留住"。

**判据（人眼）**：花田是否还占据主体；**数字**：与 A 的差越小，越接近"没控制"。
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, r"D:\AI\ds.8\Little_Crew\src")
sys.stdout.reconfigure(encoding="utf-8")

from PIL import Image, ImageChops                              # noqa: E402

from little_crew.agent.ir_bridge import shot_to_ir              # noqa: E402
from little_crew.agent.messages import Shot                     # noqa: E402
from little_crew.capability.capability import probe_from_object_info  # noqa: E402
from little_crew.compiler.compiler import ComfyCompiler          # noqa: E402
from little_crew.models.registry import Backend, BackendKind    # noqa: E402
from little_crew.runtime.channels.comfy import ComfyChannel      # noqa: E402
from little_crew.runtime.model_source import (                    # noqa: E402
    apply_control, apply_zimage_source, aux_loader_kind_from,
)
from little_crew.runtime.render import render_ir                 # noqa: E402

EP = "http://127.0.0.1:8188"
UNET = "z_image_turbo_int8_convrot.safetensors"
PATCH = "Z-Image-Turbo-Fun-Controlnet-Union-2.1-lite-2601-8steps.safetensors"
CLIP = "qwen_3_4b_fp4_mixed.safetensors"
VAE = "ae.safetensors"
COMFY_INPUT = Path(r"D:\ComfyUI\ComfyUI\input")
COMFY_OUT = Path(r"D:\ComfyUI\ComfyUI\output")
WD = Path(r"D:\AI\ds.8\Little_Crew\outputs\_P02_B")
OUT = Path(r"D:\AI\ds.8\Little_Crew\research\p0-2-control-ab")

man = json.loads((WD / "delivery" / "episode.master.json").read_text(encoding="utf-8"))
s0 = man["shots"][0]
PROMPT = s0["prompt"]
ctrl = WD / "previs" / s0["shotId"] / "previs_control.png"
shutil.copyfile(ctrl, COMFY_INPUT / "p02_ctrl.png")

ch = ComfyChannel(Backend(kind=BackendKind.COMFYUI, endpoint=EP,
                          is_local_by_default=True))
mat = probe_from_object_info(ch.probe_catalog(), endpoint=EP, comfy_version="0.39.0")
comp = ComfyCompiler(mat, default_checkpoint=UNET, default_model_patch=PATCH,
                     model_source_kind="model.source_zimage")
kind = aux_loader_kind_from(comp)


def render(strength: float, prefix: str) -> Path | None:
    shot = Shot(frm="storyboard_artist", to="p02", shot_id=s0["shotId"],
                scene_id=s0.get("sceneId") or "S01", shot_size=s0["shotSize"],
                camera_movement=s0.get("cameraMovement") or "static",
                action="stand", duration_sec=float(s0["durationSec"]),
                characters=tuple(s0.get("characters") or ()), description=PROMPT)
    ir = shot_to_ir(shot, episode_id="p02", checkpoint=UNET, resolution=(640, 384),
                    steps=8, cfg=1.0, sampler="euler", scheduler="simple",
                    filename_prefix=prefix)
    nodes = [dict(n) for n in ir.get("nodes", ())]
    for n in nodes:
        if isinstance(n.get("inputs"), dict):
            n["inputs"] = dict(n["inputs"])
    src = apply_zimage_source(nodes, unet_name=UNET, clip_name=CLIP, vae_name=VAE,
                              aux_loader_kind=kind, src_node_id="n_ckpt")
    apply_control(nodes, src=src, staged_image="p02_ctrl.png", blur_radius=0,
                  strength=strength, channel="gray")
    ir2 = dict(ir)
    ir2["nodes"] = nodes
    out = render_ir(ir2, ch, comp, expect="image", timeout_s=900)
    print(f"  strength={strength}: ok={out.ok} 秒={round(getattr(out,'seconds',0.0),1)}")
    if not out.ok:
        print("   ", str(out.reason)[:200])
        return None
    hits = sorted(COMFY_OUT.rglob(f"{prefix}*.png"), key=lambda p: p.stat().st_mtime)
    return hits[-1] if hits else None


A = OUT / "A_no_control.png"
ref = Image.open(A).convert("RGB")
rows = []
for strength, prefix in ((0.5, "p02_s05"), (0.35, "p02_s035")):
    p = render(strength, prefix)
    if p is None:
        continue
    img = Image.open(p).convert("RGB")
    img.save(OUT / f"B_strength_{strength}.png")
    px = list(ImageChops.difference(ref, img).getdata())
    mean = sum(sum(q) for q in px) / (len(px) * 3)
    changed = sum(1 for q in px if max(q) > 12) / len(px) * 100
    rows.append({"strength": strength, "meanDiff255": round(mean, 2),
                 "pct255": round(mean / 255 * 100, 2),
                 "changedPct": round(changed, 2)})
    print(f"    vs A: 平均差 {mean:.2f}/255 ({mean/255*100:.2f}%)  变化像素 {changed:.2f}%")

rows.insert(0, {"strength": 1.0, "note": "已在 step3 测过：18.74% / 98.05%"})
(OUT / "strength_sweep.json").write_text(
    json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
print("\n  写出:", OUT / "strength_sweep.json")
