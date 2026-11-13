"""IR 的第二个/第三个/第四个出口：HTTP 生图 · TTS · 视频（2026-11-11）。

## 为什么需要"出口"

`ComfyCompiler` 把 IR 编译成 **ComfyUI 节点图**；而 HTTP 后端过去是
`render_body_template(模板, IR 字段)` —— 拿字段填字符串，**没有编译**：
没有图、没有校验、没有内容哈希。于是"统一 IR"只在 ComfyUI 这条路上成立。

本模块把"IR -> 目标语言"统一成编译器族：

| 出口 | 目标 | 用途 |
|---|---|---|
| `HttpImageCompiler` | 生图 API 的请求体 | 本地 local-gen / 云端 API |
| `TtsCompiler`        | 语音合成请求 | breeze / 云端 TTS |
| `VideoCompiler`      | 视频（i2v/t2v）请求 | H3 / 云端视频 |

**共同纪律**：
1 IR 里**做不到的**不许静默丢 —— 写进 `notices`（例如后端不支持控制图）
2 编译产物带 `contentHash`，进证据链（模板直填没有这个）
3 缺关键字段 -> `ok=False` + 明确原因（不是抛异常）

## 不做什么

不在这里提交请求、也不读网络：**只做翻译**。
提交仍由通道层（`runtime/channels/*`）负责 —— 职责不混。

## 接线现状（2026-11-12 核实，别把"有编译器"当成"已经走编译器"）

| 出口 | 链路里用上了吗 | 说明 |
|---|---|---|
| 图片 | ✅ 用上了 | `EpisodeRunner._values()` 走 `compile_ir(kind="image")`；失败退回旧取值并打印原因 |
| 音频 | ❌ 还没接 | 本地 TTS 的字段是固定的（`/v1/audio/speech`），`tts_breeze.synth` 直调即可；<br>接进来的价值要等**云端 TTS**（不同 `body_template`）才体现 |
| 视频 | ❌ 还没接 | `h3_video.video_values()` 是**按后端模板占位符**构造的；<br>硬塞一份 IR 只为过编译器属于过度设计 |

**所以"三个出口"目前的准确说法是**：
接口层（IR -> 请求体）三个都实现并有测试；**链路里真正走编译的只有图片那条**。
要接音频/视频，接线点分别在 `voice_track.build()` 与 `h3_video.video_values()`。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from little_crew.models.template import TemplateError, render_body_template


# ---------------------------------------------------------------------------
# 公共：从 IR 里取参数
# ---------------------------------------------------------------------------

def _nodes(ir: Mapping[str, Any]) -> list[dict]:
    return [dict(n) for n in (ir.get("nodes") or ()) if isinstance(n, Mapping)]


def _node_params(ir: Mapping[str, Any], kind: str) -> dict:
    """取某个 kind 的**第一个**节点的 params（没有就空 dict）。"""
    for n in _nodes(ir):
        if str(n.get("kind")) == kind:
            return dict(n.get("params") or {})
    return {}


def _model_id(ir: Mapping[str, Any]) -> str:
    """从 IR 里取模型 id（meta.modelId / engine.modelId / nodes[*].engine.modelId）。"""
    for key in ("modelId", "model_id"):
        v = (ir.get("meta") or {}).get(key) if isinstance(ir.get("meta"), Mapping) else None
        if v:
            return str(v)
    for n in _nodes(ir):
        eng = n.get("engine")
        if isinstance(eng, Mapping):
            for key in ("modelId", "model_id"):
                if eng.get(key):
                    return str(eng[key])
    return ""


def _has_kind(ir: Mapping[str, Any], kind: str) -> bool:
    return any(str(n.get("kind")) == kind for n in _nodes(ir))


def _derive_seed(ir: Mapping[str, Any]) -> int:
    """没有显式 seed 时，从 IR 内容派生一个**稳定**的种子。

    为什么派生而不是随机：同一份 IR 重跑必须能复现同一张图；
    随机种子会让"内容哈希 -> 产物"这条链断掉。
    """
    payload = json.dumps(ir, ensure_ascii=False, sort_keys=True, default=str)
    return int(hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8], 16) % (2**31 - 1)


def _hash_obj(obj: Any) -> str:
    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class CompileOutput:
    """出口编译结果（与 ComfyUI 的 CompileResult 对齐的语义）。"""

    ok: bool
    kind: str = ""
    body: dict = field(default_factory=dict)
    content_hash: str = ""
    params: dict = field(default_factory=dict)
    notices: list[str] = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict:
        return {"ok": self.ok, "kind": self.kind, "body": self.body,
                "contentHash": self.content_hash, "params": self.params,
                "notices": list(self.notices), "error": self.error}


# ---------------------------------------------------------------------------
# 出口 1：HTTP 生图
# ---------------------------------------------------------------------------

class HttpImageCompiler:
    """IR -> 生图 API 请求体。"""

    kind = "image"

    #: 这些节点 HTTP 生图后端**通常不支持**，遇到要如实记 notice
    OPTIONAL_KINDS = {
        "composition.controlnet": "控制图",
        "composition.controlnet_basic": "控制图",
        "composition.zimage_controlnet": "控制图",
        "identity.ipadapter": "身份注入（IPAdapter）",
        "identity.lora": "LoRA",
        "identity.reference": "参考图",
    }

    def compile(self, ir: Mapping[str, Any], backend: Any = None,
                *, seed: int | None = None) -> CompileOutput:
        prompt = str(_node_params(ir, "prompt.text").get("text") or "").strip()
        if not prompt:
            return CompileOutput(False, self.kind,
                                 error="IR 里没有 prompt.text（生图必须要有提示词）")
        negative = str(_node_params(ir, "prompt.negative").get("text") or "")
        latent = _node_params(ir, "latent.empty")
        sampler = _node_params(ir, "sample.ksampler")
        width = int(latent.get("width") or 768)
        height = int(latent.get("height") or 1024)
        steps = int(sampler.get("steps") or 8)
        _seed = int(seed) if seed is not None else _derive_seed(ir)

        notices: list[str] = []
        for kind, label in self.OPTIONAL_KINDS.items():
            if _has_kind(ir, kind):
                notices.append(f"IR 含{label}节点（{kind}），该出口不保证支持 —— "
                               "已忽略，降级由上层决定")

        values = {"prompt": prompt, "negative_prompt": negative,
                  "width": width, "height": height,
                  # `size` / `model`：OpenAI 兼容的图像接口（含自定义供应商）
                  # 用 `"size": "1024x1024"` 与模型名，而不是宽高两个数字。
                  # 缺这两个变量时模板填充会直接报错（实测踩到）。
                  "size": f"{width}x{height}",
                  "model": str(_model_id(ir) or ""),
                  "steps": steps,
                  "seed": _seed, "cfg": float(sampler.get("cfg") or 4.0),
                  "sampler": str(sampler.get("sampler") or "euler"),
                  "scheduler": str(sampler.get("scheduler") or "simple")}
        # 没有 backend（比如只取参数供通道填模板）时，**用参数本身当请求体**
        body, err = _fill(backend, values, default_body=values)
        if err:
            return CompileOutput(False, self.kind, error=err, notices=notices)
        return CompileOutput(True, self.kind, body=body,
                             content_hash=_hash_obj(body), params=values,
                             notices=notices)


# ---------------------------------------------------------------------------
# 出口 2：TTS
# ---------------------------------------------------------------------------

class TtsCompiler:
    """IR -> 语音合成请求体（breeze /v1/audio/speech 的字段）。"""

    kind = "tts"

    def compile(self, ir: Mapping[str, Any], backend: Any = None,
                *, seed: int | None = None) -> CompileOutput:
        p = _node_params(ir, "audio.tts")
        text = str(p.get("text") or "").strip()
        if not text:
            return CompileOutput(False, self.kind,
                                 error="IR 里没有 audio.tts.text（配音必须要有台词）")
        notices: list[str] = []
        if not p.get("voice"):
            notices.append("没指定音色（voice）：将用后端默认音色，角色一致性无法保证")
        values = {"input": text, "voice": str(p.get("voice") or ""),
                  "instruction": str(p.get("instruction") or ""),
                  "response_format": "wav",
                  "seed": int(seed) if seed is not None else _derive_seed(ir)}
        body, err = _fill(backend, values, default_body=values)
        if err:
            return CompileOutput(False, self.kind, error=err, notices=notices)
        return CompileOutput(True, self.kind, body=body,
                             content_hash=_hash_obj(body), params=values,
                             notices=notices)


# ---------------------------------------------------------------------------
# 出口 3：视频（i2v / t2v）
# ---------------------------------------------------------------------------

class VideoCompiler:
    """IR -> 视频生成请求体。**i2v 必须有首帧**；没有就退 t2v 并说明。"""

    kind = "video"

    def compile(self, ir: Mapping[str, Any], backend: Any = None,
                *, seed: int | None = None) -> CompileOutput:
        prompt = str(_node_params(ir, "prompt.text").get("text") or "").strip()
        first = (_node_params(ir, "latent.from_image").get("image")
                 or _node_params(ir, "image.file").get("path")
                 or _node_params(ir, "video.i2v").get("first_frame") or "")
        prov = ir.get("provenance") or {}
        dur = float(prov.get("durationSec") or 0) or 5.0
        notices: list[str] = []
        mode = "i2v"
        if not str(first).strip():
            mode = "t2v"
            notices.append("IR 里没有首帧（latent.from_image/image.file）—— "
                           "退化为 t2v；i2v 的镜头运动控制不可用")
        if not prompt:
            return CompileOutput(False, self.kind,
                                 error="IR 里没有 prompt.text（视频也要提示词）")
        values = {"prompt": prompt, "first_frame": str(first),
                  "duration": dur, "fps": int(prov.get("fps") or 8),
                  "mode": mode,
                  "seed": int(seed) if seed is not None else _derive_seed(ir)}
        body, err = _fill(backend, values, default_body=values)
        if err:
            return CompileOutput(False, self.kind, error=err, notices=notices)
        return CompileOutput(True, self.kind, body=body,
                             content_hash=_hash_obj(body), params=values,
                             notices=notices)


# ---------------------------------------------------------------------------
# 公共：填模板
# ---------------------------------------------------------------------------

def _fill(backend: Any, values: Mapping[str, Any],
          default_body: Mapping[str, Any] | None = None) -> tuple[dict, str]:
    """用后端的 `body_template` 填出请求体；没有模板就用 `default_body`。

    模板里的占位符由 `render_body_template` 负责（引号感知 + JSON 转义）。
    模板缺我们给的变量不算错（模板只取它要的）；但**模板要求的变量我们没给**
    就是错 —— 那会静默变成空值或字面占位符。
    """
    tpl = str(getattr(backend, "body_template", "") or "").strip()
    if not tpl:
        if default_body is None:
            return {}, "后端没有 body_template，也没有默认请求体"
        return dict(default_body), ""
    missing = [k for k in _template_keys(tpl) if k not in values]
    if missing:
        return {}, f"模板需要这些变量但 IR 没给：{sorted(missing)}"
    try:
        return render_body_template(tpl, dict(values)), ""
    except TemplateError as e:
        return {}, f"模板渲染失败：{e}"


def _template_keys(tpl: str) -> set[str]:
    """粗取模板里的 `{name}` 占位符（够用：我们自己的模板都长这样）。"""
    import re

    return {m.group(1) for m in re.finditer(r"\{([a-z_][a-z0-9_]*)\}", tpl)}


# ---------------------------------------------------------------------------
# 分派
# ---------------------------------------------------------------------------

_BY_KIND = {"image": HttpImageCompiler, "tts": TtsCompiler, "video": VideoCompiler}


def compile_ir(ir: Mapping[str, Any], backend: Any = None,
               *, kind: str = "image", seed: int | None = None) -> CompileOutput:
    """按出口类型编译。未知类型 -> ok=False（不猜）。"""
    cls = _BY_KIND.get(str(kind))
    if cls is None:
        return CompileOutput(False, str(kind),
                             error=f"未知出口类型 {kind!r}；"
                                   f"支持 {sorted(_BY_KIND)}")
    return cls().compile(ir, backend, seed=seed)
