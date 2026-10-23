"""编译器与能力矩阵测试。

重点验证「环境能力不足时的行为」——这是本平台与模板填充范式的分水岭。
零第三方依赖： python -m little_crew.compiler.test_compiler
"""

from __future__ import annotations

import copy
import json
import sys

from little_crew.capability.capability import (
    CapabilityMatrix,
    DriftKind,
    Severity,
    probe_from_object_info,
)
from little_crew.capability.comfy_interfaces import DeterminismClass
from little_crew.compiler.compiler import (
    DEFAULT_RULES,
    CompileError,
    CompileErrorCode,
    CompileRule,
    CompileRuleTable,
    ComfyCompiler,
    NoticeKind,
)
from little_crew.ir.canonical import cache_key, condition_fingerprint

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, cond, detail))


def expect_compile_error(name: str, code: str, fn, *a, **kw) -> None:
    try:
        fn(*a, **kw)
    except CompileError as e:
        check(name, e.code == code, f"期望 {code}，实得 {e.code}: {e.message[:70]}")
    except BaseException as e:  # noqa: BLE001
        check(name, False, f"期望 CompileError({code})，实得 {type(e).__name__}: {e}")
    else:
        check(name, False, f"期望 CompileError({code})，但未抛错")


# ==========================================================================
# 环境夹具：三种 ComfyUI 环境
# ==========================================================================

def _port(dtype_or_enum, *, default=None):
    """构造 object_info 的输入声明。"""
    decl = [dtype_or_enum, {}]
    if default is not None:
        decl[1] = {"default": default}
    return decl


def env_full() -> dict:
    """环境 A：功能完整（含 LoadImage、IPAdapter、SageAttention、视频、TTS）。"""
    return {
        "LoadImage": {
            "input": {"required": {
                "image": _port(["anchor_front_v3.png", "anchor_side_v3.png"],
                               default="anchor_front_v3.png")}},
            "output": ["IMAGE", "MASK"], "category": "image", "output_node": False,
        },
        "CheckpointLoaderSimple": {
            "input": {"required": {"ckpt_name": _port(
                ["sd_xl_base_1.0.safetensors", "animagine_xl.safetensors"],
                default="sd_xl_base_1.0.safetensors")}},
            "output": ["MODEL", "CLIP", "VAE"], "category": "loaders",
            "output_node": False,
        },
        "CLIPTextEncode": {
            "input": {"required": {"text": _port("STRING", default=""),
                                   "clip": _port("CLIP")}},
            "output": ["CONDITIONING"], "category": "conditioning", "output_node": False,
        },
        "LoraLoader": {
            "input": {"required": {
                "model": _port("MODEL"), "clip": _port("CLIP"),
                "lora_name": _port(["linwan_v3.safetensors", "linwan_v2.safetensors"],
                                   default="linwan_v3.safetensors")},
                "optional": {"strength_model": _port("FLOAT", default=1.0),
                             "strength_clip": _port("FLOAT", default=1.0)}},
            "output": ["MODEL", "CLIP"], "category": "loaders", "output_node": False,
        },
        "IPAdapterAdvanced": {
            "input": {"required": {"model": _port("MODEL"), "image": _port("IMAGE"),
                                   "ipadapter": _port("IPADAPTER")},
                      "optional": {"weight": _port("FLOAT", default=1.0),
                                   "weight_type": _port(["linear", "ease in-out"],
                                                        default="linear")}},
            "output": ["MODEL"], "category": "ipadapter", "output_node": False,
        },
        "IPAdapterUnifiedLoader": {
            "input": {"required": {"model": _port("MODEL")}},
            "output": ["MODEL", "IPADAPTER"], "category": "ipadapter", "output_node": False,
        },
        "EmptyLatentImage": {
            "input": {"required": {"width": _port("INT", default=512),
                                   "height": _port("INT", default=512),
                                   "batch_size": _port("INT", default=1)}},
            "output": ["LATENT"], "category": "latent", "output_node": False,
        },
        "KSampler": {
            "input": {"required": {
                "model": _port("MODEL"), "positive": _port("CONDITIONING"),
                "negative": _port("CONDITIONING"), "latent_image": _port("LATENT"),
                "seed": _port("INT", default=0),
                "steps": _port("INT", default=20),
                "cfg": _port("FLOAT", default=8.0),
                "sampler_name": _port(["euler", "dpmpp_2m", "dpmpp_2m_sde"],
                                      default="euler"),
                "scheduler": _port(["normal", "karras", "exponential"], default="normal"),
                "denoise": _port("FLOAT", default=1.0)}},
            "output": ["LATENT"], "category": "sampling", "output_node": False,
        },
        "VAEDecode": {
            "input": {"required": {"samples": _port("LATENT"), "vae": _port("VAE")}},
            "output": ["IMAGE"], "category": "latent", "output_node": False,
        },
        "SaveImage": {
            "input": {"required": {"images": _port("IMAGE"),
                                   "filename_prefix": _port("STRING", default="ComfyUI")}},
            "output": [], "category": "image", "output_node": True,
        },
        "ConditioningConcat": {
            "input": {"required": {"conditioning_to": _port("CONDITIONING"),
                                   "conditioning_from": _port("CONDITIONING")}},
            "output": ["CONDITIONING"], "category": "conditioning", "output_node": False,
        },
        "MiniMaxH3MemoryEfficientSageAttentionPatch": {
            "input": {"required": {"model": _port("MODEL")}},
            "output": ["MODEL"], "category": "model_patches", "output_node": False,
        },
        "MiniMaxH3Ref2VA": {
            "input": {"required": {"reference_image": _port("IMAGE"),
                                   "model": _port("MODEL")},
                      "optional": {"length": _port("INT", default=81)}},
            "output": ["LATENT"], "category": "video", "output_node": False,
        },
        "QwenTTSNode": {
            "input": {"required": {"text": _port("STRING"),
                                   "voice": _port(["linwan_01", "narrator"],
                                                  default="linwan_01")},
                      "optional": {"speed": _port("FLOAT", default=1.0)}},
            "output": ["AUDIO"], "category": "audio", "output_node": False,
        },
    }



def env_zimage() -> dict:
    """Z-Image 在 ComfyUI 里的节点子集。

    ⚠️ **结构抓自真机**（ComfyUI 0.39.0 的 ``/object_info``），不是手写。
    手写容易把端口名或"必需/可选"记错，而那种错误的表现是
    **编译产出的图缺一个输入** —— 本地校验看不出，提交时才被拒绝。

    为什么需要它：`composition.zimage_controlnet` 与既有的
    `composition.controlnet` **契约完全不同**（前者补 MODEL，后者补 CONDITIONING），
    而 `env_full()` 里没有 Z-Image 那一套节点。
    """
    return {
        # ---- 模型三件套（Z-Image 走"分离加载"而非单一 checkpoint）----
        "UNETLoader": {
            "input": {"required": {
                "unet_name": [["z_image_turbo_int8_convrot.safetensors"],
                              {"default": "z_image_turbo_int8_convrot.safetensors"}],
                "weight_dtype": [["default", "fp8_e4m3fn", "fp8_e4m3fn_fast",
                                  "fp8_e5m2"], {}]}},
            "output": ["MODEL"], "category": "model/loaders",
            "output_node": False},
        "CLIPLoader": {
            "input": {"required": {
                "clip_name": [["qwen_3_4b_fp4_mixed.safetensors"], {}],
                "type": [["stable_diffusion", "qwen_image", "minimax"], {}]},
                "optional": {"device": [["default", "cpu"], {}]}},
            "output": ["CLIP"], "category": "model/loaders",
            "output_node": False},
        "VAELoader": {
            "input": {"required": {
                "vae_name": [["ae.safetensors", "taesd", "taesdxl"], {}]}},
            "output": ["VAE"], "category": "model/loaders",
            "output_node": False},
        "ModelSamplingAuraFlow": {
            "input": {"required": {"model": ["MODEL", {}],
                                   "shift": ["FLOAT", {"default": 3.0}]},
                      "optional": {"sampling": [["flow", "img_to_img_velocity"],
                                                {}]}},
            "output": ["MODEL"], "category": "model/patch",
            "output_node": False},
        "EmptySD3LatentImage": {
            "input": {"required": {
                "width": ["INT", {"default": 1024}],
                "height": ["INT", {"default": 1024}],
                "batch_size": ["INT", {"default": 1}]}},
            "output": ["LATENT"], "category": "model/latent/stable diffusion",
            "output_node": False},

        # ---- 空间控制：ControlNet（MODEL 补丁）----
        #
        # 注意 `image` 与两个 percent 在**可选**里 —— 这是真机签名，
        # 而 `composition.zimage_controlnet` 的 port_map 也据此只声明必需端口。
        "ZImageFunControlnet": {
            "input": {
                "required": {"model": ["MODEL", {}],
                             "model_patch": ["MODEL_PATCH", {}],
                             "vae": ["VAE", {}],
                             "strength": ["FLOAT", {"default": 1.0, "min": -10.0,
                                                    "max": 10.0, "step": 0.01}]},
                "optional": {"image": ["IMAGE", {}],
                             "inpaint_image": ["IMAGE", {}],
                             "mask": ["MASK", {}],
                             "start_percent": ["FLOAT", {"default": 0.0}],
                             "end_percent": ["FLOAT", {"default": 1.0}]}},
            "output": ["MODEL"], "category": "model/patch",
            "output_node": False},
        "ModelPatchLoader": {
            "input": {"required": {
                "name": [["Z-Image-Turbo-Fun-Controlnet-Union-2.1.safetensors",
                          "Z-Image-Turbo-Fun-Controlnet-Union-2.1-lite-2601-8steps.safetensors"],
                         {}]}},
            "output": ["MODEL_PATCH"], "category": "model/loaders",
            "output_node": False},

        # ---- 采样与落盘 ----
        "KSampler": {
            "input": {"required": {
                "model": ["MODEL", {}], "seed": ["INT", {"default": 0}],
                "steps": ["INT", {"default": 20}],
                "cfg": ["FLOAT", {"default": 8.0}],
                "sampler_name": [["euler", "dpmpp_2m"], {}],
                "scheduler": [["normal", "simple", "karras"], {}],
                "positive": ["CONDITIONING", {}],
                "negative": ["CONDITIONING", {}],
                "latent_image": ["LATENT", {}],
                "denoise": ["FLOAT", {"default": 1.0}]}},
            "output": ["LATENT"], "category": "sampling",
            "output_node": False},
        "CLIPTextEncode": {
            "input": {"required": {"text": ["STRING", {"multiline": True}],
                                   "clip": ["CLIP", {}]}},
            "output": ["CONDITIONING"], "category": "conditioning",
            "output_node": False},
        "VAEDecode": {
            "input": {"required": {"samples": ["LATENT", {}],
                                   "vae": ["VAE", {}]}},
            "output": ["IMAGE"], "category": "latent",
            "output_node": False},
        "LoadImage": {
            "input": {"required": {
                "image": [["p3_gray.png", "p3_depth.png"],
                          {"default": "p3_gray.png"}]}},
            "output": ["IMAGE", "MASK"], "category": "image",
            "output_node": False},
        # 控制信号预处理。范围是**真机约束**（1..31）——
        # `EpisodeRunner` 据此在本地就拦下越界值，
        # 而不是等 ComfyUI 在提交时以 custom_validation_failed 拒绝。
        "ImageBlur": {
            "input": {"required": {
                "image": ["IMAGE", {}],
                "blur_radius": ["INT", {"default": 1, "min": 1, "max": 31,
                                        "step": 1}],
                "sigma": ["FLOAT", {"default": 1.0, "min": 0.1, "max": 10.0,
                                    "step": 0.1}]}},
            "output": ["IMAGE"], "category": "image/filters",
            "output_node": False},
        "SaveImage": {
            "input": {"required": {"images": ["IMAGE", {}],
                                   "filename_prefix": ["STRING",
                                                       {"default": "ComfyUI"}]}},
            "output": [], "category": "image", "output_node": True},
        # ⚠️ **Z-Image 实际不用 checkpoint 加载器**（它是分离加载：
        #    UNETLoader + CLIPLoader + VAELoader）。这里仍保留它，
        #    原因是 model.checkpoint 规则把它当作**模型源**，
        #    而当前编译器只认这一条模型源路径。
        #
        #    **这是一个真实的架构缺口**：加 Z-Image 支持需要一条
        #    "分离加载" 的模型源 kind（见 CHANGELOG alpha.19 的已知限制）。
        #    本夹具先满足现状，以便独立验证 ControlNet 那两条新规则。
        "CheckpointLoaderSimple": {
            "input": {"required": {
                "ckpt_name": [["sd_xl_base_1.0.safetensors",
                                "z_image_turbo_int8_convrot.safetensors"],
                               {"default": "sd_xl_base_1.0.safetensors"}]}},
            "output": ["MODEL", "CLIP", "VAE"], "category": "loaders",
            "output_node": False},
    }

def env_minimal() -> dict:
    """环境 B：最小环境 —— 无 LoadImage、无 IPAdapter、无 SageAttention、无视频、无 TTS。"""
    raw = env_full()
    for drop in ("LoadImage", "IPAdapterAdvanced", "IPAdapterUnifiedLoader",
                 "MiniMaxH3Ref2VA", "MiniMaxH3MemoryEfficientSageAttentionPatch",
                 "QwenTTSNode"):
        raw.pop(drop, None)
    return raw


def env_drifted() -> dict:
    """环境 C：漂移环境 —— 端口改名 + 枚举缩减 + LoRA 文件不在位。"""
    raw = env_minimal()
    ks = raw["KSampler"]["input"]["required"]
    ks.pop("latent_image")
    ks["latent"] = _port("LATENT")
    ks["sampler_name"] = _port(["euler", "ddim"], default="euler")
    raw["LoraLoader"]["input"]["required"]["lora_name"] = _port(
        ["linwan_v1.safetensors"], default="linwan_v1.safetensors")
    return raw


# ==========================================================================
# Z-Image ControlNet（空间控制）
# ==========================================================================

def zimage_ir(*, with_ctrl: bool = True,
              image: str = "p3_gray.png",
              strength: float = 1.0) -> dict:
    """带/不带 Z-Image ControlNet 的最小 IR。"""
    nodes = [
        {"id": "n_unet", "kind": "model.checkpoint",
         "params": {"ckpt_name": "z_image_turbo_int8_convrot.safetensors"}},
        {"id": "n_pos", "kind": "prompt.text",
         "inputs": {"clip": {"ref": "n_unet", "port": "CLIP"}},
         "params": {"text": "一位少女走在山路上"}},
        {"id": "n_neg", "kind": "prompt.negative",
         "inputs": {"clip": {"ref": "n_unet", "port": "CLIP"}},
         "params": {"text": "模糊,低质量"}},
        {"id": "n_lat", "kind": "latent.empty",
         "params": {"width": 832, "height": 480, "batch_size": 1}},
    ]
    ks_model = {"ref": "n_unet", "port": "MODEL"}
    if with_ctrl:
        nodes += [
            {"id": "n_ctrl_img", "kind": "image.file",
             "params": {"image": image}},
            {"id": "n_ctrl", "kind": "composition.zimage_controlnet",
             "inputs": {"model": {"ref": "n_unet", "port": "MODEL"},
                        "vae": {"ref": "n_unet", "port": "VAE"},
                        "image": {"ref": "n_ctrl_img", "port": "IMAGE"}},
             "params": {"strength": strength}},
        ]
        ks_model = {"ref": "n_ctrl", "port": "MODEL"}
    nodes += [
        {"id": "n_ks", "kind": "sample.ksampler",
         "inputs": {"model": ks_model,
                    "positive": {"ref": "n_pos", "port": "CONDITIONING"},
                    "negative": {"ref": "n_neg", "port": "CONDITIONING"},
                    "latent_image": {"ref": "n_lat", "port": "LATENT"}},
         "params": {"steps": 8, "cfg": 4.0, "sampler": "euler",
                    "scheduler": "simple", "denoise": 1.0}},
        {"id": "n_dec", "kind": "decode.vae",
         "inputs": {"samples": {"ref": "n_ks", "port": "LATENT"},
                    "vae": {"ref": "n_unet", "port": "VAE"}}},
        {"id": "n_save", "kind": "io.save_image",
         "inputs": {"images": {"ref": "n_dec", "port": "IMAGE"}},
         "params": {"filename_prefix": "p3"}},
    ]
    return {"irVersion": "little_crew.ir/v1", "graphId": "G-P3",
            "scope": "shot", "bindings": {}, "nodes": nodes,
            "edges": [], "constraints": []}


PATCH_FILE = "Z-Image-Turbo-Fun-Controlnet-Union-2.1-lite-2601-8steps.safetensors"


def _connectivity_ir() -> dict:
    """带控制图的 IR —— 用来验证 image 连线**确实进了图**。"""
    ir = zimage_source_ir()
    for n in ir["nodes"]:
        if n["id"] == "n_ctrl":
            n["inputs"]["image"] = {"ref": "n_ctrl_img", "port": "IMAGE"}
    return ir


def _section_optional_ports() -> None:
    """可选端口进 port_map 的必要性。

    这一节锁住一个**代价很高的缺陷**：`port_map` 曾被误当成"必需端口表"，
    于是把真机签名里可选的 `image` 从里面删掉了。
    但 `_resolve_inputs` **只遍历 `port_map`** —— 不在里面 = 连线被静默丢弃。
    """
    c = _zc()
    res = c.compile(_connectivity_ir())
    g = res.graph

    ctrl = _ctrl_node(g)
    check("P1 ControlNet 节点在图中", ctrl is not None)
    check("P2 ★★ **ControlNet 拿到了 image 输入**（缺了它等于没控制）",
          ctrl is not None and "image" in ctrl["inputs"],
          str(sorted(ctrl["inputs"])) if ctrl else "(无节点)")

    img_id = next((k for k, n in g.items()
                   if n.get("class_type") == "LoadImage"), None)
    check("P3 image 接到 LoadImage 节点",
          ctrl is not None
          and ctrl["inputs"].get("image", [None])[0] == img_id,
          f"{ctrl['inputs'].get('image') if ctrl else None} vs [{img_id}, 0]")

    # 反证：若 port_map 里删掉 image，连线就丢 —— 这条断言让"删回去"立刻失败
    r = c.rules.get("composition.zimage_controlnet")
    check("P4 ★ port_map 含 image / start_percent / end_percent",
          {"image", "start_percent", "end_percent"} <= set(r.port_map),
          str(sorted(r.port_map)))

    # 不声明 image 时也不该报错（它真的是可选的）
    #
    # ⚠️ `zimage_source_ir()` 里**本来就带 image 连线**，所以必须显式摘掉
    # 才测得到"可选"这一面 —— 否则测的是同一条已经通过的路径。
    ir2 = zimage_source_ir()
    for n in ir2["nodes"]:
        if n["id"] == "n_ctrl":
            n["inputs"] = {k: v for k, v in n["inputs"].items()
                           if k != "image"}
    try:
        res2 = c.compile(ir2)
        c2 = _ctrl_node(res2.graph)
        check("P5 不接控制图时也能编译（image 确为可选）",
              c2 is not None and "image" not in c2["inputs"],
              str(sorted(c2["inputs"])) if c2 else "(无节点)")
    except CompileError as e:
        check("P5 不接控制图时也能编译", False, str(e)[:70])


def _section_blur_rule() -> None:
    """控制信号预处理：`composition.image_blur`。

    存在理由：白模的实心剪影带内部明暗，模型会去**描摹骨架的内部细节**，
    人物被驯化成骨架形（实测表现为"完全不像人"）。模糊抹掉内部结构，
    只留"这一团是中景偏左的一个人，这么大"。
    """
    c = _zc()
    r = c.rules.get("composition.image_blur")
    check("Q1 规则已注册", r is not None)
    check("Q2 输出 IMAGE（控制信号是图像，不是条件也不是模型）",
          r.output_ports == ("IMAGE",), str(r.output_ports))
    check("Q3 port_map 含 image", "image" in r.port_map, str(sorted(r.port_map)))

    # ---- 进图：应在 LoadImage 与 ControlNet 之间 ----
    ir = zimage_source_ir()
    ir["nodes"].append({"id": "n_blur", "kind": "composition.image_blur",
                        "inputs": {"image": {"ref": "n_ctrl_img",
                                             "port": "IMAGE"}},
                        "params": {"blur_radius": 18, "sigma": 5.0}})
    for n in ir["nodes"]:
        if n["id"] == "n_ctrl":
            n["inputs"]["image"] = {"ref": "n_blur", "port": "IMAGE"}
    res = c.compile(ir)
    g = res.graph
    blur = _by_type(g, "ImageBlur")
    check("Q4 ImageBlur 进图", len(blur) == 1, f"{len(blur)} 个")
    ctrl = _ctrl_node(g)
    blur_id = next((k for k, n in g.items()
                    if n.get("class_type") == "ImageBlur"), None)
    check("Q5 ★ ControlNet 的 image 接的是**模糊后**的信号",
          ctrl is not None
          and ctrl["inputs"].get("image", [None])[0] == blur_id,
          f"{ctrl['inputs'].get('image') if ctrl else None} vs [{blur_id}, 0]")
    check("Q6 ★ 模糊半径进入图（因而**进入 IR 哈希**，改半径必然重渲）",
          blur and blur[0]["inputs"].get("blur_radius") == 18,
          str(blur[0]["inputs"]) if blur else None)


def _zsrc() -> "ComfyCompiler":
    """用 Z-Image 分离加载作模型源的编译器。"""
    matrix = probe_from_object_info(env_zimage(), endpoint="fixture",
                                    comfy_version="fixture")
    return ComfyCompiler(matrix, default_model_patch=PATCH_FILE,
                         model_source_kind="model.source_zimage")


def zimage_source_ir() -> dict:
    """Z-Image 分离加载的最小 IR。

    模型源节点**同时携带三个权重名** —— `_apply_params` 只遍历本规则的
    `param_map`、忽略多余的键，所以这是合法的。
    """
    return {
        "irVersion": "little_crew.ir/v1", "graphId": "G-ZSRC", "scope": "shot",
        "bindings": {}, "edges": [], "constraints": [],
        "nodes": [
            {"id": "n_zsrc", "kind": "model.source_zimage",
             "params": {"unet_name": "z_image_turbo_int8_convrot.safetensors",
                        "clip_name": "qwen_3_4b_fp4_mixed.safetensors",
                        "vae_name": "ae.safetensors"}},
            # 两个 prompt 节点**故意不声明 clip 连线** —— 测按类型自动接线
            {"id": "n_pos", "kind": "prompt.text",
             "params": {"text": "一位少女"}},
            {"id": "n_neg", "kind": "prompt.negative", "params": {"text": "模糊"}},
            {"id": "n_lat", "kind": "latent.empty",
             "params": {"width": 832, "height": 480, "batch_size": 1}},
            {"id": "n_ctrl_img", "kind": "image.file",
             "params": {"image": "seq.png"}},
            {"id": "n_ctrl", "kind": "composition.zimage_controlnet",
             "inputs": {"model": {"ref": "n_zsrc", "port": "MODEL"},
                        "vae": {"ref": "n_zsrc", "port": "VAE"},
                        "image": {"ref": "n_ctrl_img", "port": "IMAGE"}},
             "params": {"strength": 1.0}},
            {"id": "n_ks", "kind": "sample.ksampler",
             "inputs": {"model": {"ref": "n_ctrl", "port": "MODEL"},
                        "positive": {"ref": "n_pos", "port": "CONDITIONING"},
                        "negative": {"ref": "n_neg", "port": "CONDITIONING"},
                        "latent_image": {"ref": "n_lat", "port": "LATENT"}},
             "params": {"steps": 8, "cfg": 4.0, "sampler": "euler",
                        "scheduler": "simple", "denoise": 1.0}},
            {"id": "n_dec", "kind": "decode.vae",
             "inputs": {"samples": {"ref": "n_ks", "port": "LATENT"},
                        "vae": {"ref": "n_zsrc", "port": "VAE"}}},
            {"id": "n_save", "kind": "io.save_image",
             "inputs": {"images": {"ref": "n_dec", "port": "IMAGE"}},
             "params": {"filename_prefix": "zsrc"}},
        ],
    }


def _by_type(g: dict, ct: str) -> list[dict]:
    return [n for n in g.values() if n.get("class_type") == ct]


def _section_zimage_source() -> None:
    """Z-Image 的**分离加载**模型源。

    这是"一条规则 = 一个节点"这一假设**第三次**成为阻碍后的解法：
    把多节点模式显式声明出来（`requires_loader`），由编译器注入与接线。
    """
    c = _zsrc()

    check("S1 model_source_kind 可指定", c.model_source_kind == "model.source_zimage")
    check("S2 ★ 能提供的端口类型含 CLIP 与 VAE（虽主加载器只出 MODEL）",
          c._model_provided_types() == frozenset({"MODEL", "CLIP", "VAE"}),
          str(sorted(c._model_provided_types())))

    r = c.rules.get("model.source_zimage")
    check("S3 模型源规则声明了辅助加载器",
          r is not None and set(r.requires_loader) ==
          {"model.zimage_clip", "model.zimage_vae"}, str(r.requires_loader))
    check("S4 主加载器是 UNETLoader（不是 CheckpointLoaderSimple）",
          r.class_types == ("UNETLoader",), str(r.class_types))

    res = c.compile(zimage_source_ir())
    g = res.graph
    check("S5 编译通过", len(g) >= 10, f"{len(g)} 个节点")

    check("S6 ★ 注入了 UNETLoader", len(_by_type(g, "UNETLoader")) == 1)
    check("S7 ★ 注入了 CLIPLoader", len(_by_type(g, "CLIPLoader")) == 1)
    check("S8 ★ 注入了 VAELoader", len(_by_type(g, "VAELoader")) == 1)
    check("S9 ★★ **恰好一个模型源**（IR 已声明时不再重复注入）",
          len(_by_type(g, "UNETLoader")) == 1
          and len(_by_type(g, "CheckpointLoaderSimple")) == 0,
          f"UNET={len(_by_type(g, 'UNETLoader'))} "
          f"CKPT={len(_by_type(g, 'CheckpointLoaderSimple'))}")

    # ---- 参数同步：三个文件名各归各家 ----
    unet = _by_type(g, "UNETLoader")[0]
    clip = _by_type(g, "CLIPLoader")[0]
    vae = _by_type(g, "VAELoader")[0]
    check("S10 ★ UNETLoader 用 `unet_name`（不是硬编码的 ckpt_name）",
          unet["inputs"].get("unet_name") == "z_image_turbo_int8_convrot.safetensors"
          and "ckpt_name" not in unet["inputs"],
          str(unet["inputs"]))
    check("S11 ★ CLIPLoader 的权重名来自 IR 的 params",
          clip["inputs"].get("clip_name") == "qwen_3_4b_fp4_mixed.safetensors",
          str(clip["inputs"].get("clip_name")))
    check("S12 ★ CLIPLoader 的 type 是 qwen_image（Z-Image 归此架构）",
          clip["inputs"].get("type") == "qwen_image",
          str(clip["inputs"].get("type")))
    check("S13 ★ VAELoader 的权重名来自 IR 的 params",
          vae["inputs"].get("vae_name") == "ae.safetensors",
          str(vae["inputs"].get("vae_name")))

    # ---- 接线 ----
    cid = next(k for k, n in g.items() if n.get("class_type") == "CLIPLoader")
    clip_ids = {n["inputs"].get("clip", [None])[0]
                for n in g.values() if n.get("class_type") == "CLIPTextEncode"}
    check("S14 ★ 两个 prompt 节点的 clip 都接到**模型源的 CLIPLoader**",
          clip_ids == {cid}, f"接到 {clip_ids}，应为 {{{cid!r}}}")
    check("S15 通知如实说明是「分离加载」",
          any("分离加载" in n.reason for n in res.notices),
          str([n.reason[:40] for n in res.notices if "分离" in n.reason][:1]))

    # ---- 自动注入路径（IR 不声明模型源）----
    ir2 = zimage_source_ir()
    # ⚠️ 删掉模型源节点时，**所有引用它 `inputs` 的边都要一并改掉** ——
    # 否则会得到 DANGLING_REF（那是我这个用例自己的问题，不是实现的）。
    ir2["nodes"] = [n for n in ir2["nodes"] if n["id"] != "n_zsrc"]
    for n in ir2["nodes"]:
        n["inputs"] = {k: v for k, v in (n.get("inputs") or {}).items()
                       if not (isinstance(v, dict) and v.get("ref") == "n_zsrc")}
    try:
        res2 = c.compile(ir2)
        check("S16 IR 不声明模型源时也能自动注入（含辅助加载器）",
              len(_by_type(res2.graph, "UNETLoader")) == 1
              and len(_by_type(res2.graph, "CLIPLoader")) == 1,
              f"UNET={len(_by_type(res2.graph, 'UNETLoader'))} "
              f"CLIP={len(_by_type(res2.graph, 'CLIPLoader'))}")
    except CompileError as e:
        check("S16 IR 不声明模型源时也能自动注入", False, str(e)[:70])


def _zc() -> "ComfyCompiler":
    """构造一个认 Z-Image 环境的编译器。"""
    matrix = probe_from_object_info(env_zimage(), endpoint="fixture",
                                    comfy_version="fixture")
    return ComfyCompiler(matrix, default_model_patch=PATCH_FILE)


def _ctrl_node(graph: dict) -> dict | None:
    return next((n for n in graph.values()
                 if n.get("class_type") == "ZImageFunControlnet"), None)


def _patch_node(graph: dict) -> dict | None:
    return next((n for n in graph.values()
                 if n.get("class_type") == "ModelPatchLoader"), None)


def _section_zimage_controlnet() -> None:
    """Z-Image ControlNet 的编译链。

    实测依据（2026-10-12 真机跑通）：白模 Gray 序列 → 机位与姿态跟随；
    同提示词同种子只换白模，相对基线平均绝对差 35.6~37.8（阈值 8）。
    """
    c = _zc()

    # ---- 规则表本身 ----
    r = c.rules.get("composition.zimage_controlnet")
    check("Z1 新 kind 已注册", r is not None)
    check("Z2 ★ 与既有的 composition.controlnet **是不同 kind**"
          "（前者补 MODEL，后者补 CONDITIONING）",
          c.rules.get("composition.controlnet") is not None
          and c.rules.get("composition.controlnet").output_ports
          != r.output_ports,
          f"zimage={r.output_ports} std="
          f"{c.rules.get('composition.controlnet').output_ports}")
    check("Z3 声明了 requires_loader（MODEL_PATCH 需独立加载器）",
          r.requires_loader == {"composition.model_patch": "MODEL_PATCH"},
          str(r.requires_loader))
    check("Z4 **不设降级目标**（空间控制没有「退一步还能用」的替代品）",
          r.degrade_to is None, str(r.degrade_to))
    check("Z5 required=False（缺 ControlNet 不阻断整条链）",
          r.required is False)
    # ⚠️ 这一条曾经写成 set(r.port_map) == {"model", "vae"} —— 想表达
    # "image 在真机签名里是可选"。**那是错的，而且代价很高**：
    # _resolve_inputs 只遍历 port_map，从里面删掉 image 会让 IR 声明的
    # 连线**被静默丢弃** —— 产出的图里 ControlNet 没有 image 输入，
    # 于是它与"完全不加控制"逐像素相同。
    # **"可选"应表达为 required=False 与 defaults，不是从 port_map 里删。**
    check("Z6 ★ port_map 必须含 image（否则连线被静默丢弃）",
          {"model", "vae", "image"} <= set(r.port_map),
          str(sorted(r.port_map)))

    # ---- 实际编译 ----
    res = c.compile(zimage_ir())
    g = res.graph
    check("Z7 带 ControlNet 编译通过", len(g) >= 9, f"{len(g)} 个节点")
    check("Z8 ★ 自动注入了 ModelPatchLoader",
          _patch_node(g) is not None,
          str([n.get("class_type") for n in g.values()]))

    ctrl = _ctrl_node(g)
    check("Z9 ControlNet 节点在图中", ctrl is not None)
    pnode = _patch_node(g)
    check("Z10 注入的加载器带上了权重文件名（来自 default_model_patch）",
          pnode is not None and pnode["inputs"].get("name") == PATCH_FILE,
          str(pnode["inputs"].get("name") if pnode else None))

    # ★★ 这一条锁住一个真实缺陷 ★★
    pat_id = next((k for k, n in g.items()
                   if n.get("class_type") == "ModelPatchLoader"), None)
    check("Z11 ★★ model_patch **接到注入的加载器**，而不是模型源",
          ctrl is not None
          and ctrl["inputs"].get("model_patch", [None])[0] == pat_id,
          f"实际接到 {ctrl['inputs'].get('model_patch') if ctrl else None}"
          f"，应为 [{pat_id}, 0]")

    # 对照：不带 ControlNet
    res2 = c.compile(zimage_ir(with_ctrl=False))
    check("Z12 不带 ControlNet 时不注入 ModelPatchLoader",
          _patch_node(res2.graph) is None)
    check("Z13 两种 IR 的图哈希不同（ControlNet 真的改变了图）",
          res.graph_hash != res2.graph_hash,
          f"{res.graph_hash[:16]} vs {res2.graph_hash[:16]}")

    # 显式声明加载器时不该重复注入
    ir3 = zimage_ir()
    ir3["nodes"].append({"id": "n_patch", "kind": "composition.model_patch",
                         "params": {"name": "other.safetensors"}})
    ir3["nodes"] = [n for n in ir3["nodes"]
                    if n["id"] != "n_ctrl"] + [
        {"id": "n_ctrl", "kind": "composition.zimage_controlnet",
         "inputs": {"model": {"ref": "n_unet", "port": "MODEL"},
                    "vae": {"ref": "n_unet", "port": "VAE"},
                    "image": {"ref": "n_ctrl_img", "port": "IMAGE"}},
         "params": {"strength": 1.0}}]
    try:
        res3 = c.compile(ir3)
        n_patch = sum(1 for n in res3.graph.values()
                      if n.get("class_type") == "ModelPatchLoader")
        check("Z14 显式声明组装后 ModelPatchLoader 不重复（恰好 1 个）",
              n_patch == 1, f"{n_patch} 个")
    except CompileError as e:
        check("Z14 显式声明加载器时可编译", False, str(e)[:80])

    # 强度作为参数进入图
    res4 = c.compile(zimage_ir(strength=0.5))
    c4 = _ctrl_node(res4.graph)
    check("Z15 strength 作为参数写入节点",
          c4 is not None and abs(float(c4["inputs"].get("strength", -1))
                                 - 0.5) < 1e-9,
          str(c4["inputs"].get("strength") if c4 else None))

    # 通知如实说明注入
    auto = [n for n in res.notices
            if "MODEL_PATCH" in str(getattr(n, "to", ""))]
    check("Z16 注入产出了可追溯的通知", bool(auto), f"{len(auto)} 条")
    check("Z17 ★ 通知里写明**注入的加载器 id**（不是笼统说模型源）",
          any("n_auto_composition_model_patch" in str(getattr(n, "to", ""))
              for n in res.notices),
          str([getattr(n, "to", "") for n in auto][:2]))


# ==========================================================================
# IR 夹具
# ==========================================================================

def shot_ir(*, checkpoint: str = "sd_xl_base_1.0.safetensors",
            constraint_refs=("cr_face_007", "cr_costume_007")) -> dict:
    return {
        "irVersion": "little_crew.ir/v1",
        "graphId": "G-ep001-sh005",
        "scope": "shot",
        "bindings": {
            "CHAR-0007": {
                "anchorSet": {"front": "char/CHAR-0007/anchor_front_v3.png"},
                "anchorGeneration": 3,
                "loraFile": "linwan_v3.safetensors",
                "loraStrength": 0.75,
                "embeddingId": "emb_8f21",
            }
        },
        "nodes": [
            {"id": "n_neg", "kind": "prompt.negative",
             "params": {"text": "多手,睁眼异常,服装变色,白天"}},
            {"id": "n_latent", "kind": "latent.empty",
             "params": {"width": 768, "height": 1344, "batch_size": 1}},
            {"id": "n_ref", "kind": "identity.reference",
             "inputs": {"image": {"binding": "CHAR-0007.anchorSet.front"}},
             "params": {"weight": 0.65, "weight_type": "linear"},
             "semantic": {"characterRef": "CHAR-0007",
                          "constraintRefs": list(constraint_refs)}},
            {"id": "n_prompt", "kind": "prompt.text",
             "params": {"text": "雨夜天台, 米色风衣, 回头, 近景, 缓推"},
             "semantic": {"characterRef": "CHAR-0007",
                          "constraintRefs": ["cr_costume_007"]}},
            {"id": "n_ckpt", "kind": "model.checkpoint",
             "params": {"ckpt_name": checkpoint}},
            {"id": "n_ks", "kind": "sample.ksampler",
             "inputs": {"model": {"ref": "n_lora", "port": "MODEL"},
                        "positive": {"ref": "n_prompt", "port": "CONDITIONING"},
                        "negative": {"ref": "n_neg", "port": "CONDITIONING"},
                        "latent_image": {"ref": "n_latent", "port": "LATENT"}},
             "params": {"steps": 20, "cfg": 5.5, "sampler": "dpmpp_2m",
                        "scheduler": "karras", "denoise": 1.0}},
            {"id": "n_lora", "kind": "identity.lora",
             "inputs": {"model": {"ref": "n_ckpt", "port": "MODEL"},
                        "clip": {"ref": "n_ckpt", "port": "CLIP"}},
             "params": {"strength": 0.75},
             "semantic": {"characterRef": "CHAR-0007",
                          "constraintRefs": ["cr_face_007"]}},
            {"id": "n_decode", "kind": "decode.vae",
             "inputs": {"samples": {"ref": "n_ks", "port": "LATENT"},
                        "vae": {"ref": "n_ckpt", "port": "VAE"}}},
            {"id": "n_save", "kind": "io.save_image",
             "inputs": {"images": {"ref": "n_decode", "port": "IMAGE"}},
             "params": {"filename_prefix": "EP001_SH005"}},
        ],
        "edges": [
            {"id": "e1", "type": "data",
             "from": {"node": "n_lora", "port": "MODEL"},
             "to": {"node": "n_ks", "port": "model"}},
            {"id": "e2", "type": "constraint",
             "from": {"node": "n_ref", "port": "image"},
             "to": {"node": "n_ks", "port": "latent"},
             "assertion": {"op": "identity_similarity", "threshold": 0.82,
                           "against": "CHAR-0007.anchor_set"},
             "enforcement": "hard"},
            {"id": "e3", "type": "constraint",
             "from": {"node": "n_prompt", "port": "text"},
             "to": {"node": "n_ks", "port": "positive"},
             "assertion": {"op": "token_contains_all", "tokens": ["米色", "风衣"]},
             "enforcement": "hard"},
        ],
        # 图级约束声明：节点通过 semantic.constraintRefs 引用它们。
        # 编译器会校验引用完整性 —— 悬空引用是典型的 IR 书写错误。
        "constraints": [
            {"ref": "cr_face_007",
             "assertion": {"op": "identity_similarity", "threshold": 0.82,
                           "against": "CHAR-0007.anchor_set"},
             "enforcement": "hard",
             "description": "林晚面部与锚点集相似度"},
            {"ref": "cr_costume_007",
             "assertion": {"op": "token_contains_all", "tokens": ["米色", "风衣"]},
             "enforcement": "hard",
             "description": "林晚服装 token 一致性"},
            {"ref": "cr_weather",
             "assertion": {"op": "token_contains_all", "tokens": ["雨", "夜"]},
             "enforcement": "soft",
             "description": "雨夜氛围"},
        ],
        "provenance": {
            "authorAgent": "storyboard.shot_composer",
            "createdAt": "2026-10-11T10:20:00+08:00",
            "seedDerivation": {"projectSeed": 20261011, "episodeIdx": 1,
                               "sceneIdx": 3, "shotIdx": 5, "attempt": 0},
        },
    }


# ==========================================================================
# 测试
# ==========================================================================

def _section_optional_lora_degrade() -> None:
    """可选身份节点缺 LoRA 文件时**降级出片**，而不是整集编译失败。

    ## 这一节存在的理由（2026-10-23 审计 H-13）
    链路：本机没装 IPAdapter -> `identity.reference` 沿 `degrade_to` 降级成
    `identity.lora` -> 它的 `param_map` 要 `lora_name` -> 而平台产出的
    binding 只有 `shotId`/`anchorSet`，**没有 `loraFile`**
    （角色 LoRA 本机训不出来）-> `_apply_params` 抛 `MISSING_BINDING`
    -> **整集编译硬失败**。

    两个 kind 都是 `required=False` —— **负向降级**：可选能力把整集挡住了。

    ⚠️ 为什么此前没被测出来：默认夹具 `env_full()` **装着 IPAdapter**，
    规则永远命中第一候选，降级路径**从未被端到端走过**。
    所以这里用 `env_minimal()`（无 IPAdapter）。

    ⚠️ 也不能在这里 import `agent` 层（`ir_bridge`）来造 IR ——
    `compiler` 不得依赖 `agent`（架构守卫 R1 会拦）。因此直接改夹具
    `shot_ir()` 的 binding：把 `loraFile` 删掉，正是**平台真实产出**的形态
    （`ir_bridge` 只写 `shotId`/`anchorSet`）。
    """
    ir = copy.deepcopy(shot_ir())
    ir["bindings"]["CHAR-0007"].pop("loraFile", None)

    check("L1 前置：IR 里有 identity.reference 节点",
          any(n["kind"] == "identity.reference" for n in ir["nodes"]),
          str([n["kind"] for n in ir["nodes"]]))
    check("L2 前置：binding 里**没有** loraFile（平台真实产出的形态）",
          "loraFile" not in (ir["bindings"].get("CHAR-0007") or {}))

    m_min = probe_from_object_info(env_minimal(), endpoint="fixture",
                                   comfy_version="0.3.x")
    try:
        res = ComfyCompiler(m_min).compile(ir)
    except CompileError as e:
        check("L3 ★★ 缺 IPAdapter + 缺 loraFile 时仍然能编译（降级不阻断）",
              False, f"{e.code}: {e.message[:80]}")
        return
    check("L3 ★★ 缺 IPAdapter + 缺 loraFile 时仍然能编译（降级不阻断）", True)

    def _kn(n) -> str:
        return str(getattr(n.kind, "value", n.kind))

    kinds = [_kn(n) for n in res.notices]
    skipped_ids = {n.ir_node_id for n in res.notices
                   if _kn(n) == "SKIPPED_OPTIONAL"}
    check("L4 ★ 发了 DEGRADED（IPAdapter 不在 -> 降到 LoRA）",
          "DEGRADED" in kinds, str(kinds))
    check("L5 ★★ 缺 loraFile 的节点被记录为 SKIPPED_OPTIONAL（n_ref / n_lora）",
          {"n_ref", "n_lora"} <= skipped_ids, str(sorted(skipped_ids)))
    check("L6 ★★ 被跳过的节点**不在图里**（不留引用不存在文件的节点）",
          "n_ref" not in res.graph and "n_lora" not in res.graph,
          str(sorted(res.graph)))

    ks_model = (res.graph.get("n_ks") or {}).get("inputs", {}).get("model")
    check("L7 ★★ 下游已重接到存活祖先（n_ks.model -> n_ckpt）",
          isinstance(ks_model, list) and ks_model == ["n_ckpt", 0],
          str(ks_model))

    dangling = [(nid, port, val) for nid, n in res.graph.items()
                for port, val in (n.get("inputs") or {}).items()
                if isinstance(val, list) and len(val) == 2
                and str(val[0]) not in res.graph]
    check("L8 ★★ 产出的图没有悬空引用（重接是干净的）",
          not dangling, str(dangling[:2]))

    # 对照组：完整环境（有 IPAdapter）—— 锚点图那条路仍然成立，
    # 但**缺 loraFile 的 n_lora 照样要跳过**（与能力无关，是数据缺）
    m_full = probe_from_object_info(env_full(), endpoint="fixture",
                                    comfy_version="0.3.x")
    res_full = ComfyCompiler(m_full).compile(ir)
    check("L9 有 IPAdapter 时 n_ref 仍在图里（控制组）",
          "n_ref" in res_full.graph, str(sorted(res_full.graph)))
    check("L10 缺 loraFile 与能力无关：完整环境里 n_lora 同样被跳过",
          "n_lora" not in res_full.graph, str(sorted(res_full.graph)))

    # ---- L11：兜底重接目标不在 IR 里时，必须是**结构化失败** ----
    #
    # `_remap_table` 的兜底是常量 `MODEL_SOURCE_ID`（`n_ckpt`）。
    # 分离式模型源（Z-Image）通路里没有这个节点 —— 而消费端 `nodes[tgt]`
    # 是直接下标，曾经抛**裸 `KeyError`**（审计 H-6 的崩溃点）。
    #
    # 这条此前要「权重文件缺失」才走得到；L 节（H-13）修好之后，
    # 「缺 loraFile 就跳过」成了**常规路径** —— 于是降级时必踩。
    # 所以这条断言必须跟着加：它锁的是「病从结构化变成裸崩溃」这条反向退化。
    ir_no_ckpt = {
        "irVersion": "little_crew.ir/v1",
        "graphId": "G-no-ckpt", "scope": "shot",
        "bindings": {"C1": {"anchorSet": {"front": "char/C1/front.png"}}},
        "nodes": [
            {"id": "n_unet", "kind": "model.source_zimage",
             "params": {"unet_name": "z_image_turbo_bf16.safetensors"}},
            {"id": "n_ref", "kind": "identity.reference",
             "inputs": {"image": {"binding": "C1.anchorSet.front"}},
             "params": {"weight": 0.65, "weight_type": "linear"},
             "semantic": {"characterRef": "C1"}},
            {"id": "n_pos", "kind": "prompt.text",
             "inputs": {"clip": {"ref": "n_unet", "port": "CLIP"}},
             "params": {"text": "少女"}},
            {"id": "n_neg", "kind": "prompt.negative",
             "inputs": {"clip": {"ref": "n_unet", "port": "CLIP"}},
             "params": {"text": "多手"}},
            {"id": "n_latent", "kind": "latent.empty",
             "params": {"width": 768, "height": 1344, "batch_size": 1}},
            # ⚠️ model 指向会被跳过的 n_ref —— 与 `ir_bridge` 有锚点时的接线一致
            {"id": "n_ks", "kind": "sample.ksampler",
             "inputs": {"model": {"ref": "n_ref", "port": "MODEL"},
                        "positive": {"ref": "n_pos", "port": "CONDITIONING"},
                        "negative": {"ref": "n_neg", "port": "CONDITIONING"},
                        "latent_image": {"ref": "n_latent", "port": "LATENT"}},
             "params": {"steps": 20, "cfg": 5.5, "sampler": "dpmpp_2m",
                        "scheduler": "karras", "denoise": 1.0}},
            {"id": "n_dec", "kind": "decode.vae",
             "inputs": {"samples": {"ref": "n_ks", "port": "LATENT"},
                        "vae": {"ref": "n_unet", "port": "VAE"}}},
            {"id": "n_save", "kind": "io.save_image",
             "inputs": {"images": {"ref": "n_dec", "port": "IMAGE"}},
             "params": {"filename_prefix": "probe"}},
        ],
        "edges": [],
    }
    expect_compile_error(
        "L11 ★★ 兜底重接目标不在 IR 里 -> 结构化 CompileError（不是裸 KeyError）",
        CompileErrorCode.INCOMPLETE_GRAPH,
        ComfyCompiler(m_min, model_source_kind="model.source_zimage").compile,
        ir_no_ckpt)


def _section_self_check() -> None:
    """产出图引用自检 + 兜底 id 推导 + 端口类型表同源（审计 M-18 / H-6 / H-14 / H-15）。

    ## 为什么这一节最该存在
    ARCHITECTURE §2.1.2 的要求是「**编译器必须自证产出的图是完整的**」。
    3.5) 只验「必需端口有没有被填」，不验「填进去的连线指向谁、指向第几个输出」
    —— 而后者恰恰是重接/注入最容易出错的地方：

      · `H-6`  重接兜底写死 `n_ckpt`，分离式模型源通路里它不存在 -> 悬空
      · `H-15` `_resolve_inputs` 写死 `[n_ckpt, 0/1]` -> 悬空 + 越界
      · `H-14` `MODEL_PATCH` 不在连线端口表 -> 终检**漏检**它

    实测：加上自检后，Z-Image 通路的 `n_ctrl.model -> 'n_ckpt'` 立刻被抓住
    （那是一个一直存在、只在提交时才暴露的悬空引用）。
    """
    m = probe_from_object_info(env_zimage(), endpoint="fixture",
                               comfy_version="0.3.x")

    # ---- M1/M2：闸门必须**证明自己会落下** ----
    #
    # 正常路径上，上游守卫（兜底 id 推导 / 输出索引推断 / 模型源映射）
    # 已经把这两类缺陷挡住了 —— 所以闸门得**直接**测：
    # 构造一张有悬空引用 / 越界索引的图，看它是否判死。
    c_ref = ComfyCompiler(m, model_source_kind="model.source_zimage")
    expect_compile_error(
        "M1 ★★ 悬空引用 -> DANGLING_REF（而不是让它流到提交）",
        CompileErrorCode.DANGLING_REF,
        c_ref._check_graph_refs,
        {"n_a": {"class_type": "KSampler", "inputs": {"model": ["n_ghost", 0]}}})

    expect_compile_error(
        "M2 ★★ 越界输出索引 -> PORT_MISMATCH（而不是让 ComfyUI 500）",
        CompileErrorCode.PORT_MISMATCH,
        c_ref._check_graph_refs,
        {"n_ckpt": {"class_type": "CheckpointLoaderSimple", "inputs": {}},
         "n_a": {"class_type": "KSampler", "inputs": {"model": ["n_ckpt", 9]}}})

    # 合法图不得被误杀（`or {}` 保护下的普通列表值不该被当连线）
    try:
        c_ref._check_graph_refs({
            "n_ckpt": {"class_type": "CheckpointLoaderSimple", "inputs": {}},
            "n_a": {"class_type": "KSampler",
                    "inputs": {"model": ["n_ckpt", 0],
                               "size": [832, 480],          # 普通列表值
                               "note": "x"}},
        })
        check("M2b ★ 自检不误杀合法图（普通列表值不当连线）", True)
    except CompileError as e:                                 # noqa: BLE001
        check("M2b ★ 自检不误杀合法图（普通列表值不当连线）", False,
              f"{e.code}: {e.message[:60]}")

    # ---- M3：正常图**不该**被自检误杀（自检只该抓真缺陷） ----
    ok = ComfyCompiler(m, model_source_kind="model.source_zimage").compile(
        zimage_source_ir())
    dangling = [(nid, p, v) for nid, n in ok.graph.items()
                for p, v in (n.get("inputs") or {}).items()
                if isinstance(v, list) and len(v) == 2
                and isinstance(v[0], str) and str(v[0]) not in ok.graph]
    check("M3 正常 Z-Image 图零悬空引用（自检不是误报发生器）",
          not dangling, str(dangling[:2]))

    # ---- M4：兜底 id 按配置推导（H-6） ----
    c_ck = ComfyCompiler(m, model_source_kind="model.checkpoint")
    c_z = ComfyCompiler(m, model_source_kind="model.source_zimage")
    check("M4 ★★ 兜底 id 按配置推导：checkpoint -> n_ckpt / Z-Image -> n_src_*",
          c_ck._model_source_default_id() == "n_ckpt"
          and c_z._model_source_default_id() == "n_src_model_source_zimage",
          f"{c_ck._model_source_default_id()} / {c_z._model_source_default_id()}")

    lonely = {"n_ref": {"id": "n_ref", "kind": "identity.reference",
                        "inputs": {"image": {"binding": "C1.anchorSet.front"}}}}
    remap = c_z._remap_table({"n_ref"}, {}, lonely)
    check("M5 ★★ 无可上溯祖先时，兜底指向**本配置**的模型源（不再是硬编码 n_ckpt）",
          remap.get("n_ref", ("", ""))[0] == "n_src_model_source_zimage",
          str(remap))

    # ---- M6/M7：两张端口类型表**同源**（H-14） ----
    from little_crew.capability.capability import LINK_PORT_DTYPES
    from little_crew.compiler.compiler import _KNOWN_PORT_TYPES

    check("M6 ★★ 编译器不再维护第二张端口类型表（直接派生自 capability）",
          set(_KNOWN_PORT_TYPES) == set(LINK_PORT_DTYPES),
          str(sorted(set(_KNOWN_PORT_TYPES) ^ set(LINK_PORT_DTYPES))[:6]))
    check("M7 ★ MODEL_PATCH 是连线端口（否则 3.5) 终检**漏检**它）",
          "MODEL_PATCH" in LINK_PORT_DTYPES)
    check("M8 ★ PHOTOMAKER 也在同一张表里（否则重接时跳过类型校验）",
          "PHOTOMAKER" in LINK_PORT_DTYPES)

    # 规则表里声明过的自定义张量类型，必须在表里 —— 否则又会静默漏检
    from little_crew.compiler.compiler import DEFAULT_RULES

    declared: set[str] = set()
    for rule in DEFAULT_RULES:
        declared |= {str(v) for v in (rule.requires_loader or {}).values()}
    missing = sorted(t for t in declared
                     if t.upper() not in LINK_PORT_DTYPES
                     and t.upper() not in {"INT", "FLOAT", "STRING", "BOOLEAN"})
    check("M9 ★★ 规则表里出现的张量类型都在连线端口表里",
          not missing, str(missing))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    print("=" * 78)
    print("ComfyUI 编译器与能力矩阵 —— 测试")
    print("=" * 78)

    full = probe_from_object_info(env_full(), endpoint="local:8188", comfy_version="0.3.x")
    minimal = probe_from_object_info(env_minimal(), endpoint="local:8188",
                                     comfy_version="0.3.x")
    drifted = probe_from_object_info(env_drifted(), endpoint="local:8188",
                                     comfy_version="0.3.x")

    # ---------- C1 能力矩阵基础 ----------
    check("C1 节点计数正确", full.node_count() == len(env_full()),
          f"{full.node_count()} vs {len(env_full())}")
    check("C1 节点存在性查询", full.has("KSampler") and not full.has("NoSuchNode"))
    check("C1 按优先级选型", full.first_available(("NoSuch", "KSampler")) == "KSampler")
    check("C1 枚举候选可见",
          "dpmpp_2m" in (full.enum_values("KSampler", "sampler_name") or ()),
          str(full.enum_values("KSampler", "sampler_name")))
    check("C1 文件型输入被识别",
          full.spec("LoraLoader").port("lora_name").is_file_input,
          "lora_name 应被识别为文件型输入")

    # ---------- C1b 缓存失效钩子（无法从 /object_info 探测，靠已知表补全）----------
    li = full.spec("LoadImage")
    check("C1b 已知节点的失效钩子被正确补全（LoadImage.IS_CHANGED）",
          li.fingerprint_hook == "IS_CHANGED"
          and li.determinism == DeterminismClass.HOOKED.value
          and li.fingerprint_source == "known_table",
          f"hook={li.fingerprint_hook} det={li.determinism} src={li.fingerprint_source}")
    ks_det = full.spec("KSampler").determinism
    check("C1b 官方确定性节点被判为 PURE（降噪：避免告警疲劳）",
          ks_det == DeterminismClass.PURE.value, ks_det)
    check("C1b 未收录的自定义节点判为 UNKNOWN（保守，不默认 PURE）",
          probe_from_object_info({"MyCustomNode": {"input": {"required": {}},
                                                   "output": ["IMAGE"]}}
                                 ).spec("MyCustomNode").determinism
          == DeterminismClass.UNKNOWN.value, "")
    check("C1b 钩子来源可追溯（用于影响评估）",
          "sha256" in li.fingerprint_of, li.fingerprint_of)

    # ---------- C2 环境哈希 ----------
    h1 = full.catalog_hash()
    h2 = probe_from_object_info(env_full(), endpoint="OTHER:9999").catalog_hash()
    check("C2 环境哈希与 endpoint 无关", h1 == h2)

    with_tooltip = env_full()
    with_tooltip["KSampler"]["input"]["required"]["seed"][1]["tooltip"] = "随机种子"
    with_tooltip["KSampler"]["description"] = "采样器"
    h3 = probe_from_object_info(with_tooltip).catalog_hash()
    check("C2 环境哈希忽略 tooltip/description（装翻译插件不该全量重跑）", h1 == h3)

    h4 = minimal.catalog_hash()
    check("C2 环境哈希对节点集合敏感", h1 != h4)
    enum_shift = env_full()
    enum_shift["KSampler"]["input"]["required"]["sampler_name"][0].append("heun")
    check("C2 环境哈希对枚举候选敏感",
          probe_from_object_info(enum_shift).catalog_hash() != h1)

    # 失效钩子的存在与否直接改变 ComfyUI 的缓存键计算方式，
    # 因此属于 ABI 变化 —— 必须使环境哈希改变。
    hook_removed = probe_from_object_info(env_full(), declared_hooks={"LoadImage": False})
    check("C2 环境哈希对失效钩子的存在性敏感（ABI 变化）",
          hook_removed.catalog_hash() != h1,
          "loadImage 从 HOOKED 变为 PURE 应改变环境身份")
    check("C2 显式声明可覆盖已知表",
          hook_removed.spec("LoadImage").fingerprint_hook is None
          and hook_removed.spec("LoadImage").determinism == DeterminismClass.PURE.value,
          f"{hook_removed.spec('LoadImage').determinism}")

    # 钩子来源不同但事实相同时，环境哈希不应改变（来源不是环境身份的一部分）
    same_fact = probe_from_object_info(env_full(), declared_hooks={"LoadImage": True})
    check("C2 钩子事实相同则环境哈希相同（来源不影响身份）",
          same_fact.catalog_hash() == h1,
          "declared 与 known_table 指向同一事实时应等价")

    # ---------- C3 快照往返 ----------
    snap = full.to_snapshot()
    restored = CapabilityMatrix.from_snapshot(json.loads(json.dumps(snap)))
    check("C3 快照往返后哈希一致", restored.catalog_hash() == h1)
    check("C3 快照含全部节点", restored.node_count() == full.node_count())

    # ---------- C4 完整环境：干净编译 ----------
    comp_full = ComfyCompiler(full)
    r_full = comp_full.compile(shot_ir())
    check("C4 完整环境编译成功", len(r_full.graph) >= 8, f"nodes={len(r_full.graph)}")
    check("C4 无 blocker 漂移", not r_full.blocked,
          "; ".join(d.detail for d in r_full.drifts if d.severity == Severity.BLOCKER.value))
    check("C4 无降级通知",
          [n for n in r_full.notices if n.kind == NoticeKind.DEGRADED] == [],
          str([n.as_dict() for n in r_full.notices]))
    check("C4 阈值未被调整", r_full.threshold_adjustments == {},
          str(r_full.threshold_adjustments))
    check("C4 使用显式声明的 checkpoint",
          r_full.graph["n_ckpt"]["inputs"]["ckpt_name"] == "sd_xl_base_1.0.safetensors")

    ks = r_full.graph["n_ks"]
    check("C4 KSampler class_type 正确", ks["class_type"] == "KSampler")
    check("C4 KSampler 端口连接正确",
          ks["inputs"]["model"] == ["n_lora", 0]
          and ks["inputs"]["positive"] == ["n_prompt", 0]
          and ks["inputs"]["negative"] == ["n_neg", 0]
          and ks["inputs"]["latent_image"] == ["n_latent", 0],
          json.dumps({k: v for k, v in ks["inputs"].items() if isinstance(v, list)},
                     ensure_ascii=False))
    check("C4 KSampler widget 值映射正确",
          ks["inputs"]["steps"] == 20 and ks["inputs"]["cfg"] == 5.5
          and ks["inputs"]["sampler_name"] == "dpmpp_2m"
          and ks["inputs"]["scheduler"] == "karras",
          json.dumps({k: ks["inputs"][k] for k in
                      ("steps", "cfg", "sampler_name", "scheduler", "denoise")}))
    check("C4 种子已按 seedDerivation 派生",
          isinstance(ks["inputs"].get("seed"), int) and ks["inputs"]["seed"] > 0,
          str(ks["inputs"].get("seed")))

    check("C4 LoRA 文件名从 bindings 解析",
          r_full.graph["n_lora"]["inputs"]["lora_name"] == "linwan_v3.safetensors",
          str(r_full.graph["n_lora"]["inputs"].get("lora_name")))
    # 参考图必须是"路径 -> LoadImage -> IMAGE 引用"，不能把路径字符串直接塞进 IMAGE 端口。
    # 早期版本正是那样做的：编译"成功"但真实执行必然报类型错误。
    ref_img = r_full.graph["n_ref"]["inputs"].get("image")
    loader_id = ref_img[0] if isinstance(ref_img, list) else None
    check("C4 参考图经 LoadImage 转换后接入 IMAGE 端口（而非直接塞路径字符串）",
          isinstance(ref_img, list) and len(ref_img) == 2 and ref_img[1] == 0
          and loader_id in r_full.graph
          and r_full.graph[loader_id]["class_type"] == "LoadImage",
          f"image={ref_img}")
    check("C4 自动注入的 LoadImage 携带正确的嵌套 binding 路径",
          loader_id is not None
          and r_full.graph[loader_id]["inputs"]["image"]
          == "char/CHAR-0007/anchor_front_v3.png",
          str(r_full.graph.get(loader_id, {}).get("inputs")))
    check("C4 注入的 LoadImage 节点 id 由路径确定性派生（编译可复现）",
          loader_id == f"n_loadimg_{__import__('hashlib').sha256(b'char/CHAR-0007/anchor_front_v3.png').hexdigest()[:10]}",
          str(loader_id))
    check("C4 注入被记录在 load_image_injections 中（可观测）",
          loader_id in r_full.load_image_injections,
          str(r_full.load_image_injections))
    check("C4 注入产生 AUTO_INJECTED 通知",
          any(n.kind == NoticeKind.AUTO_INJECTED and n.ir_node_id == "n_ref"
              for n in r_full.notices),
          str([n.as_dict() for n in r_full.notices
               if n.kind == NoticeKind.AUTO_INJECTED]))

    # ---------- C5 编译纯度与图哈希 ----------
    r_full2 = comp_full.compile(shot_ir())
    check("C5 编译是纯函数（图哈希稳定）", r_full.graph_hash == r_full2.graph_hash)
    check("C5 IR 哈希与内容哈希一致", r_full.ir_hash.startswith("sha256:"))
    check("C5 图哈希随 IR 语义变化",
          comp_full.compile(shot_ir(checkpoint="animagine_xl.safetensors")).graph_hash
          != r_full.graph_hash)

    # ---------- C6 最小环境：自动降级 ----------
    comp_min = ComfyCompiler(minimal)
    r_min = comp_min.compile(shot_ir())
    degradations = [n for n in r_min.notices if n.kind == NoticeKind.DEGRADED]
    check("C6 缺 IPAdapter 时触发降级", len(degradations) == 1,
          str([n.as_dict() for n in r_min.notices]))
    if degradations:
        d = degradations[0]
        check("C6 降级链正确（identity.reference -> identity.lora）",
              "identity.reference" in d.frm and "identity.lora" in d.to,
              f"{d.frm} -> {d.to}")
        check("C6 降级通知携带修复建议", "IPAdapter" in d.ui_hint, d.ui_hint)
        check("C6 降级通知携带影响评估", bool(d.impact), d.impact[:60])
        check("C6 降级通知标记可回滚", d.reversible is True)
    check("C6 降级后仍能编译出可执行图", len(r_min.graph) >= 8,
          f"nodes={len(r_min.graph)}")

    # 降级补偿的真实行为：夹具里同时存在独立的 identity.lora 节点（n_lora）
    # 与 identity.reference 节点（n_ref）。构造"只有 identity.reference"的 IR，
    # 使降级成为唯一身份锁定手段，补偿效果才可观测。
    ir_ref_only = shot_ir()
    ir_ref_only["nodes"] = [n for n in ir_ref_only["nodes"]
                            if n["kind"] != "identity.lora"]
    for n in ir_ref_only["nodes"]:
        if n["id"] == "n_ks":
            n["inputs"]["model"] = {"ref": "n_ckpt", "port": "MODEL"}
    ir_ref_only["edges"] = [e for e in ir_ref_only["edges"]
                            if e["from"]["node"] != "n_lora" and e["to"]["node"] != "n_lora"]
    r_inj = comp_min.compile(ir_ref_only)
    injected_lora = next((cid for cid, g in r_inj.graph.items()
                          if g["class_type"] == "LoraLoader"
                          and (g.get("_meta") or {}).get("irNodeId") == "n_ref"), None)
    check("C6 降级补偿：LoRA 权重按源规则提高（0.90）",
          injected_lora is not None
          and r_inj.graph[injected_lora]["inputs"]["strength_model"] == 0.90,
          f"injected={injected_lora} "
          f"inputs={r_inj.graph.get(injected_lora, {}).get('inputs') if injected_lora else None}")
    check("C6 降级补偿的同时阈值被放宽",
          r_inj.threshold_adjustments.get("cr_face_007") == 0.08,
          str(r_inj.threshold_adjustments))

    r_noinj = comp_full.compile(ir_ref_only)
    check("C6 未降级时不做补偿、不放宽阈值（对照）",
          r_noinj.graph.get("n_ref", {}).get("class_type") == "IPAdapterAdvanced"
          and r_noinj.threshold_adjustments == {},
          f"n_ref={r_noinj.graph.get('n_ref', {}).get('class_type')} "
          f"adjust={r_noinj.threshold_adjustments}")

    # 最小环境：n_ref 沿降级链被替换为 LoraLoader，_meta.irKind 保留原始语义
    check("C6 降级为替换语义：n_ref 编译为 LoraLoader 且保留 irKind",
          r_min.graph["n_ref"]["class_type"] == "LoraLoader"
          and r_min.graph["n_ref"]["_meta"]["irKind"] == "identity.reference",
          json.dumps(r_min.graph["n_ref"]["_meta"], ensure_ascii=False))
    check("C6 降级补偿在替换节点上生效（strength 0.90）",
          r_min.graph["n_ref"]["inputs"]["strength_model"] == 0.90,
          str(r_min.graph["n_ref"]["inputs"]))
    check("C6 替换节点不含源规则的端口（无 IPAdapter 端口）",
          not ({"image", "weight", "weight_type", "ipadapter"}
               & set(r_min.graph["n_ref"]["inputs"])),
          str(sorted(r_min.graph["n_ref"]["inputs"])))
    check("C6 完整环境下 n_ref 保持为参考图注入节点（对照）",
          r_noinj.graph["n_ref"]["class_type"] == "IPAdapterAdvanced",
          str(r_noinj.graph["n_ref"]["class_type"]))

    # ---------- C7 阈值随降级自动调整 ----------
    check("C7 一致性约束阈值被自动放宽",
          any(k.startswith("cr_face") for k in r_min.threshold_adjustments),
          str(r_min.threshold_adjustments))
    thr_notices = [n for n in r_min.notices if n.kind == NoticeKind.THRESHOLD_ADJUSTED]
    check("C7 产生阈值调整通知且关联约束", len(thr_notices) >= 1,
          str([n.as_dict() for n in thr_notices]))
    if thr_notices:
        t = thr_notices[0]
        check("C7 阈值通知含 delta 与可回滚标记",
              t.threshold_adjusted is not None and t.reversible,
              str(t.threshold_adjusted))
    check("C7 完整环境阈值不被调整（对照）", r_full.threshold_adjustments == {})

    # ---------- C8 漂移环境：端口改名 + 枚举缩减 + 文件缺失 ----------
    comp_drift = ComfyCompiler(drifted)
    ir_old_lora = shot_ir()
    ir_old_lora["bindings"]["CHAR-0007"]["loraFile"] = "linwan_v1.safetensors"

    def drift_reqs(comp, ir):
        # `_file_requirements` 现在返回 {(端口): ((来源kind, 文件名), ...)}，
        # 因为 FILE_MISSING 漂移本身不带规则信息，必须回查来源 kind 才能
        # 区分"必需规则缺文件"（阻断）与"可选规则缺文件"（降级信号）。
        # `rules.requirements()` 只关心文件名，故在此解包。
        return comp.rules.requirements(
            ir, matrix=comp.matrix,
            files_by_port={p: tuple(v for _, v in pairs)
                           for p, pairs in comp._file_requirements(ir).items()},
        )

    drifts = drifted.detect_drift(drift_reqs(comp_drift, shot_ir()))
    kinds = {d.kind for d in drifts}
    check("C8 检出文件缺失（LoRA 不在目标环境）",
          DriftKind.FILE_MISSING.value in kinds,
          str([d.as_dict() for d in drifts if d.kind == DriftKind.FILE_MISSING.value]))
    file_drift = next((d for d in drifts if d.kind == DriftKind.FILE_MISSING.value), None)
    if file_drift:
        check("C8 文件缺失为 blocker 且给出修复建议",
              file_drift.severity == Severity.BLOCKER.value and "models" in file_drift.remedy,
              f"{file_drift.severity} / {file_drift.remedy}")

    enum_drift = [d for d in drifts if d.kind == DriftKind.ENUM_VALUE_MISSING.value]
    check("C8 检出枚举取值缺失（sampler dpmpp_2m 被移除）",
          any("dpmpp_2m" in str(d.expected) for d in enum_drift),
          str([d.as_dict() for d in enum_drift]))

    check("C8 端口改名被报出（latent_image -> latent）",
          any(d.kind == DriftKind.PORT_MISSING.value and d.port == "latent_image"
              for d in drifted.detect_drift(drift_reqs(comp_drift, ir_old_lora))),
          str([d.as_dict() for d in drifted.detect_drift(drift_reqs(comp_drift, ir_old_lora))]))

    # ---- 可选规则缺文件 vs 必需规则缺文件的**行为分叉** ----------------
    #
    # 这是本项目与"缺什么就报错"的朴素做法最重要的区别，所以单独成为一组断言。
    #
    # 背景：本机**无法训练角色 LoRA**（Z-Image fp32 底模需 22.93 GB 内存
    # > 本机 15.8 GB）。若"缺 LoRA 文件"就阻断编译，平台在这台机器上
    # 永远无法出片 —— 哪怕参考图路径完全可用。因此：
    #
    #   可选规则缺文件 -> 降级信号：继续编译 + 发 DegradationNotice
    #   必需规则缺文件 -> 硬阻断：抛 MISSING_REQUIRED_NODE
    r_opt = comp_drift.compile(shot_ir())
    opt_kinds = {n.kind for n in r_opt.notices}
    check("C8 可选规则缺文件**不阻断**编译（改走降级链）",
          len(r_opt.graph) >= 7,
          f"nodes={len(r_opt.graph)}")
    check("C8b 且发出 SKIPPED_OPTIONAL 降级通知（不是静默忽略）",
          NoticeKind.SKIPPED_OPTIONAL in opt_kinds
          or NoticeKind.DEGRADED in opt_kinds,
          f"notices={sorted(opt_kinds)}")
    check("C8b 降级通知携带影响评估与修复建议",
          all(n.impact and n.ui_hint for n in r_opt.notices),
          "; ".join(f"{n.kind}:{n.impact[:34]}" for n in r_opt.notices))

    # ---- **端到端验证抓出的两个缺陷**的回归锁定 -------------------------
    #
    # 这两个缺陷都是"编译通过、提交必失败"的静默错误，只有真实 ComfyUI 能发现。
    # 因此必须在本地就锁死，否则会再次退回去。
    #
    # 缺陷 1：必需**连线**端口被留空（`CLIPTextEncode.clip`）
    #   -> 提交时 ComfyUI 报 `required_input_missing`
    # 缺陷 2：明知文件不存在仍生成引用它的节点
    #   -> 提交时 ComfyUI 报 `value_not_in_list`
    graph_opt = r_opt.graph
    prompt_nodes = [nid for nid, g in graph_opt.items()
                    if g["class_type"] == "CLIPTextEncode"]
    check("C8e【缺陷1】CLIPTextEncode 的必需端口 clip 已被自动接线",
          prompt_nodes and all(
              isinstance(graph_opt[nid]["inputs"].get("clip"), list)
              for nid in prompt_nodes),
          "; ".join(f"{nid}.clip={graph_opt[nid]['inputs'].get('clip')}"
                    for nid in prompt_nodes))

    check("C8f【缺陷2】引用不存在 LoRA 文件的节点已被移除（而非照常生成）",
          not any("lora_name" in g["inputs"] for g in graph_opt.values()),
          str([nid for nid, g in graph_opt.items()
               if "lora_name" in g["inputs"]]))

    # 降级链必须**重接下游**：n_ks.model 原本指向被移除的 n_lora，现在应指向模型源
    ks = next((g for g in graph_opt.values()
               if g["class_type"] == "KSampler"), None)
    check("C8g【缺陷2】被移除节点的下游已重接到最近的存活祖先",
          ks is not None and isinstance(ks["inputs"].get("model"), list)
          and ks["inputs"]["model"][0] in graph_opt
          and "lora_name" not in graph_opt[ks["inputs"]["model"][0]]["inputs"],
          f"n_ks.model -> {ks['inputs'].get('model') if ks else None}")

    # 产出完整性终检本身必须在场（它是这两类缺陷的通用防线）
    check("C8h 产出完整性终检已建立（INCOMPLETE_GRAPH 错误码存在）",
          hasattr(CompileErrorCode, "INCOMPLETE_GRAPH")
          and CompileErrorCode.INCOMPLETE_GRAPH == "INCOMPLETE_GRAPH",
          CompileErrorCode.INCOMPLETE_GRAPH)

    # 必需规则缺文件仍必须硬阻断 —— 用它守住"不该放过的绝不放"这一侧
    ir_bad_ckpt = shot_ir()
    for _n in ir_bad_ckpt["nodes"]:
        if _n["kind"] == "model.checkpoint":
            _n["params"]["ckpt_name"] = "根本不存在的底模.safetensors"
    expect_compile_error("C8c 必需规则缺文件仍硬阻断（另一端不能被放松）",
                         CompileErrorCode.MISSING_REQUIRED_NODE,
                         comp_drift.compile, ir_bad_ckpt)

    r_old = comp_drift.compile(ir_old_lora)
    check("C8d 换用其他 LoRA 文件仍可编译（降级路径不被破坏）",
          len(r_old.graph) >= 7, f"nodes={len(r_old.graph)}")

    # ---------- C9 环境基线对比 ----------
    base_v1 = probe_from_object_info(env_full())
    base_v2 = probe_from_object_info(env_minimal())
    env_diff = base_v2.detect_drift({}, baseline=base_v1)
    removed = [d for d in env_diff if d.kind == DriftKind.NODE_MISSING.value]
    check("C9 基线对比检出被卸载的节点包",
          len(removed) == 6, f"检出 {len(removed)} 个: {[d.class_type for d in removed]}")

    port_renamed = env_full()
    port_renamed["KSampler"]["input"]["required"].pop("latent_image")
    port_renamed["KSampler"]["input"]["required"]["latent"] = _port("LATENT")
    renamed_drifts = probe_from_object_info(port_renamed).detect_drift({}, baseline=base_v1)
    check("C9 基线对比检出端口改名",
          any(d.kind == DriftKind.PORT_MISSING.value and d.port == "latent_image"
              for d in renamed_drifts),
          str([d.as_dict() for d in renamed_drifts]))
    check("C9 基线对比检出新增端口",
          any(d.kind == DriftKind.PORT_ADDED.value and d.port == "latent"
              for d in renamed_drifts),
          "")

    out_changed = env_full()
    out_changed["LoraLoader"]["output"] = ["MODEL"]
    oc = probe_from_object_info(out_changed).detect_drift({}, baseline=base_v1)
    check("C9 输出数量变化被判为 blocker（outputIndex 会错位）",
          any(d.kind == DriftKind.OUTPUT_COUNT_CHANGED.value
              and d.severity == Severity.BLOCKER.value for d in oc),
          str([d.as_dict() for d in oc if d.kind == DriftKind.OUTPUT_COUNT_CHANGED.value]))

    # ---------- C10 自动注入模型源 ----------
    ir_no_ckpt = shot_ir()
    ir_no_ckpt["nodes"] = [n for n in ir_no_ckpt["nodes"]
                           if n["kind"] != "model.checkpoint"]
    for n in ir_no_ckpt["nodes"]:
        n["inputs"] = {k: v for k, v in (n.get("inputs") or {}).items()
                       if not (isinstance(v, dict) and v.get("ref") == "n_ckpt")}
    ir_no_ckpt["edges"] = [e for e in ir_no_ckpt["edges"]
                           if e["from"]["node"] != "n_ckpt"]

    comp_auto = ComfyCompiler(full)
    r_auto = comp_auto.compile(ir_no_ckpt)
    check("C10 缺失模型源时自动注入",
          ComfyCompiler.MODEL_SOURCE_ID in r_auto.graph
          and r_auto.graph[ComfyCompiler.MODEL_SOURCE_ID]["class_type"]
          == "CheckpointLoaderSimple",
          str(list(r_auto.graph)))
    check("C10 自动注入产生 INFO 通知",
          any(n.kind == NoticeKind.AUTO_INJECTED for n in r_auto.notices),
          str([n.as_dict() for n in r_auto.notices]))
    check("C10 自动注入的模型源被下游引用",
          r_auto.graph["n_lora"]["inputs"]["model"] == [ComfyCompiler.MODEL_SOURCE_ID, 0],
          str(r_auto.graph["n_lora"]["inputs"]))

    # ---------- C11 结构性错误 ----------
    bad_kind = shot_ir()
    bad_kind["nodes"].append({"id": "n_unknown", "kind": "totally.unknown.kind"})
    expect_compile_error("C11 未登记 kind 报 UNKNOWN_KIND",
                         CompileErrorCode.UNKNOWN_KIND, comp_full.compile, bad_kind)

    dangling = shot_ir()
    for n in dangling["nodes"]:
        if n["id"] == "n_ks":
            n["inputs"]["positive"] = {"ref": "n_ghost", "port": "CONDITIONING"}
    expect_compile_error("C11 悬空引用报 DANGLING_REF",
                         CompileErrorCode.DANGLING_REF, comp_full.compile, dangling)

    cyclic = copy.deepcopy(shot_ir())
    cyclic["edges"].append({"id": "e_cyc", "type": "data",
                            "from": {"node": "n_ks", "port": "LATENT"},
                            "to": {"node": "n_latent", "port": "LATENT"}})
    expect_compile_error("C11 环报 CYCLE", CompileErrorCode.CYCLE,
                         comp_full.compile, cyclic)

    missing_binding = copy.deepcopy(shot_ir())
    missing_binding["bindings"].pop("CHAR-0007")
    expect_compile_error("C11 绑定缺失报 MISSING_BINDING",
                         CompileErrorCode.MISSING_BINDING, comp_full.compile, missing_binding)

    no_ksampler = probe_from_object_info({k: v for k, v in env_full().items()
                                          if k != "KSampler"})
    expect_compile_error("C11 必需节点缺失阻断编译",
                         CompileErrorCode.MISSING_REQUIRED_NODE,
                         ComfyCompiler(no_ksampler).compile, shot_ir())

    # ---------- C12 可选能力缺失 -> 跳过而非失败 ----------
    ir_with_tts = shot_ir()
    ir_with_tts["nodes"].append({
        "id": "n_tts", "kind": "audio.tts",
        "params": {"text": "林晚：谁在那里？", "voice": "linwan_01"},
    })
    r_with_tts_full = comp_full.compile(ir_with_tts)
    check("C12 完整环境实现可选 TTS",
          "n_tts" in r_with_tts_full.graph, str(list(r_with_tts_full.graph)))
    r_with_tts_min = comp_min.compile(ir_with_tts)
    check("C12 最小环境跳过可选 TTS 而整体成功",
          "n_tts" not in r_with_tts_min.graph and len(r_with_tts_min.graph) >= 8,
          str(list(r_with_tts_min.graph)))
    check("C12 跳过可选能力产生 INFO 通知",
          any(n.kind == NoticeKind.SKIPPED_OPTIONAL for n in r_with_tts_min.notices),
          str([n.as_dict() for n in r_with_tts_min.notices
               if n.kind == NoticeKind.SKIPPED_OPTIONAL]))

    # ---------- C13 三级缓存键联动 ----------
    k_t1_full = cache_key(r_full.ir_hash, r_full.catalog_hash, r_full.rule_version)
    k_t1_min = cache_key(r_min.ir_hash, r_min.catalog_hash, r_min.rule_version)
    check("C13 同一 IR 在不同环境得到不同 Tier-1 键",
          k_t1_full != k_t1_min,
          "环境哈希参与缓存键 => 换环境自动失效")
    check("C13 同一 IR 跨环境共享 Tier-2 键",
          cache_key(r_full.ir_hash) == cache_key(r_min.ir_hash),
          "Tier-2 允许跨环境语义复用（需重校验质量断言）")
    check("C13 编译结果摘要完整",
          set(r_full.summary()) >= {"graphHash", "irHash", "catalogHash",
                                    "ruleVersion", "nodeCount", "blockerCount"},
          str(sorted(r_full.summary())))

    # ---------- C14 规则表可扩展 ----------
    #
    # 测的是"新增 kind 只需追加一条规则、不改编译器"。
    #
    # 选 `EmptyLatentImage` 而非 `KSampler` 作目标：后者需要
    # positive / negative / latent_image 三个**连线型**必需输入，
    # 而本用例只追加一个孤立节点、不建上游 —— 那会触发产出完整性终检。
    # 完整性终检是对的（孤立的重节点确实编不出可执行的图），
    # 所以这里换一个"无连线型必需端口"的节点，让用例聚焦在扩展机制本身。
    extended = CompileRuleTable(DEFAULT_RULES + (
        CompileRule(kind="custom.my_node", class_types=("EmptyLatentImage",),
                    param_map={"width": "width", "height": "height"},
                    defaults={"width": 512, "height": 512},
                    output_ports=("LATENT",), required=True),
    ))
    ir_custom = shot_ir()
    ir_custom["nodes"].append({"id": "n_custom", "kind": "custom.my_node"})
    r_custom = ComfyCompiler(full, extended).compile(ir_custom)
    check("C14 新增 kind 只需追加规则、不改编译器",
          "n_custom" in r_custom.graph
          and r_custom.graph["n_custom"]["_meta"].get("irKind") == "custom.my_node",
          str(r_custom.graph.get("n_custom", {}).get("_meta")))

    # ---------- C15 确定性 ----------
    hashes = {comp_full.compile(shot_ir()).graph_hash for _ in range(5)}
    check("C15 多次编译图哈希完全一致", len(hashes) == 1, str(hashes))
    check("C15 图哈希为 sha256 形式",
          r_full.graph_hash.startswith("sha256:") and len(r_full.graph_hash) == 71,
          r_full.graph_hash)

    # ---------- C16 端口类型强制：文件路径必须经 LoadImage ----------
    no_loader = probe_from_object_info({k: v for k, v in env_full().items()
                                        if k != "LoadImage"})
    expect_compile_error(
        "C16 缺 LoadImage 且需转换文件路径 -> 阻断编译（而非产出类型错误的图）",
        CompileErrorCode.MISSING_REQUIRED_NODE,
        ComfyCompiler(no_loader).compile, shot_ir(),
    )
    check("C16 需求声明在编译前即报出该端口需要 LoadImage",
          ("IPAdapterAdvanced", "image") in comp_full.compile(shot_ir()).required_loaders,
          str(comp_full.compile(shot_ir()).required_loaders))

    # 同一路径被多处引用时只注入一个 LoadImage（去重）
    ir_two_refs = shot_ir()
    ir_two_refs["nodes"].append({
        "id": "n_ref2", "kind": "identity.reference",
        "inputs": {"image": {"binding": "CHAR-0007.anchorSet.front"}},
        "params": {"weight": 0.5},
        "semantic": {"characterRef": "CHAR-0007"},
    })
    r_two = comp_full.compile(ir_two_refs)
    loaders = [nid for nid, g in r_two.graph.items()
               if g["class_type"] == "LoadImage"]
    check("C16 同一路径多处引用只注入一个 LoadImage（去重）",
          len(loaders) == 1, str(loaders))

    # 不同路径 -> 各自的 LoadImage，且 id 由路径确定性派生
    ir_two_paths = shot_ir()
    ir_two_paths["nodes"].append({
        "id": "n_ref3", "kind": "identity.reference",
        "inputs": {"image": {"binding": "CHAR-0007.anchorSet.side"}},
        "params": {"weight": 0.5},
        "semantic": {"characterRef": "CHAR-0007"},
    })
    ir_two_paths["bindings"]["CHAR-0007"]["anchorSet"]["side"] = "char/CHAR-0007/anchor_side_v3.png"
    r_paths = comp_full.compile(ir_two_paths)
    loaders2 = sorted(nid for nid, g in r_paths.graph.items()
                      if g["class_type"] == "LoadImage")
    check("C16 不同路径生成各自的 LoadImage", len(loaders2) == 2, str(loaders2))
    check("C16 LoadImage 节点 id 由路径派生、可复现",
          r_paths.graph_hash == comp_full.compile(ir_two_paths).graph_hash,
          "")

    # ---------- C17 确定性存疑节点：只对"确实未知"告警，且按类去重 ----------
    # 完整环境含 IPAdapterAdvanced（第三方节点，钩子未核实）——
    # 它**应当**被标为存疑，这是正确行为而非噪音。
    check("C17 第三方未核实节点被标为存疑",
          "n_ref" in r_full.determinism_concerns,
          str(r_full.determinism_concerns))
    det_notices = [n for n in r_full.notices
                   if n.kind == NoticeKind.CACHE_KEY_INCOMPLETE]
    check("C17 存疑告警按 class_type 去重（每类一条，不逐节点）",
          len(det_notices) == 1, f"{len(det_notices)} 条")
    check("C17 官方确定性节点（KSampler/CLIPTextEncode/…）不产生告警",
          all(n.frm.startswith("IPAdapterAdvanced") for n in det_notices),
          str([n.frm for n in det_notices]))
    check("C17 存疑告警明确说明是第三方节点且给出核实方法",
          det_notices and "第三方" in det_notices[0].ui_hint
          and "IS_CHANGED" in det_notices[0].ui_hint,
          det_notices[0].ui_hint if det_notices else "")
    check("C17 存疑告警声明影响范围（涉及节点数）",
          det_notices and "涉及" in det_notices[0].to,
          det_notices[0].to if det_notices else "")

    # 全内置节点（无第三方）的图不应有任何存疑告警
    ir_builtin_only = shot_ir()
    ir_builtin_only["nodes"] = [n for n in ir_builtin_only["nodes"]
                                if n["kind"] != "identity.reference"]
    ir_builtin_only["edges"] = [e for e in ir_builtin_only["edges"]
                                if e["from"]["node"] != "n_ref"
                                and e["to"]["node"] != "n_ref"]
    r_builtin = comp_full.compile(ir_builtin_only)
    check("C17 纯内置节点的图零存疑告警（避免告警疲劳）",
          r_builtin.determinism_concerns == {}
          and not any(n.kind == NoticeKind.CACHE_KEY_INCOMPLETE
                      for n in r_builtin.notices),
          f"concerns={r_builtin.determinism_concerns}")

    # 未收录的自定义节点 -> 存疑，且可用声明消除
    env_custom = env_full()
    env_custom["MyCustomEnhance"] = {
        "input": {"required": {"images": _port("IMAGE")}},
        "output": ["IMAGE"], "category": "custom", "output_node": False,
    }
    ir_custom_det = shot_ir()
    ir_custom_det["nodes"].append({"id": "n_enh", "kind": "custom.my_node"})
    custom_rules = CompileRuleTable(DEFAULT_RULES + (
        CompileRule(kind="custom.my_node", class_types=("MyCustomEnhance",),
                    port_map={"images": "images"}, output_ports=("IMAGE",),
                    required=False),
    ))
    r_custom_det = ComfyCompiler(probe_from_object_info(env_custom), custom_rules) \
        .compile(ir_custom_det)
    check("C17 自定义节点被判为存疑并登记",
          "n_enh" in r_custom_det.determinism_concerns
          and r_custom_det.determinism_concerns["n_enh"][1]
          == DeterminismClass.UNKNOWN.value,
          str(r_custom_det.determinism_concerns))
    r_declared = ComfyCompiler(
        probe_from_object_info(env_custom, declared_hooks={"MyCustomEnhance": False}),
        custom_rules,
    ).compile(ir_custom_det)
    check("C17 显式声明可消除存疑告警（无需改代码）",
          "n_enh" not in r_declared.determinism_concerns,
          str(r_declared.determinism_concerns))

    # ---------- C18 基线漂移：钩子变化可检出 ----------
    hook_drift = probe_from_object_info(env_full(),
                                        declared_hooks={"LoadImage": False}
                                        ).detect_drift({}, baseline=full)
    check("C18 基线对比检出失效钩子变化（ABI 变化）",
          any(d.kind == DriftKind.FINGERPRINT_HOOK_CHANGED.value for d in hook_drift),
          str([d.as_dict() for d in hook_drift
               if d.kind == DriftKind.FINGERPRINT_HOOK_CHANGED.value]))

    # ---------- 汇总 ----------
    print()
    # ---- Z-Image ControlNet（空间控制）----
    _section_zimage_controlnet()
    print()
    _section_zimage_source()
    print()
    _section_optional_ports()
    print()
    _section_blur_rule()
    print()
    _section_optional_lora_degrade()
    print()
    _section_self_check()
    print()
    npass = sum(1 for _, ok, _ in _results if ok)
    for name, ok, detail in _results:
        print(f" [{'v' if ok else 'X'}] {name}")
        if not ok or detail:
            print(f"       {detail}")
    print("=" * 78)
    print(f"结果：{npass}/{len(_results)} 通过")

    if npass == len(_results):
        print()
        print("--- 完整环境编译出的执行图（节选）---")
        for nid in ("n_ckpt", "n_lora", "n_ks", "n_prompt"):
            if nid in r_full.graph:
                print(f"   {nid}: {json.dumps(r_full.graph[nid], ensure_ascii=False)[:150]}")
        print()
        print("--- 最小环境的降级通知 ---")
        for n in r_min.notices:
            d = n.as_dict()
            print(f"   [{d['kind']}] {d['from']} -> {d['to']}")
            print(f"       原因: {d['reason']}")
            print(f"       影响: {d['impact']}")
            print(f"       建议: {d['uiHint']}")
        print()
        print(f"   环境哈希 (完整): {full.catalog_hash()[:34]}...")
        print(f"   环境哈希 (最小): {minimal.catalog_hash()[:34]}...")
        print(f"   Tier-1 缓存键(完整): {k_t1_full}")
        print(f"   Tier-1 缓存键(最小): {k_t1_min}")
    return 0 if npass == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())