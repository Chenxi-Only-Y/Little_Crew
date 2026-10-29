"""P0-2：**同一份 IR 渲两次**，唯一差别是有没有控制图。

不复用"跑两集"的做法 —— 两集之间提示词/分镜都会变，差异无法归因。
这里提示词逐字取自 `outputs/_P02_B`（那一镜是整集链路真跑出来的），
然后：

    IR（固定） ─┬─ 不给 apply_control  → A.png
                └─ 给   apply_control  → B.png

API 全部照抄 `step2_render_b.py`（跑通过的那份），只把渲染段抽成一次函数。
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, r"D:\AI\ds.8\Little_Crew\src")
sys.stdout.reconfigure(encoding="utf-8")

from little_crew.agent.ir_bridge import shot_to_ir                      # noqa: E402
from little_crew.capability.capability import probe_from_object_info    # noqa: E402
from little_crew.compiler.compiler import ComfyCompiler                 # noqa: E402
from little_crew.agent.messages import Shot                             # noqa: E402
from little_crew.models.registry import Backend, BackendKind               # noqa: E402
from little_crew.runtime.channels.comfy import ComfyChannel             # noqa: E402
from little_crew.runtime.model_source import (                          # noqa: E402
    apply_control, apply_zimage_source, aux_loader_kind_from,
)
from little_crew.runtime.render import render_ir                       # noqa: E402

EP = "http://127.0.0.1:8188"
UNET = "z_image_turbo_int8_convrot.safetensors"
PATCH = "Z-Image-Turbo-Fun-Controlnet-Union-2.1-lite-2601-8steps.safetensors"
CLIP = "qwen_3_4b_fp4_mixed.safetensors"
VAE = "ae.safetensors"
COMFY_INPUT = Path(r"D:\ComfyUI\ComfyUI\input")
WD = Path(r"D:\AI\ds.8\Little_Crew\outputs\_P02_B")
OUT = Path(r"D:\AI\ds.8\Little_Crew\research\p0-2-control-ab")
OUT.mkdir(parents=True, exist_ok=True)

man = json.loads((WD / "delivery" / "episode.master.json").read_text(encoding="utf-8"))
s0 = man["shots"][0]
PROMPT = s0["prompt"]
print(f"  提示词（{len(PROMPT)} 字）：{PROMPT[:70]}…")
print(f"  镜号 {s0['shotId']}  {s0['shotSize']}  {s0['durationSec']}s")

ctrl_src = WD / "previs" / s0["shotId"] / "previs_control.png"
assert ctrl_src.is_file(), f"控制图不存在：{ctrl_src}"
name = "p02_ctrl.png"
shutil.copyfile(ctrl_src, COMFY_INPUT / name)
print("  控制图已投放:", COMFY_INPUT / name)

ch = ComfyChannel(Backend(kind=BackendKind.COMFYUI, endpoint=EP,
                          is_local_by_default=True))
print("  通道:", ch.available())
mat = probe_from_object_info(ch.probe_catalog(), endpoint=EP, comfy_version="0.39.0")
comp = ComfyCompiler(mat, default_checkpoint=UNET, default_model_patch=PATCH,
                     model_source_kind="model.source_zimage")
kind = aux_loader_kind_from(comp)


def build_ir(*, with_control: bool, prefix: str) -> dict:
    shot = Shot(frm="storyboard_artist", to="p02", shot_id=s0["shotId"],
                scene_id=s0.get("sceneId") or "S01", shot_size=s0["shotSize"],
                camera_movement=s0.get("cameraMovement") or "static",
                action="stand", duration_sec=float(s0["durationSec"]),
                characters=tuple(s0.get("characters") or ()),
                description=PROMPT)
    ir = shot_to_ir(shot, episode_id="p02", checkpoint=UNET,
                    resolution=(640, 384), steps=8, cfg=1.0,
                    sampler="euler", scheduler="simple", filename_prefix=prefix)
    nodes = [dict(n) for n in ir.get("nodes", ())]
    for n in nodes:
        if isinstance(n.get("inputs"), dict):
            n["inputs"] = dict(n["inputs"])
    src = apply_zimage_source(nodes, unet_name=UNET, clip_name=CLIP, vae_name=VAE,
                              aux_loader_kind=kind, src_node_id="n_ckpt")
    if with_control:
        apply_control(nodes, src=src, staged_image=name, blur_radius=0,
                      strength=1.0, channel="gray")
    out = dict(ir)
    out["nodes"] = nodes
    print(f"  {prefix}: 节点 {len(nodes)} 个  控制图={'有' if with_control else '无'}")
    return out


res: dict[str, dict] = {}
for tag, with_ctrl, prefix in (("A", False, "p02_a"), ("B", True, "p02_b")):
    print(f"\n  ---- {tag} 组（控制图{'有' if with_ctrl else '无'}）----")
    ir = build_ir(with_control=with_ctrl, prefix=prefix)
    out = render_ir(ir, ch, comp, expect="image", timeout_s=900)
    arts = [str(getattr(a, "path", "") or "") for a in (out.artifacts or ())]
    print(f"   ok={out.ok} failure={out.failure} 秒={round(getattr(out,'seconds',0.0),1)}")
    print(f"   产物 {arts}")
    if not out.ok:
        print("   reason:", str(out.reason)[:300])
    res[tag] = {"ok": out.ok, "reason": str(out.reason)[:400], "artifacts": arts}

(OUT / "ab.json").write_text(json.dumps(res, ensure_ascii=False, indent=2),
                             encoding="utf-8")
print("\n  写出:", OUT / "ab.json")
