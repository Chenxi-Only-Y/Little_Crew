"""P0-2 扩镜：**三镜各渲两次**（A 无控制 / B strength=0.5）。

单镜版（`step3_render_ab.py`）只能看方向。这一步把样本从 1 镜扩到 3 镜，
回答"0.5 的优势是普遍规律，还是这一镜的偶然"。

判据：三镜的 B 是否**都**优于 A（盲判分）。数字这里先出像素差与地平线，
**最终结论以盲判为准**（像素差已实测不可用作"构图听话度"）。
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
STRENGTH = 0.5
WD = Path(r"D:\AI\ds.8\Little_Crew\outputs\_E8")
COMFY_IN = Path(r"D:\ComfyUI\ComfyUI\input")
COMFY_OUT = Path(r"D:\ComfyUI\ComfyUI\output")
OUT = Path(r"D:\AI\ds.8\Little_Crew\research\p0-2-control-ab\three")
OUT.mkdir(parents=True, exist_ok=True)

man = json.loads((WD / "delivery" / "episode.master.json").read_text(encoding="utf-8"))
shots = man["shots"]
print(f"  三镜：{len(shots)}")

ch = ComfyChannel(Backend(kind=BackendKind.COMFYUI, endpoint=EP,
                          is_local_by_default=True))
print("  通道:", ch.available())
mat = probe_from_object_info(ch.probe_catalog(), endpoint=EP, comfy_version="0.39.0")
comp = ComfyCompiler(mat, default_checkpoint=UNET, default_model_patch=PATCH,
                     model_source_kind="model.source_zimage")
kind = aux_loader_kind_from(comp)


def build(shot, *, with_control: bool, ctrl_name: str, prefix: str):
    ir = shot_to_ir(shot, episode_id="p02x3", checkpoint=UNET, resolution=(640, 384),
                    steps=8, cfg=1.0, sampler="euler", scheduler="simple",
                    filename_prefix=prefix)
    nodes = [dict(n) for n in ir.get("nodes", ())]
    for n in nodes:
        if isinstance(n.get("inputs"), dict):
            n["inputs"] = dict(n["inputs"])
    src = apply_zimage_source(nodes, unet_name=UNET, clip_name=CLIP, vae_name=VAE,
                              aux_loader_kind=kind, src_node_id="n_ckpt")
    if with_control:
        apply_control(nodes, src=src, staged_image=ctrl_name, blur_radius=0,
                      strength=STRENGTH, channel="gray")
    out = dict(ir)
    out["nodes"] = nodes
    return out


rows = []
for idx, s in enumerate(shots, 1):
    sid = s["shotId"]
    ctrl_src = WD / "previs" / sid / "previs_control.png"
    if not ctrl_src.is_file():
        print(f"  [{idx}] {sid}: 缺控制图，跳过"); continue
    ctrl_name = f"p6_{idx}_ctrl.png"
    shutil.copyfile(ctrl_src, COMFY_IN / ctrl_name)

    shot = Shot(frm="storyboard_artist", to="p02x3", shot_id=sid,
                scene_id=s.get("sceneId") or "S01", shot_size=s["shotSize"],
                camera_movement=s.get("cameraMovement") or "static", action="stand",
                duration_sec=float(s["durationSec"]),
                characters=tuple(s.get("characters") or ()),
                description=s["prompt"])

    got = {}
    for tag, wc in (("A", False), ("B", True)):
        ir = build(shot, with_control=wc, ctrl_name=ctrl_name, prefix=f"p6_{idx}{tag}")
        r = render_ir(ir, ch, comp, expect="image", timeout_s=900)
        if not r.ok:
            print(f"  [{idx}] {tag} 失败: {str(r.reason)[:120]}"); got[tag] = None; continue
        hit = sorted(COMFY_OUT.rglob(f"p6_{idx}{tag}*.png"),
                     key=lambda p: p.stat().st_mtime)[-1]
        dst = OUT / f"{idx}{tag}.png"
        shutil.copyfile(hit, dst)
        got[tag] = dst
        print(f"  [{idx}] {tag} ok ({round(getattr(r,'seconds',0.0),1)}s) -> {dst.name}")

    if got.get("A") and got.get("B"):
        ia, ib = Image.open(got["A"]).convert("RGB"), Image.open(got["B"]).convert("RGB")
        px = list(ImageChops.difference(ia, ib).getdata())
        mean = sum(sum(q) for q in px) / (len(px) * 3)
        rows.append({"shotId": sid, "shotSize": s["shotSize"],
                     "meanDiff255": round(mean, 2), "pct": round(mean / 255 * 100, 2)})
        print(f"      vs A: {mean/255*100:.2f}%")

(OUT / "ab3.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                              encoding="utf-8")
print("\n  写出:", OUT / "ab3.json", f"（{len(rows)} 镜）")
