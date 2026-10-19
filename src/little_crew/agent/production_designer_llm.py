"""**美术指导**：从剧本产出 `art.bible`（每场的美术设定）。

## 为什么它不与灯光重叠

灯光管「**光怎么打**」（时间/光源/色温/影调），美术管「**东西是什么样**」
（建筑/材质/年代/道具/色板）。两者都进画面，但问的是不同的问题：

| | 灯光师问 | 美术指导问 |
|---|---|---|
| 时间 | 夜 / 晨 / 黄昏 | — |
| 光源 | 窗在左侧，硬光斜切 | — |
| **空间** | — | 木结构老屋 / 砖墙 / 茅草顶 |
| **材质** | — | 粗布 / 褪色木 / 锈铁 |
| **年代** | — | 近代 / 民国 / 架空 |
| **色板** | — | 靛蓝 · 灰白 · 赭石 |

**分开的理由**：合成一个职位会让提示词里"光"与"物"互相稀释 ——
实测灯光方案单独注入时画面**逐项兑现**（亮度 −37、色温 −11），
说明"一次只问一件事"是有效的。

## 边界

**不写分镜、不定镜头、不改剧情、不打光。** 它只回答"这场的东西长什么样"。
"""
from __future__ import annotations

from typing import Any, Mapping

from little_crew.agent.llm import LLMError, LlmClient
from little_crew.agent.protocol import Budget, CallEnvelope, CallResult

__all__ = [
    "ERA_LEVELS",
    "ProductionDesignerLlm",
    "normalize_art",
]

#: 年代档位的**封闭集**。自由文本会让下游无法对齐
#: （同 `gaffer` 的 `contrast` 封闭集，理由一致）。
ERA_LEVELS: tuple[str, ...] = ("古代", "近代", "民国", "当代", "未来", "架空")

_SYSTEM = (
    "你是一位美术指导。你只负责**每一场的空间、材质、道具、色板**。"
    "你**不打光**（那是灯光师）、**不写分镜**、**不改剧情**。"
    "你的输出会被直接转成画面的用词，所以要具体到能执行。"
)


class ProductionDesignerLlm:
    """把 `production_designer` 实现为真实 LLM 职位。"""

    agent_id = "production_designer"
    capability = "scene.design"
    #: **产物键**是名册声明的 `produces`，不是 capability 名
    produces_key = "art.bible"
    consumes_key = "script.draft"

    def __init__(self, *, client: LlmClient | None = None,
                 temperature: float = 0.3) -> None:
        self.client = client or LlmClient("deepseek-flash")
        self.temperature = temperature

    def _render(self, payload: Mapping[str, Any]) -> tuple[str, str]:
        art = payload.get(self.consumes_key)
        body = art if isinstance(art, str) else _as_text(art)
        prompt = (
            f"## 剧本\n{body or '（空）'}\n\n"
            "## 任务\n为**每一场**给出美术设定。\n\n"
            "严格输出 JSON：\n"
            '{"scenes": [{"sceneId": "S01",\n'
            '  "space": "这是什么空间（建筑/结构/大小）",\n'
            '  "materials": ["主要材质，2~3 个"],\n'
            '  "props": ["这场的关键道具"],\n'
            '  "era": "古代|近代|民国|当代|未来|架空",\n'
            '  "palette": ["这场的主色，2~3 个"]}],\n'
            ' "throughLine": "整集美术上的一条贯穿线索（一句话）"}\n\n'
            "规则：\n"
            "  · **`scenes` 必须覆盖剧本每一场**，用剧本里的场次号\n"
            "  · `era` 只能取那**六个值之一**\n"
            "  · `space` 要写**结构与尺度**：写「木结构老屋，一间，窗很小」"
            "而不是「古色古香」\n"
            "  · `props` 只写**剧情用得到**的，不要堆砌\n"
            "  · **不要打光、不要写分镜、不要改剧情**"
        )
        return prompt, _SYSTEM

    def __call__(self, *, envelope: CallEnvelope,
                 runtime: Any = None) -> CallResult:
        payload = dict(envelope.payload or {})
        art = payload.get(self.consumes_key)
        if not art:
            return CallResult.fail(
                "F-INPUT", f"production_designer 需要 payload[{self.consumes_key!r}]")
        text = art if isinstance(art, str) else _as_text(art)
        if not text.strip():
            return CallResult.fail("F-INPUT", "剧本正文为空 —— 无法做美术设定")
        try:
            prompt, system = self._render(payload)
        except Exception as e:                                # noqa: BLE001
            return CallResult.fail("F-RENDER", f"提示词构造失败: {e}")
        try:
            data = self.client.chat_json(prompt, system=system,
                                         what="美术设定")
        except LLMError as e:
            return CallResult.fail(f"F-LLM-{e.kind}",
                                   f"production_designer 调用失败: {e}",
                                   escalate=not e.retryable)
        except Exception as e:                                # noqa: BLE001
            return CallResult.fail("F-LLM", f"production_designer 解析失败: {e}")
        return CallResult.success({self.produces_key: normalize_art(data)})


def normalize_art(data: Mapping[str, Any]) -> dict[str, Any]:
    """规整成下游可消费的美术设定。两条规矩：`era` 封闭集、无 `sceneId` 丢弃。"""
    scenes: list[dict[str, Any]] = []
    for s in (data.get("scenes") if isinstance(data.get("scenes"), list) else ()):
        if not isinstance(s, Mapping):
            continue
        sid = str(s.get("sceneId") or s.get("scene_id") or "").strip()
        if not sid:
            continue
        era = str(s.get("era") or "").strip()
        if era not in ERA_LEVELS:
            era = next((e for e in ERA_LEVELS if e in era), "架空")
        scenes.append({
            "sceneId": sid,
            "space": str(s.get("space") or "")[:140],
            "materials": [str(x)[:30] for x in (s.get("materials") or ())
                          if str(x).strip()][:4],
            "props": [str(x)[:40] for x in (s.get("props") or ())
                      if str(x).strip()][:6],
            "era": era,
            "palette": [str(x)[:24] for x in (s.get("palette") or ())
                        if str(x).strip()][:4],
        })
    return {"scenes": scenes,
            "throughLine": str(data.get("throughLine") or "")[:160]}


def _as_text(art: Any) -> str:
    """优先用项目已有的 `script_text()`（与定妆表/导演/灯光同一套）。"""
    try:
        from little_crew.agent.character_sheet import script_text
        got = script_text(art)
        if isinstance(got, str) and got.strip():
            return got
    except Exception:                                         # noqa: BLE001
        pass
    for attr in ("as_text", "to_text", "text"):
        v = getattr(art, attr, None)
        if isinstance(v, str) and v.strip():
            return v
        if callable(v):
            try:
                got = v()
                if isinstance(got, str) and got.strip():
                    return got
            except Exception:                                 # noqa: BLE001
                pass
    return str(art or "")
