"""B：**给这一镜加两个场景道具，重渲白模 + 重渲 B 组**，看土坡是否消失。

假设：土坡来自"空平地被模型自由发挥"。
若加了道具白模出现可辨识物件，B 组就该画出"花垄 + 岩石"，而不是大片土坡。

`set` 每项的键（已从渲染器调用点确认）：`label / x / z / w / d / h`（米）。
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
from little_crew.runtime.previs import render_layout             # noqa: E402
from little_crew.runtime.render import render_ir                 # noqa: E402

EP = "http://127.0.0.1:8188"
UNET = "z_image_turbo_int8_convrot.safetensors"
PATCH = "Z-Image-Turbo-Fun-Controlnet-Union-2.1-lite-2601-8steps.safetensors"
CLIP = "qwen_3_4b_fp4_mixed.safetensors"
VAE = "ae.safetensors"
BLENDER = r"D:\Blender\blender.exe"
COMFY_INPUT = Path(r"D:\ComfyUI\ComfyUI\input")
COMFY_OUT = Path(r"D:\ComfyUI\ComfyUI\output")
WD = Path(r"D:\AI\ds.8\Little_Crew\outputs\_P02_B")
OUT = Path(r"D:\AI\ds.8\Little_Crew\research\p0-2-control-ab")

sid = "SH-E601-S01-001"
spec = json.loads((WD / "previs" / sid / "shot.json").read_text(encoding="utf-8"))
man = json.loads((WD / "delivery" / "episode.master.json").read_text(encoding="utf-8"))
PROMPT = man["shots"][0]["prompt"]

# ---- 1) 造布局：原布局 + 两个道具 ----
lay = {
    "shotId": sid,
    "shot": dict(spec["shot"]),
    "subjects": list(spec.get("subjects") or ()),
    "set": [
        # 花垄：人物脚前一条长条台
        {"label": "花垄", "x": 0.0, "z": 2.4, "w": 3.2, "d": 0.9, "h": 0.35},
        # 岩石：右后侧一个盒体
        {"label": "岩石", "x": 2.2, "z": 6.5, "w": 1.2, "d": 1.0, "h": 0.9},
    ],
    "reason": "室外空地，加花垄与岩石提供场景尺度参照（B 实验用）",
}
print("  布局道具:", [s["label"] for s in lay["set"]])

r = render_layout(lay, blender=BLENDER, out_root=str(WD / "previs"),
                  resolution=(640, 384), fps=24)
print(f"  白模重渲: ok={r.get('ok')} frames={r.get('frames')} control={bool(r.get('control'))}")
ctrl_new = Path(str(r.get("control") or ""))
assert ctrl_new.is_file(), f"控制图未生成：{ctrl_new}"
shutil.copyfile(ctrl_new, COMFY_INPUT / "p02_ctrl_props.png")
print("  新控制图:", ctrl_new.name)

# ---- 2) 用新控制图渲 B（strength 0.35）----
ch = ComfyChannel(Backend(kind=BackendKind.COMFYUI, endpoint=EP,
                          is_local_by_default=True))
mat = probe_from_object_info(ch.probe_catalog(), endpoint=EP, comfy_version="0.39.0")
comp = ComfyCompiler(mat, default_checkpoint=UNET, default_model_patch=PATCH,
                     model_source_kind="model.source_zimage")
kind = aux_loader_kind_from(comp)
s0 = man["shots"][0]
shot = Shot(frm="storyboard_artist", to="p02", shot_id=sid,
            scene_id=s0.get("sceneId") or "S01", shot_size=s0["shotSize"],
            camera_movement=s0.get("cameraMovement") or "static", action="stand",
            duration_sec=float(s0["durationSec"]),
            characters=tuple(s0.get("characters") or ()), description=PROMPT)
ir = shot_to_ir(shot, episode_id="p02", checkpoint=UNET, resolution=(640, 384),
                steps=8, cfg=1.0, sampler="euler", scheduler="simple",
                filename_prefix="p02_props")
nodes = [dict(n) for n in ir.get("nodes", ())]
for n in nodes:
    if isinstance(n.get("inputs"), dict):
        n["inputs"] = dict(n["inputs"])
src = apply_zimage_source(nodes, unet_name=UNET, clip_name=CLIP, vae_name=VAE,
                          aux_loader_kind=kind, src_node_id="n_ckpt")
apply_control(nodes, src=src, staged_image="p02_ctrl_props.png", blur_radius=0,
              strength=0.35, channel="gray")
ir2 = dict(ir)
ir2["nodes"] = nodes
out = render_ir(ir2, ch, comp, expect="image", timeout_s=900)
print(f"  B(有道具) ok={out.ok} 秒={round(getattr(out,'seconds',0.0),1)}")
if not out.ok:
    print("   reason:", str(out.reason)[:300])
    raise SystemExit(1)

hit = sorted(COMFY_OUT.rglob("p02_props*.png"), key=lambda p: p.stat().st_mtime)[-1]
img = Image.open(hit).convert("RGB")
img.save(OUT / "B_with_props_0.35.png")
img.save(OUT / "B_props_ctrl.png")
shutil.copyfile(ctrl_new, OUT / "ctrl_with_props.png")

ref = Image.open(OUT / "A_no_control.png").convert("RGB")
px = list(ImageChops.difference(ref, img).getdata())
mean = sum(sum(q) for q in px) / (len(px) * 3)
print(f"  vs A: 平均差 {mean:.2f}/255 ({mean/255*100:.2f}%)")
print("  写出:", OUT / "B_with_props_0.35.png")
