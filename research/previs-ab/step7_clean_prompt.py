import json, shutil, sys
from pathlib import Path
sys.path.insert(0, r"D:\AI\ds.8\Little_Crew\src")
sys.stdout.reconfigure(encoding="utf-8")
from PIL import Image
from little_crew.agent.ir_bridge import shot_to_ir
from little_crew.agent.messages import Shot
from little_crew.capability.capability import probe_from_object_info
from little_crew.compiler.compiler import ComfyCompiler
from little_crew.models.registry import Backend, BackendKind
from little_crew.runtime.channels.comfy import ComfyChannel
from little_crew.runtime.model_source import apply_control, apply_zimage_source, aux_loader_kind_from
from little_crew.runtime.render import render_ir

EP="http://127.0.0.1:8188"; UNET="z_image_turbo_int8_convrot.safetensors"
PATCH="Z-Image-Turbo-Fun-Controlnet-Union-2.1-lite-2601-8steps.safetensors"
CLIP="qwen_3_4b_fp4_mixed.safetensors"; VAE="ae.safetensors"
WD=Path(r"D:\AI\ds.8\Little_Crew\outputs\_E8")
CI=Path(r"D:\ComfyUI\ComfyUI\input"); CO=Path(r"D:\ComfyUI\ComfyUI\output")
OUT=Path(r"D:\AI\ds.8\Little_Crew\research\p0-2-control-ab\clean")
OUT.mkdir(parents=True, exist_ok=True)
man=json.loads((WD/"delivery"/"episode.master.json").read_text(encoding="utf-8"))
s=man["shots"][1]
sid=s["shotId"]
# 清洗提示词：把"山坡"及其修饰换成花田，去掉混杂
p=s["prompt"].replace("整片山坡","整片花田").replace("山坡","花田")
print("  原提示词:", s["prompt"][:50])
print("  清洗后  :", p[:50])
print("  仍含山坡:", "山坡" in p)
ctrl=WD/"previs"/sid/"previs_control.png"
shutil.copyfile(ctrl, CI/"clean_ctrl.png")
ch=ComfyChannel(Backend(kind=BackendKind.COMFYUI, endpoint=EP, is_local_by_default=True))
mat=probe_from_object_info(ch.probe_catalog(), endpoint=EP, comfy_version="0.39.0")
comp=ComfyCompiler(mat, default_checkpoint=UNET, default_model_patch=PATCH, model_source_kind="model.source_zimage")
kind=aux_loader_kind_from(comp)
def run(tag, wc):
    sh=Shot(frm="storyboard_artist", to="clean", shot_id=sid, scene_id="S01",
            shot_size=s["shotSize"], camera_movement="static", action="stand",
            duration_sec=float(s["durationSec"]), characters=("少女",), description=p)
    ir=shot_to_ir(sh, episode_id="clean", checkpoint=UNET, resolution=(640,384),
                  steps=8, cfg=1.0, sampler="euler", scheduler="simple",
                  filename_prefix="clean_"+tag)
    nodes=[dict(n) for n in ir.get("nodes",())]
    for n in nodes:
        if isinstance(n.get("inputs"),dict): n["inputs"]=dict(n["inputs"])
    src=apply_zimage_source(nodes, unet_name=UNET, clip_name=CLIP, vae_name=VAE, aux_loader_kind=kind, src_node_id="n_ckpt")
    if wc:
        apply_control(nodes, src=src, staged_image="clean_ctrl.png", blur_radius=0, strength=0.5, channel="gray")
    out=dict(ir); out["nodes"]=nodes
    r=render_ir(out, ch, comp, expect="image", timeout_s=900)
    print(f"   {tag} ok={r.ok}")
    if not r.ok: return None
    hit=sorted(CO.rglob(f"clean_{tag}*.png"), key=lambda x:x.stat().st_mtime)[-1]
    dst=OUT/f"{tag}.png"; shutil.copyfile(hit,dst); return dst
a=run("A", False); b=run("B", True)
print("  产出:", a.name if a else None, b.name if b else None)
