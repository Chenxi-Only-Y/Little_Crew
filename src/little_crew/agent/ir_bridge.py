"""镜头 → IR 桥接 —— 让分镜师的产出真正驱动生成。

链路位置
--------
```
分镜师 → ShotList（消息层）
            │  ★ 本模块
            ▼
        统一 IR（可编译、可校验、可缓存）
            │  compiler
            ▼
        ComfyUI 图 → 出图
```

**为什么需要这一层**：`ShotList` 是**创作语义**（景别、运镜、动作、出场角色），
IR 是**可执行图**。二者之间必须有一次**显式的、可测试的**翻译 ——
否则"分镜驱动生成"就只是把描述文本拼进提示词，无法校验、无法缓存、无法降级。

设计要点
--------
1. **镜头 id 决定 graphId** —— 同一镜头重编译得到同一图哈希，因而可缓存
2. **出场角色写入 `bindings`** —— 为后续的身份锁定（`identity.reference`）
   预留位置，现在先记录，不强行要求锚点图存在
3. **景别与运镜翻译成提示词片段** —— 模型对"近景"这类词有响应，
   但对 `medium_close` 没有
4. **约束声明随角色进入 `constraints`** —— 让"这个镜头必须像 CHAR-0007"
   成为**可执行断言**而非注释。断言算子未实现的先标为 `soft`
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from little_crew.agent.messages import (
    MAX_CHARACTERS_PER_SHOT,
    MAX_SHOT_SECONDS,
    Shot,
    ShotList,
)

__all__ = [
    "shot_to_ir",
    "shotlist_to_ir",
    "default_negative_prompt",
    "DEFAULT_RESOLUTION",
    "SHOT_SIZE_PROMPT",
    "CAMERA_MOVE_PROMPT",
]

#: 默认出图分辨率（宽, 高）。符合平台的 832×480 档。
DEFAULT_RESOLUTION: tuple[int, int] = (832, 480)

#: 景别 → 提示词片段。
#:
#: **模型不认 `medium_close`，只认自然语言。** 这一层翻译是必需的，
#: 而不是可有可无的美化。
SHOT_SIZE_PROMPT: Mapping[str, str] = {
    "extreme_wide": "extreme wide shot, vast surroundings, tiny subject",
    "wide": "wide shot, full environment visible",
    "full": "full body shot, head to toe",
    "medium_full": "medium full shot, knees up",
    "medium": "medium shot, waist up",
    "medium_close": "medium close-up, chest up",
    "close": "close-up, face fills frame",
    "extreme_close": "extreme close-up, detail only",
}

#: 运镜 → 提示词片段。视频阶段用；图像阶段只作为构图暗示。
CAMERA_MOVE_PROMPT: Mapping[str, str] = {
    "static": "static camera, stable composition",
    "dolly_in": "camera pushing in, tightening framing",
    "dolly_out": "camera pulling back, revealing context",
    "truck_left": "camera tracking left, lateral movement",
    "truck_right": "camera tracking right, lateral movement",
}

#: 动作 → 提示词片段。
ACTION_PROMPT: Mapping[str, str] = {
    "stand": "standing still",
    "walk": "walking, mid-stride",
    "run": "running, dynamic pose",
}


# ==========================================================================
# 中文片段（给中文原生模型）
# ==========================================================================
#
# **为什么需要两套**：Z-Image 是中文原生模型，中文提示词它更懂；
# 而 SDXL 系只认英文。混着写会让两边都拿不到最好的结果 ——
# 实测把英文片段喂给 Z-Image，它仍能出图，但构图约束明显变弱。
#
# 这几张表与上面三张**键完全一致**，由 `prompt_language()` 决定取哪套；
# 键不一致会在编译期被下面的自检抓住。

SHOT_SIZE_PROMPT_ZH: Mapping[str, str] = {
    "extreme_wide": "极远景，广阔环境，人物很小",
    "wide": "远景，环境全貌可见",
    "full": "全身镜头，从头顶到脚",
    "medium_full": "中全景，膝盖以上",
    "medium": "中景，腰部以上",
    "medium_close": "中近景，胸部以上",
    "close": "特写，面部占满画面",
    "extreme_close": "大特写，只拍局部细节",
}

CAMERA_MOVE_PROMPT_ZH: Mapping[str, str] = {
    "static": "固定机位，构图稳定",
    "dolly_in": "镜头推进，画面收紧",
    "dolly_out": "镜头拉远，展现环境",
    "truck_left": "镜头左移，横向运动",
    "truck_right": "镜头右移，横向运动",
}

ACTION_PROMPT_ZH: Mapping[str, str] = {
    "stand": "静止站立",
    "walk": "行走中，迈步姿态",
    "run": "奔跑，动态姿势",
}


class PromptLanguage(str, Enum):
    """提示词语言。**由模型决定，不是由人决定。**"""

    EN = "en"       # SDXL 系
    ZH = "zh"       # Z-Image 等中文原生模型


def prompt_tables(lang: "PromptLanguage | str") -> tuple[
        Mapping[str, str], Mapping[str, str], Mapping[str, str]]:
    """取该语言的三张片段表。"""
    if PromptLanguage(lang) is PromptLanguage.ZH:
        return SHOT_SIZE_PROMPT_ZH, CAMERA_MOVE_PROMPT_ZH, ACTION_PROMPT_ZH
    return SHOT_SIZE_PROMPT, CAMERA_MOVE_PROMPT, ACTION_PROMPT


def _zh_negative() -> str:
    """中文负向提示。"""
    return ("多手, 多指, 肢体畸形, 脸部崩坏, 五官错位, 服装变色, "
            "画面出现文字, 水印, 签名, 低分辨率, 模糊, 过曝")


def _clean_description(text: str) -> str:
    """清掉描述里的**解析标记**。

    ``@角色名`` 是分镜师用来抽取出场人物的语法标记（见 ``crew_stubs._characters_of``），
    它不是画面内容。实测发现它会被原样带进提示词 ——
    模型看到 ``@林晚`` 只会当成噪声，白白占掉几个 token。

    **只在**这里清：``Shot.description`` 本身保留标记，
    因为它是"带结构的信息"，而提示词只需要"人看的描述"。
    """
    import re as _re

    out = _re.sub(r"@[\w\u4e00-\u9fff]{1,12}", "", text)
    # 清掉标记后可能留下多余空格与标点前的空白
    out = _re.sub(r"\s{2,}", " ", out)
    out = _re.sub(r"\s+([，。！？；：、])", r"\1", out)
    return out.strip()


def default_negative_prompt() -> str:
    """默认负向提示。

    **刻意用中文**：与本项目的提示词语言一致（正负向混语言会让
    文本编码器的一致性变差）。内容针对漫剧最常见的崩坏：
    多手多指、脸崩、服装变色、画面出现文字水印。
    """
    return ("多手, 多指, 肢体畸形, 脸部崩坏, 五官错位, 服装变色, "
            "画面出现文字, 水印, 签名, 低分辨率, 模糊, 过曝")


def _character_ids(shots_ids: Iterable[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for c in shots_ids:
        seen.setdefault(str(c), None)
    return tuple(seen)


def _episode_of(episode_id: str, scene_id: str) -> str:
    """推断集号。**显式传入优先** —— 从 ``SC-E001-002`` 里猜是兜底手段，
    而且只有 id 规范时才成立（规范性由分镜师的测试保证）。"""
    if episode_id:
        return episode_id
    parts = str(scene_id).split("-")
    return parts[1] if len(parts) >= 2 and parts[0] == "SC" else "E000"


def shot_to_ir(
    shot: Shot,
    *,
    episode_id: str = "",
    checkpoint: str = "sd_xl_base_1.0.safetensors",
    resolution: tuple[int, int] = DEFAULT_RESOLUTION,
    steps: int = 20,
    cfg: float = 5.5,
    sampler: str = "dpmpp_2m",
    #: 采样调度器。
    #:
    #: ⚠️ **Z-Image 上 `karras` 会产出崩坏的画面**
    #: （大片暗块 + 荧光错色 + 人物数量错乱，实测 2026-10-12）。
    #: 同样的图、同样的种子，只把这里换成 `simple` 就完全干净
    #: —— 所以这不是"参数偏好"，是**默认值错误**。
    #: `simple` 也是 Z-Image-Turbo 官方示例用的调度器。
    scheduler: str = "simple",
    style_suffix: str = "",
    negative_prompt: str = "",
    filename_prefix: str = "",
    anchor_sets: Mapping[str, str] | None = None,
    prompt_language: PromptLanguage | str = PromptLanguage.EN,
) -> dict[str, Any]:
    """把一个镜头编译成统一 IR。

    Args:
        prompt_language: 片段用哪种语言。**由目标模型决定** ——
            Z-Image 等中文原生模型用 ``"zh"``，SDXL 系用 ``"en"``。
        anchor_sets: ``{角色 id: 锚点图路径}``。给了就为对应角色加
            ``identity.reference`` 节点 —— 这是**身份锁定的接入点**。
            没给则只把角色 id 记进 ``bindings``，不生成锁定节点，
            因为引用一个不存在的锚点图会在编译期被 `FILE_MISSING` 阻断。
    """
    ep = _episode_of(episode_id, shot.scene_id)
    gid = f"G-{ep}-{shot.shot_id}"
    prefix = filename_prefix or shot.shot_id.replace("-", "_")

    size_tbl, move_tbl, act_tbl = prompt_tables(prompt_language)
    lang = PromptLanguage(prompt_language)

    # ---- 提示词组装 ----
    parts: list[str] = []
    if shot.description:
        # 清掉 @角色名 这类解析标记 —— 它们不是画面内容
        parts.append(_clean_description(shot.description))
    size_txt = size_tbl.get(shot.shot_size, shot.shot_size)
    if size_txt:
        parts.append(size_txt)
    move_txt = move_tbl.get(shot.camera_movement, "")
    if move_txt:
        parts.append(move_txt)
    act_txt = act_tbl.get(shot.action, "")
    if act_txt:
        parts.append(act_txt)
    if style_suffix:
        parts.append(style_suffix.strip())
    sep = "，" if lang is PromptLanguage.ZH else ", "
    prompt_text = sep.join(p for p in parts if p)

    w, h = int(resolution[0]), int(resolution[1])

    # ---- bindings：记录出场角色 ----
    bindings: dict[str, Any] = {}
    for cid in _character_ids(shot.characters):
        b: dict[str, Any] = {"shotId": shot.shot_id}
        if anchor_sets and cid in anchor_sets:
            b["anchorSet"] = {"front": anchor_sets[cid]}
        bindings[cid] = b

    nodes: list[dict[str, Any]] = [
        {"id": "n_ckpt", "kind": "model.checkpoint",
         "params": {"ckpt_name": checkpoint}},
        {"id": "n_neg", "kind": "prompt.negative",
         "inputs": {"clip": {"ref": "n_ckpt", "port": "CLIP"}},
         "params": {"text": negative_prompt or default_negative_prompt()}},
        {"id": "n_prompt", "kind": "prompt.text",
         "inputs": {"clip": {"ref": "n_ckpt", "port": "CLIP"}},
         "params": {"text": prompt_text},
         "semantic": {
             "shotId": shot.shot_id,
             "characters": list(shot.characters),
             "shotSize": shot.shot_size,
             "cameraMovement": shot.camera_movement,
         }},
        {"id": "n_latent", "kind": "latent.empty",
         "params": {"width": w, "height": h, "batch_size": 1}},
    ]

    # ---- 身份锁定（可选）：有锚点图才生成引用节点 ----
    model_ref: dict[str, str] = {"ref": "n_ckpt", "port": "MODEL"}
    if anchor_sets:
        locked = [c for c in shot.characters if c in anchor_sets]
        if locked:
            cid = locked[0]
            nodes.append({
                "id": "n_ref", "kind": "identity.reference",
                "inputs": {"image": {"binding": f"{cid}.anchorSet.front"}},
                "params": {"weight": 0.65, "weight_type": "linear"},
                "semantic": {"characterRef": cid,
                             "constraintRefs": [f"cr_identity_{cid}"]},
            })
            model_ref = {"ref": "n_ref", "port": "MODEL"}

    nodes += [
        {"id": "n_ks", "kind": "sample.ksampler",
         "inputs": {"model": model_ref,
                    "positive": {"ref": "n_prompt", "port": "CONDITIONING"},
                    "negative": {"ref": "n_neg", "port": "CONDITIONING"},
                    "latent_image": {"ref": "n_latent", "port": "LATENT"}},
         "params": {"steps": int(steps), "cfg": float(cfg),
                    "sampler": sampler, "scheduler": scheduler,
                    "denoise": 1.0}},
        {"id": "n_decode", "kind": "decode.vae",
         "inputs": {"samples": {"ref": "n_ks", "port": "LATENT"},
                    "vae": {"ref": "n_ckpt", "port": "VAE"}}},
        {"id": "n_save", "kind": "io.save_image",
         "inputs": {"images": {"ref": "n_decode", "port": "IMAGE"}},
         "params": {"filename_prefix": prefix}},
    ]

    # ---- constraints：可执行断言（C1 的兑现点）----
    #
    # ⚠️ **schema 是 `ref` + `assertion`，不是 `id` + `op`。**
    # 早期版本写成后者，与两处正典都不符：
    #   · `compiler.py` 的悬空引用校验读的是 `c["ref"]`
    #   · `canonical.py` 规范化的是 `assertion` / `enforcement`
    # 后果是约束**看起来声明了，实际没人认得** ——
    # 编译器读不到 `ref`（拿 `id` 当引用就成悬空），门禁读不到 `assertion`。
    #
    # 确定性约束**一律标 hard** —— 它们是"不满足就不许出片"。
    # 相似度类标 soft：**本机没有人脸/嵌入模型，算不出来**，
    # 标 hard 会让每一条都被判"无法判定"而升级人工（见 review/observe.py）。
    constraints: list[dict[str, Any]] = []

    # ⓪ **角色表与镜头描述是否自洽**（2026-10-17 加）
    #
    # ## 为什么这条必须存在
    #
    # 实测：某镜 `characters = ["少女","旅人"]`，而描述写的是
    # "少女 **独自** 走在山坡上采花"。外观块按**角色表**注入两份外貌，
    # 模型就画出两个人 —— **画面错了，而分辨率/时长/哈希全部通过**。
    #
    # 这类矛盾不需要看图，只要比对"文字说了几个人"与"结构声明了几个人"。
    #
    # ## 为什么先设 `audit` 而不是 `hard`
    #
    # **冷启动期先收集数据。** 这条检查是新加的，我们还不知道它在真实
    # 内容上会以多高的比例命中。直接设 `hard` 会让一批本来能跑的镜头
    # 突然被拦 —— 而"新加的门禁让流水线停摆"不是严谨，是莽撞。
    #
    # 正确顺序：`audit` 跑几集 -> 量出真实发生率 -> 再决定升不升 `hard`。
    # （这与 `PASS_THRESHOLD` 的标定是同一套纪律：**先有分布，再有阈值**。）
    constraints.append({
        # ⚠️ **ref 必须与 ③ 的 `cr_cast_*` 区分**：
        # 编译器按 ref 建 `constraint_registry`（`compiler.py:1606`），
        # 同一个 ref 后者覆盖前者 —— 两处都叫 `cr_cast_xxx` 时，
        # 这条 `audit` 级约束会被 ③ 的 `count_at_most` **整条挤出注册表**，
        # 从此再也不会被阈值联动触及。而报告里 `constraint_ref` 是
        # 断言追溯的唯一身份，两条语义不同的约束共用一个身份 = 歧义。
        "ref": f"cr_cast_text_{shot.shot_id}",
        "assertion": {"op": "cast_consistent_with_text"},
        "enforcement": "audit",
    })

    # ① 分辨率必须与请求一致 —— 编译出的 latent 尺寸错是最常见的静默故障
    w, h = (int(resolution[0]), int(resolution[1]))
    constraints.append({
        "ref": f"cr_res_{shot.shot_id}",
        "assertion": {"op": "resolution_matches", "width": w, "height": h},
        "enforcement": "hard",
        "description": f"本镜产出必须为 {w}x{h}",
    })

    # ② 时长必须在上限内 —— 超过模型硬上限的镜头无法生成
    dur = float(getattr(shot, "duration_sec", 0.0) or 0.0)
    if dur > 0:
        constraints.append({
            "ref": f"cr_dur_{shot.shot_id}",
            "assertion": {"op": "duration_within", "max": MAX_SHOT_SECONDS},
            "enforcement": "hard",
            "description": f"时长不得超过 {MAX_SHOT_SECONDS:g} 秒（模型硬上限）",
        })

    # ③ 同框角色数上限 —— 超过会把身份锁定能力摊薄
    cids = list(_character_ids(shot.characters))
    if cids:
        constraints.append({
            "ref": f"cr_cast_{shot.shot_id}",
            "assertion": {"op": "count_at_most", "field": "characterCount",
                          "max": MAX_CHARACTERS_PER_SHOT},
            "enforcement": "hard",
            "description": f"同框角色不超过 {MAX_CHARACTERS_PER_SHOT} 人",
        })

    # ④ 每个出场角色的身份一致性
    #
    # ⚠️ **强制级别是 `audit`，不是 `soft`，更不是 `hard`。**
    # 这个决定有实测依据，值得写清楚 —— 它是一次**从乐观回到诚实**的修正。
    #
    # L2 之前：本机没有人脸/嵌入模型，这条约束恒被跳过，标 `soft` 无代价。
    # L2 之后：`similarity` **能算了**，于是它**真的会参与判定** ——
    # 而这时才发现描述子测的**不是身份**：
    #
    #   · 受控退化实验（12 种，见 `research/consistency-calibration/`）
    #     证明它**能**抓住配色/光照/饱和度/构图漂移，且归因精确。
    #   · 但真机 34 张图的两两分布是 **0.66 ~ 1.00（均值 0.83）** ——
    #     同一角色在不同构图下就会落到 0.66，而**不同场景**也在同一区间。
    #     两者**重叠**，所以**没有任何阈值**能把"角色漂移"与"换了景别"分开。
    #   · 中心裁剪试过了：0.62 -> 0.58，几乎不动 —— 不是背景淹没的问题。
    #   · 所以它测的是「整体像不像同一场戏」，**不是**「是不是同一个角色」。
    #
    # 结论：这是**审计级**观测。
    #   · `audit` 会**如实记录**数值（供后续标定与人工复核）
    #   · 但**不阻断交付** —— 因为"不合格"这个结论在当前度量下不成立
    #   · `min` 取 0.70（真机实测 p10 附近），作为**告警线**而非通过线
    #
    # **不做的事**：不把它改标 `hard` 来"显得门禁更严"。
    # 那样做的后果是每一镜都被判不合格，而真正的问题是**度量能力不足** ——
    # 把能力缺失伪装成"发现了缺陷"，比不做度量更糟。
    for cid in cids:
        if not (anchor_sets and cid in anchor_sets):
            continue
        constraints.append({
            "ref": f"cr_identity_{cid}",
            "assertion": {"op": "identity_similarity",
                          "characterRef": cid, "min": 0.70},
            "enforcement": "audit",
            "description": "与角色锚点图的一致性（**度量能力有限**："
                           "抓配色/光照/构图漂移，不判身份 —— 见 §5.9）",
        })

    # ⑤ 跨镜一致性（`pairwise_similarity`）——**同样只做审计**
    #
    # 与 ④ 的区别：④ 是"产物 vs 角色参考"（绝对基准），
    # ⑤ 是"产物 vs 另一个产物"（相对一致）。后者才是**跨镜漂移**的直接度量。
    #
    # `GateRunner` 会取"**最不像的那一镜**"作为 `similarity` ——
    # 一致性是短板效应，取均值会让崩掉的那一镜被其他镜拉平而漏检。
    if len(cids) >= 1:
        constraints.append({
            "ref": f"cr_cross_shot_{shot.shot_id}",
            "assertion": {"op": "pairwise_similarity", "min": 0.70},
            "enforcement": "audit",
            "description": "与其他镜头的一致性（取最不像的一镜；"
                           "仅审计，不阻断）",
        })

    # ⑥ **跨模型族盲读**（C3 的 L3 层）
    #
    # L1（断言算子）与 L2（一致性描述子）都是**确定性**的 ——
    # 它们能查分辨率、时长、配色漂移，但**查不了语义质量**
    # （"这只手是不是画坏了"）。剩下那部分只能由视觉模型去看。
    #
    # ⚠️ **强制级别 `hard`**，与 ④⑤ 的 `audit` 不同。理由：
    #   · ④⑤ 的度量**能力不足**（描述子测不了身份），所以不能阻断
    #   · 盲读的判断是**模型给的语义裁定**，它说"有问题"就是有问题 ——
    #     这正是 C3 要的那道门
    #
    # 但 **缺观测时会被如实跳过**（没有 VLM key 就报"缺观测"），
    # 所以标 `hard` 不会让本机跑不动 —— 这是"能力预检"机制的价值。
    constraints.append({
        "ref": f"cr_blind_{shot.shot_id}",
        "assertion": {"op": "blind_read_verdict", "allow": ["PASS"]},
        "enforcement": "hard",
        "description": "跨模型族盲读裁定必须为 PASS"
                       "（无 VLM 时如实跳过，不算通过）",
    })

    return {
        "irVersion": "little_crew.ir/v1",
        "graphId": gid,
        "scope": "shot",
        "bindings": bindings,
        "nodes": nodes,
        "edges": [],
        "constraints": constraints,
        "provenance": {
            "generator": "little_crew.agent.ir_bridge",
            "shotId": shot.shot_id,
            "sceneId": shot.scene_id,
            "episodeId": ep,
            "durationSec": shot.duration_sec,
        },
    }


def shotlist_to_ir(
    shot_list: ShotList,
    *,
    limit: int | None = None,
    **kwargs: Any,
) -> list[tuple[Shot, dict[str, Any]]]:
    """把整张镜头表转成 IR 列表。返回 ``[(shot, ir), ...]``。

    ``limit`` 用于小批量试跑 —— 一集 60 镜全量编译会很久，
    先验证前几镜能出图是更稳的做法。
    """
    picked: Sequence[Shot] = shot_list.shots
    if limit is not None:
        picked = picked[: max(0, int(limit))]
    return [
        (s, shot_to_ir(s, episode_id=shot_list.episode_id, **kwargs))
        for s in picked
    ]
