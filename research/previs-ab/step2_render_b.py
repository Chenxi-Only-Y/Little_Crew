"""B 组：带白模控制图。A 组已出（`step1_render.py`）。

B 与 A 的差别**只有控制图这一件事** —— 提示词、种子、步数、分辨率全部相同。
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, r"D:\AI\ds.8\Little_Crew\src")
sys.stdout.reconfigure(encoding="utf-8")

from little_crew.agent.ir_bridge import shot_to_ir                      # noqa: E402
from little_crew.agent.messages import Shot                           # noqa: E402
from little_crew.capability.capability import probe_from_object_info  # noqa: E402
from little_crew.compiler.compiler import ComfyCompiler                # noqa: E402
from little_crew.models.registry import Backend, BackendKind           # noqa: E402
from little_crew.runtime.channels.comfy import ComfyChannel            # noqa: E402
from little_crew.runtime.model_source import (                         # noqa: E402
    apply_control,
    apply_zimage_source,
    aux_loader_kind_from,
)
from little_crew.runtime.render import render_ir                      # noqa: E402

EP = "http://127.0.0.1:8188"
UNET = "z_image_turbo_int8_convrot.safetensors"
CLIP = "qwen_3_4b_fp4_mixed.safetensors"
VAE = "ae.safetensors"
COMFY_INPUT = Path(r"D:\ComfyUI\ComfyUI\input")
PREVIS = Path(r"D:\AI\ds.8\Little_Crew\research\blender-previs"
              r"\shot640-evidence\previs_first.png")
OUT = Path(r"D:\AI\_previs_out\ab")
OUT.mkdir(parents=True, exist_ok=True)

PROMPT = ("a young woman in a plain linen dress standing alone in a dim "
          "wooden room, rain-streaked window behind her, soft grey daylight, "
          "cinematic still, muted colors")

# ---- 投放控制图（LoadImage 只认 input/ 里的裸文件名）----
assert PREVIS.is_file(), f"白模首帧不存在：{PREVIS}"
name = "previs_ab_ctrl.png"
shutil.copyfile(PREVIS, COMFY_INPUT / name)
print("控制图已投放:", COMFY_INPUT / name)

ch = ComfyChannel(Backend(kind=BackendKind.COMFYUI, endpoint=EP,
                          is_local_by_default=True))
print("channel:", ch.available())
mat = probe_from_object_info(ch.probe_catalog(), endpoint=EP,
                            comfy_version="0.39.0")
# ⚠️ **`default_model_patch` 必须给** —— `ZImageFunControlnet` 的 required
# 输入是 `model / model_patch / vae / strength`，缺 `model_patch` 会被
# ComfyUI 以 HTTP 400 拒绝（实测）。
PATCH = "Z-Image-Turbo-Fun-Controlnet-Union-2.1-lite-2601-8steps.safetensors"
comp = ComfyCompiler(mat, default_checkpoint=UNET,
                     default_model_patch=PATCH,
                     model_source_kind="model.source_zimage")
kind = aux_loader_kind_from(comp)

shot = Shot(frm="storyboard_artist", to="previs_ab",
            shot_id="SH-001", scene_id="S01", shot_size="full",
            camera_movement="static", action="stand", duration_sec=2.5,
            characters=("少女",), description=PROMPT)
ir = shot_to_ir(shot, episode_id="previs-ab", checkpoint=UNET,
                resolution=(640, 384), steps=8, cfg=1.0,
                sampler="euler", scheduler="simple",
                filename_prefix="previs_ab_b")

nodes = [dict(n) for n in ir.get("nodes", ())]
for n in nodes:
    if isinstance(n.get("inputs"), dict):
        n["inputs"] = dict(n["inputs"])
src = apply_zimage_source(nodes, unet_name=UNET, clip_name=CLIP, vae_name=VAE,
                          aux_loader_kind=kind, src_node_id="n_ckpt")
apply_control(nodes, src=src, staged_image=name, blur_radius=0,
              strength=1.0, channel="gray")
ir2 = dict(ir)
ir2["nodes"] = nodes
print("IR 节点数:", len(nodes))
print("kinds:", [n.get("kind") for n in nodes])

out = render_ir(ir2, ch, comp, expect="image", timeout_s=900)
print("ok      :", out.ok)
print("failure :", out.failure)
print("reason  :", str(out.reason)[:300])
print("seconds :", round(getattr(out, "seconds", 0.0), 1))
arts = [str(getattr(a, "path", "") or "") for a in (out.artifacts or ())]
(OUT / "step2.json").write_text(
    json.dumps({"ok": out.ok, "reason": str(out.reason)[:400],
                "artifacts": arts}, ensure_ascii=False, indent=2),
    encoding="utf-8")
