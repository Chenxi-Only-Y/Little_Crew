"""IR 出口自检（2026-11-11）：三个出口（图片/音频/视频）× 本地/云端。

不调模型、不联网：只验证"IR -> 请求体"的翻译，以及注册表里
每个模态都**同时有本地与云端两条通道**（架构一致、只换通道）。
"""
from __future__ import annotations

import sys

from little_crew.compiler.exits import compile_ir
from little_crew.models.registry import build_default_registry
from little_crew.models.spec import BackendKind, TaskKind

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def _img_ir(prompt="风从山坡上压下来", neg="低清", w=832, h=480):
    return {"irVersion": "little_crew.ir/v1", "graphId": "G-1",
            "nodes": [
                {"id": "p", "kind": "prompt.text", "params": {"text": prompt}},
                {"id": "n", "kind": "prompt.negative", "params": {"text": neg}},
                {"id": "l", "kind": "latent.empty",
                 "params": {"width": w, "height": h}},
                {"id": "s", "kind": "sample.ksampler",
                 "params": {"steps": 8, "cfg": 4.0, "sampler": "euler"}},
            ],
            "provenance": {"shotId": "SH1"}}


def _tts_ir(text="喂，你醒着吗？", voice="girl"):
    return {"irVersion": "little_crew.ir/v1", "graphId": "G-2",
            "nodes": [{"id": "t", "kind": "audio.tts",
                       "params": {"text": text, "voice": voice}}],
            "provenance": {"shotId": "SH1"}}


def _vid_ir(first="", dur=5.0):
    nodes = [{"id": "p", "kind": "prompt.text", "params": {"text": "风吹过山坡"}}]
    if first:
        nodes.append({"id": "i", "kind": "image.file", "params": {"path": first}})
    return {"irVersion": "little_crew.ir/v1", "graphId": "G-3", "nodes": nodes,
            "provenance": {"shotId": "SH1", "durationSec": dur}}


class _B:
    def __init__(self, tpl=""):
        self.body_template = tpl
        self.endpoint = "x"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("IR 出口（图片 / 音频 / 视频 × 本地 / 云端）—— 自检")
    print("=" * 66)

    print(); print("A. 图片出口")
    local_tpl = ('{"model": "z-image-turbo", "prompt": "{prompt}",'
                 ' "negative_prompt": "{negative_prompt}", "width": {width},'
                 ' "height": {height}, "num_inference_steps": {steps},'
                 ' "seed": {seed}}')
    o = compile_ir(_img_ir(), _B(local_tpl), kind="image")
    check("A1 ★★★ 编译成功且提示词进了请求体",
          o.ok and "风从山坡" in str(o.body.get("prompt")), str(o.error)[:60])
    check("A2 ★★★ 尺寸与步数来自 IR 节点",
          o.body.get("width") == 832 and o.body.get("height") == 480
          and o.body.get("num_inference_steps") == 8, str(o.body)[:80])
    check("A3 ★★★ 没有 seed 时从 IR 派生（可复现）",
          isinstance(o.body.get("seed"), int)
          and o.body.get("seed") == compile_ir(_img_ir(), _B(local_tpl)).body.get("seed"),
          str(o.body.get("seed")))
    check("A4 ★★ 显式 seed 覆盖派生值",
          compile_ir(_img_ir(), _B(local_tpl), kind="image", seed=7).body.get("seed") == 7, "")
    check("A5 ★★ 缺 prompt 节点 -> 明确失败（不静默出空图）",
          not compile_ir({"nodes": []}, _B(local_tpl), kind="image").ok, "")
    check("A6 ★★ 模板要的变量 IR 没给 -> 失败并点名",
          not compile_ir(_img_ir(), _B('{"x": "{not_a_field}"}'), kind="image").ok, "")
    o2 = compile_ir(_img_ir(), _B('{"prompt": "{prompt}"}'), kind="image")
    check("A7 ★★ 模板只取它要的变量（不要求全给）", o2.ok, str(o2.error)[:50])
    # 云端模板：size 是 "WxH" 字符串
    cloud_tpl = ('{"model": "gpt-image", "prompt": "{prompt}",'
                 ' "size": "{width}x{height}", "n": 1}')
    oc = compile_ir(_img_ir(), _B(cloud_tpl), kind="image")
    check("A8 ★★★ 云端模板（size 拼字符串）也能编译",
          oc.ok and oc.body.get("size") == "832x480", str(oc.body)[:70])

    _nb = compile_ir(_img_ir(), None, kind="image")
    check("A9 ★★★ 没有 backend 也能编译（通道自己填模板的路径）",
          _nb.ok and _nb.body.get("prompt"), str(_nb.error)[:60])

    print(); print("B. 音频出口")
    t = compile_ir(_tts_ir(), None, kind="tts")
    check("B1 ★★★ 台词与音色进请求体",
          t.ok and t.body.get("input") == "喂，你醒着吗？" and t.body.get("voice") == "girl",
          str(t.body)[:70])
    check("B2 ★★ 没有音色 -> 编译成功但给 notice（不假装一致）",
          any("音色" in n for n in compile_ir(_tts_ir(voice=""), None, kind="tts").notices), "")
    check("B3 ★★ 没有台词 -> 明确失败", not compile_ir({"nodes": []}, None, kind="tts").ok, "")

    print(); print("C. 视频出口")
    v = compile_ir(_vid_ir(first="outputs/a.png"), None, kind="video")
    check("C1 ★★★ 有首帧 -> i2v 且首帧进请求体",
          v.ok and v.body.get("mode") == "i2v"
          and v.body.get("first_frame") == "outputs/a.png", str(v.body)[:70])
    v2 = compile_ir(_vid_ir(), None, kind="video")
    check("C2 ★★★ 没有首帧 -> 退化 t2v 并明确说明",
          v2.body.get("mode") == "t2v" and any("首帧" in n for n in v2.notices),
          str(v2.notices)[:70])
    check("C3 ★★ 时长来自 provenance",
          abs(float(v.body.get("duration")) - 5.0) < 1e-6, str(v.body.get("duration")))

    print(); print("D. 分派与注册表矩阵")
    check("D1 ★★★ 未知出口 -> 明确失败（不猜）",
          not compile_ir({"nodes": []}, None, kind="audio").ok, "")
    reg = build_default_registry()
    groups = {"图片": [TaskKind.IMAGE_T2I], "音频": [TaskKind.AUDIO_TTS],
              "视频": [TaskKind.VIDEO_I2V, TaskKind.VIDEO_T2V]}
    for label, tasks in groups.items():
        specs = [s for s in reg.all() if s.task in tasks]
        local, cloud = set(), set()
        for s in specs:
            for b in s.backends:
                if b.kind is BackendKind.COMFYUI or getattr(b, "is_local_by_default", False):
                    local.add(s.model_id)
                elif b.kind is BackendKind.HTTP:
                    cloud.add(s.model_id)
        check(f"D·{label} ★★★ 同时有本地与云端两条通道",
              bool(local) and bool(cloud), f"local={sorted(local)} cloud={sorted(cloud)}")

    n = sum(1 for _, ok, _ in _r if ok)
    print()
    for name, ok, detail in _r:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 66)
    print(f"结果：{n}/{len(_r)} 通过")
    return 0 if n == len(_r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
