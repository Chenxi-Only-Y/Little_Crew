"""E8 用 v2 判据重做：**三镜在 strength=1.0 下**，与已有 A（无控制）配对盲判。

E8 原问题："白模什么时候坏事？"
早先单镜发现 1.0 会把土坡放大成主体（占 2/3），但只有一镜、且判据不适用。
这里补三镜，并用 v2 判据（画面是否成立 / 空间关系是否合理，与内容无关）。

本脚本：渲 3 张（strength=1.0）+ 生成 blind5（3×A + 3×C 共 6 张，打乱、匿名）。
"""
import json
import random
import shutil
import sys
from pathlib import Path

sys.path.insert(0, r"D:\AI\ds.8\Little_Crew\src")
sys.stdout.reconfigure(encoding="utf-8")

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
WD = Path(r"D:\AI\ds.8\Little_Crew\outputs\_E8")
CI = Path(r"D:\ComfyUI\ComfyUI\input")
CO = Path(r"D:\ComfyUI\ComfyUI\output")
BASE = Path(r"D:\AI\ds.8\Little_Crew\research\p0-2-control-ab")
SRC = BASE / "three"
OUT = BASE / "blind5"

man = json.loads((WD / "delivery" / "episode.master.json").read_text(encoding="utf-8"))
shots = man["shots"]
ch = ComfyChannel(Backend(kind=BackendKind.COMFYUI, endpoint=EP,
                          is_local_by_default=True))
print("  通道:", ch.available())
mat = probe_from_object_info(ch.probe_catalog(), endpoint=EP, comfy_version="0.39.0")
comp = ComfyCompiler(mat, default_checkpoint=UNET, default_model_patch=PATCH,
                     model_source_kind="model.source_zimage")
kind = aux_loader_kind_from(comp)

for idx, s in enumerate(shots, 1):
    sid = s["shotId"]
    ctrl = WD / "previs" / sid / "previs_control.png"
    if not ctrl.is_file():
        print(f"  [{idx}] 缺控制图，跳过"); continue
    name = f"e8_{idx}_ctrl.png"
    shutil.copyfile(ctrl, CI / name)
    shot = Shot(frm="storyboard_artist", to="e8", shot_id=sid, scene_id="S01",
                shot_size=s["shotSize"], camera_movement="static", action="stand",
                duration_sec=float(s["durationSec"]), characters=("少女",),
                description=s["prompt"])
    ir = shot_to_ir(shot, episode_id="e8", checkpoint=UNET, resolution=(640, 384),
                    steps=8, cfg=1.0, sampler="euler", scheduler="simple",
                    filename_prefix=f"e8_{idx}C")
    nodes = [dict(n) for n in ir.get("nodes", ())]
    for n in nodes:
        if isinstance(n.get("inputs"), dict):
            n["inputs"] = dict(n["inputs"])
    src = apply_zimage_source(nodes, unet_name=UNET, clip_name=CLIP, vae_name=VAE,
                              aux_loader_kind=kind, src_node_id="n_ckpt")
    apply_control(nodes, src=src, staged_image=name, blur_radius=0,
                  strength=1.0, channel="gray")
    out = dict(ir)
    out["nodes"] = nodes
    r = render_ir(out, ch, comp, expect="image", timeout_s=900)
    if not r.ok:
        print(f"  [{idx}] 失败 {str(r.reason)[:100]}"); continue
    hit = sorted(CO.rglob(f"e8_{idx}C*.png"), key=lambda p: p.stat().st_mtime)[-1]
    shutil.copyfile(hit, SRC / f"{idx}C.png")
    print(f"  [{idx}] strength=1.0 ok ({round(getattr(r,'seconds',0.0),1)}s)")

# ---- blind5：3×A（无控制）+ 3×C（1.0）----
if OUT.exists():
    shutil.rmtree(OUT)
OUT.mkdir(parents=True)
items = []
for i in (1, 2, 3):
    items.append((f"{i}A.png", f"镜{i} · A 无控制"))
    items.append((f"{i}C.png", f"镜{i} · C strength=1.0"))
random.seed(20261101)
random.shuffle(items)
key, rows = {}, ["文件,画面是否成立(1-5),空间关系是否合理(1-5),备注"]
for i, (fn, label) in enumerate(items, 1):
    shutil.copyfile(SRC / fn, OUT / f"b{i}.png")
    key[f"b{i}.png"] = {"label": label, "origin": fn}
    rows.append(f"b{i}.png,,,")
(OUT / "answer_key.json").write_text(json.dumps(key, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
(OUT / "scores.csv").write_text("\n".join(rows) + "\n", encoding="utf-8-sig")

html = ["<!doctype html><meta charset='utf-8'><title>E8 盲判 v2</title>",
        "<style>body{background:#15171b;color:#e6e8ec;font:14px/1.6 system-ui;padding:24px}",
        "h2{font-size:15px;color:#9aa3b2}.g{display:grid;grid-template-columns:1fr 1fr;gap:16px}",
        "figure{margin:0;background:#1d2026;border:1px solid #2c313a;border-radius:8px;padding:8px}",
        "img{width:100%;border-radius:5px}figcaption{color:#7dd3fc;font-weight:600;margin-bottom:6px}",
        ".q{background:#1a1f27;border-left:3px solid #fbbf24;padding:10px 14px;border-radius:6px;margin:14px 0}",
        "</style><h1>E8 盲判（6 张）</h1>",
        "<div class='q'>问一 画面是否成立？1 严重崩坏 / 3 有瑕疵能看 / 5 完全成立<br>",
        "问二 人物与场景的空间关系是否合理？1 完全不合理 / 3 说不准 / 5 完全合理<br>",
        "<b>不要打开 answer_key.json。</b></div><div class='g'>"]
for i in range(1, len(items) + 1):
    html.append(f"<figure><figcaption>b{i}</figcaption><img src='b{i}.png'></figure>")
html.append("</div>")
(OUT / "index.html").write_text("".join(html), encoding="utf-8")
print("\n  blind5 已生成:", OUT / "index.html")
