"""白模 A/B —— 先跑通「出一张图」这一步。

判据见 `DESIGN.md`；本脚本只负责**出图**，判定由人按判据做。
"""
import json
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
    apply_zimage_source,
    aux_loader_kind_from,
)
from little_crew.runtime.render import render_ir                      # noqa: E402

EP = "http://127.0.0.1:8188"
UNET = "z_image_turbo_int8_convrot.safetensors"
CLIP = "qwen_3_4b_fp4_mixed.safetensors"
VAE = "ae.safetensors"
OUT = Path(r"D:\AI\_previs_out\ab")
OUT.mkdir(parents=True, exist_ok=True)

PROMPT = ("a young woman in a plain linen dress standing alone in a dim "
          "wooden room, rain-streaked window behind her, soft grey daylight, "
          "cinematic still, muted colors")

ch = ComfyChannel(Backend(kind=BackendKind.COMFYUI, endpoint=EP,
                          is_local_by_default=True))
ok, why = ch.available()
print("channel:", ok, why)
mat = probe_from_object_info(ch.probe_catalog(), endpoint=EP,
                            comfy_version="0.39.0")
comp = ComfyCompiler(mat, default_checkpoint=UNET,
                     model_source_kind="model.source_zimage")
src_kind = aux_loader_kind_from(comp)
print("aux CLIP 加载器:", src_kind("CLIP") or "(无)",
      " VAE:", src_kind("VAE") or "(无)")

shot = Shot(frm="storyboard_artist", to="previs_ab",
            shot_id="SH-001", scene_id="S01", shot_size="full",
            camera_movement="static", action="stand", duration_sec=2.5,
            characters=("少女",), description=PROMPT)
ir = shot_to_ir(shot, episode_id="previs-ab", checkpoint=UNET,
                resolution=(640, 384), steps=8, cfg=1.0,
                sampler="euler", scheduler="simple",
                filename_prefix="previs_ab_a")

# 关键：换成 Z-Image 的分离式模型源
nodes = [dict(n) for n in ir.get("nodes", ())]
for n in nodes:
    if isinstance(n.get("inputs"), dict):
        n["inputs"] = dict(n["inputs"])
apply_zimage_source(nodes, unet_name=UNET, clip_name=CLIP, vae_name=VAE,
                    aux_loader_kind=src_kind, src_node_id="n_ckpt")
ir2 = dict(ir)
ir2["nodes"] = nodes
print("IR 节点数:", len(nodes), " kinds:",
      [n.get("kind") for n in nodes])

out = render_ir(ir2, ch, comp, expect="image", timeout_s=900)
print("ok      :", out.ok)
print("failure :", out.failure)
print("reason  :", str(out.reason)[:300])
print("seconds :", round(getattr(out, "seconds", 0.0), 1))
arts = []
for a in (out.artifacts or ()):
    p = str(getattr(a, "path", "") or "")
    print("  artifact:", p, getattr(a, "kind", ""))
    if p:
        arts.append(p)
(OUT / "step1.json").write_text(
    json.dumps({"ok": out.ok, "reason": str(out.reason)[:400],
                "artifacts": arts}, ensure_ascii=False, indent=2),
    encoding="utf-8")
