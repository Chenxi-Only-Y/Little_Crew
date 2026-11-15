"""场景设计师（`scene_designer`，2026-11-15）。

## 为什么单独设一个职位

角色有专门的**角色设计师**（产 `character.sheet` 图 + 外观文字）；场景此前却塞在
**美术指导**（`production_designer`）的能力里，只产文字 `art.bible`，没有场景资产。
而现实里场景会**跨镜头、跨集重复出现** —— 没有固定的场景设计，前后必然漂移。

所以拆成两个同级职位（都在美术组）：
- **美术指导**：总体视觉基调 + `art.bible` + 跨资产一致性裁决（**不直接产场景资产**）
- **场景设计师**：逐场 `space / materials / props / palette / era` → `scene.library`

## 两种实现

| 实现 | 何时用 | 说明 |
|---|---|---|
`SceneDesignerRules`（规则桩） | **默认** | 从剧本场景原文**逐字**取 `space`、按关键词推断 `era`；零成本、完全确定 |
`SceneDesignerLlm` | `--llm-scene` | 真实 LLM，补全材质/道具/色板；仍经 `normalize_scene_library` 严格校验 |

> 规则桩的意义：**即使不开 LLM，"同一场戏的每个镜头也拿到逐字相同的场景描述"**
> —— 一致性先于丰富度。
"""

from __future__ import annotations

from typing import Any, Mapping

#: 年代关键词（规则桩用；LLM 走封闭集校验）
_ERA_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("古代", "古装", "江湖", "宫廷", "沙场", "马车"), "古代"),
    (("民国", "旗袍", "洋楼", "黄包车"), "民国"),
    (("未来", "赛博", "飞船", "星舰", "机甲", "全息"), "未来"),
    (("架空", "异世界", "魔法", "大陆"), "架空"),
    (("近代", "蒸汽", "工厂"), "近代"),
)
DEFAULT_ERA = "当代"


def _scene_text(scene: Mapping[str, Any]) -> str:
    """取场景原文（逐字稳定；不摘要、不改写）。"""
    for key in ("text", "description", "summary", "content"):
        v = scene.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _scene_id(scene: Mapping[str, Any]) -> str:
    for key in ("scene_id", "sceneId", "id"):
        v = scene.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _guess_era(text: str) -> str:
    for words, era in _ERA_HINTS:
        if any(w in text for w in words):
            return era
    return DEFAULT_ERA


def normalize_scene_library(data: Mapping[str, Any] | None) -> dict[str, Any]:
    """归一化 + 校验场景库。坏 `era` 直接报错（不静默）。

    返回 `{"scenes": [{sceneId, space, materials, props, palette, era}],
    "throughLine": str}`。
    """
    from little_crew.ir.scene_card import ERA_LEVELS, canonical_scene_card

    out: dict[str, Any] = {"kind": "scene.library", "scenes": [],
                           "throughLine": ""}
    if not data:
        return out
    raw = data.get("scenes")
    for sc in (raw if isinstance(raw, list) else ()):
        if not isinstance(sc, Mapping):
            continue
        sid = _scene_id(sc)
        if not sid:
            continue                      # 没场次号的场景卡无法对账
        try:
            card = canonical_scene_card(sc)
        except ValueError:
            raise
        if not card.get("space"):
            # 场景没有空间描述 = 没有设计，不产出空卡
            continue
        out["scenes"].append({"sceneId": sid, **card})
    tl = str(data.get("throughLine") or "").strip()
    if tl:
        out["throughLine"] = tl[:200]
    return out


# ==========================================================================
# 规则桩（默认）
# ==========================================================================

class SceneDesignerRules:
    """确定性场景设计：从剧本场景原文生成 `scene.library`。**不调模型**。

    每个场景：
    - `space` = 场景原文（逐字，跨镜一致的前提）
    - `era` = 关键词推断（现代题材兜底"当代"）
    - `materials/props/palette` 留空（规则推不出来，**不编**）
    """

    agent_id = "scene_designer"
    capability = "scene.design"
    produces_key = "scene.library"
    consumes_key = "script.draft"

    def __call__(self, *, envelope: Any, runtime: Any = None) -> Any:
        from little_crew.agent.protocol import CallResult

        payload = dict(getattr(envelope, "payload", {}) or {})
        draft = payload.get(self.consumes_key)
        scenes: list[dict[str, Any]] = []
        raw_scenes = getattr(draft, "scenes", None) or []
        for sc in raw_scenes:
            try:
                m = dict(sc)
            except Exception:                                 # noqa: BLE001
                continue
            sid = _scene_id(m)
            text = _scene_text(m)
            if not sid or not text:
                continue
            scenes.append({"sceneId": sid, "space": text[:160],
                           "era": _guess_era(text)})
        lib = normalize_scene_library({"scenes": scenes})
        return CallResult.success({self.produces_key: lib})


# ==========================================================================
# 真实 LLM（--llm-scene）
# ==========================================================================

_SYSTEM = (
    "你是一位场景设计师。你只负责**每一场的空间、材质、道具、色板、年代**。"
    "你**不打光**（那是灯光师）、**不写分镜**、**不改剧情**。"
    "输出会被直接转成画面用词，必须具体到能执行。"
)


class SceneDesignerLlm:
    """把 `scene_designer` 实现为真实 LLM 职位。"""

    agent_id = "scene_designer"
    capability = "scene.design"
    produces_key = "scene.library"
    consumes_key = "script.draft"

    def __init__(self, *, client: Any = None, temperature: float = 0.3) -> None:
        if client is None:
            from little_crew.agent.llm import LlmClient

            client = LlmClient("deepseek-flash")
        self.client = client
        self.temperature = temperature

    def _render(self, payload: Mapping[str, Any]) -> tuple[str, str]:
        art = payload.get(self.consumes_key)
        body = art if isinstance(art, str) else _as_text(art)
        prompt = (
            f"## 剧本\n{body or '（空）'}\n\n"
            "## 任务\n为**每一场**给出场景设计。\n\n"
            "严格输出 JSON：\n"
            '{"scenes": [{"sceneId": "S01",\n'
            '  "space": "空间结构与尺度（建筑/结构/大小）",\n'
            '  "materials": ["主要材质，2~3 个"],\n'
            '  "props": ["剧情用得到的道具"],\n'
            '  "palette": ["主色，2~3 个"],\n'
            '  "era": "古代|近代|民国|当代|未来|架空"}],\n'
            ' "throughLine": "整集场景上的一条贯穿线索（一句话）"}\n\n'
            "规则：\n"
            "  · `scenes` 必须覆盖剧本每一场，用剧本里的场次号\n"
            "  · `era` 只能取那六个值之一\n"
            "  · `space` 写结构与尺度，不写「古色古香」这类形容词\n"
            "  · 不要打光、不要写分镜、不要改剧情"
        )
        return prompt, _SYSTEM

    def __call__(self, *, envelope: Any, runtime: Any = None) -> Any:
        from little_crew.agent.protocol import CallResult

        payload = dict(getattr(envelope, "payload", {}) or {})
        prompt, system = self._render(payload)
        reply = self.client.chat(prompt, system=system,
                                 temperature=self.temperature)
        text = str(getattr(reply, "text", "") or reply or "")
        try:
            data = _loads_json(text)
            lib = normalize_scene_library(data)
        except Exception as exc:                              # noqa: BLE001
            return CallResult.fail("F-PARSE",
                                   f"场景库解析失败：{type(exc).__name__}: {exc}")
        return CallResult.success({self.produces_key: lib})


def _loads_json(text: str) -> dict[str, Any]:
    import json
    import re

    s = str(text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        s = s[i:j + 1]
    obj = json.loads(s)
    if not isinstance(obj, dict):
        raise ValueError("不是一个 JSON 对象")
    return obj


def _as_text(art: Any) -> str:
    if isinstance(art, str):
        return art
    if isinstance(art, Mapping):
        for k in ("text", "script", "final", "content"):
            v = art.get(k)
            if isinstance(v, str) and v.strip():
                return v
    return str(art or "")
