"""**灯光师**：从剧本 + 导演方案产出 `lighting.plan`（每场的光怎么打）。

## 为什么灯光方案能影响画面，而"氛围"不能

「这一场要有压抑感」是**没法执行**的 —— 分镜师不知道往提示词里写什么。
而下面这些是**可执行的**：

| 字段 | 例子 | 对提示词的作用 |
|---|---|---|
| `timeOfDay` | 清晨 / 正午 / 黄昏 / 夜 | 决定整体色温与阴影长度 |
| `keyLight` | 窗侧逆光 / 顶上单一光源 | 决定明暗结构与立体感 |
| `colorTemp` | 冷白 / 暖黄 / 惨绿 | 决定色板 |
| `contrast` | 高 / 中 / 低 | 决定影调（硬光 vs 柔光） |
| `note` | 一句话说明这场光要服务什么 | 给分镜师的意图 |

**这五项都能直接变成提示词里的词**，而「压抑感」不能 ——
所以灯光师产的是**可执行的光**，不是形容词。

## 边界

**不写分镜、不写景别、不改剧情。** 它只回答"这场的光是什么样"。
"""
from __future__ import annotations

from typing import Any, Mapping

from little_crew.agent.llm import LLMError, LlmClient
from little_crew.agent.protocol import Budget, CallEnvelope, CallResult

__all__ = [
    "CONTRAST_LEVELS",
    "GafferLlm",
    "normalize_lighting",
]

#: 合法的影调档位。**封闭集** —— 自由文本会让分镜师无法对齐。
CONTRAST_LEVELS: tuple[str, ...] = ("高", "中", "低")

_SYSTEM = (
    "你是一位灯光师。你只负责**每一场的光**：时间、主光源、色温、影调。"
    "你**不写**分镜、**不写**景别、**不改**剧情。"
    "你的输出会被直接转成画面的用词，所以要具体到能执行。"
)


class GafferLlm:
    """把 `gaffer` 实现为真实 LLM 职位。"""

    agent_id = "gaffer"
    capability = "lighting.plan"
    #: **产物键**是名册声明的 `produces`，不是 capability 名
    produces_key = "lighting.plan"
    consumes_key = "script.draft"

    def __init__(self, *, client: LlmClient | None = None,
                 plan: Mapping[str, Any] | None = None,
                 temperature: float = 0.3) -> None:
        self.client = client or LlmClient("deepseek-v4")
        #: 导演方案（有就参考基调，没有也能工作）
        self.plan = dict(plan or {})
        self.temperature = temperature

    # ---- 提示词 --------------------------------------------------------

    def _render(self, payload: Mapping[str, Any]) -> tuple[str, str]:
        art = payload.get(self.consumes_key)
        body = art if isinstance(art, str) else _as_text(art)
        extra = ""
        if self.plan.get("tone"):
            extra += f"\n导演定的基调：{self.plan['tone']}"
        if self.plan.get("motifs"):
            extra += f"\n反复出现的视觉动机：{'、'.join(self.plan['motifs'])}"
        prompt = (
            f"## 剧本\n{body or '（空）'}\n{extra}\n\n"
            "## 任务\n为**每一场**给出灯光方案。\n\n"
            "严格输出 JSON：\n"
            '{"scenes": [{"sceneId": "S01",\n'
            '  "timeOfDay": "清晨|正午|黄昏|夜|室内常亮",\n'
            '  "keyLight": "主光源在哪、什么性质",\n'
            '  "colorTemp": "冷白|暖黄|惨绿|中性",\n'
            '  "contrast": "高|中|低",\n'
            '  "note": "这场的光要服务什么"}],\n'
            ' "palette": ["这一集的主色板，2~4 个"]}\n\n'
            "规则：\n"
            "  · **`scenes` 必须覆盖剧本每一场**，用剧本里的场次号\n"
            "  · `contrast` 只能是 **高 / 中 / 低** 三档之一\n"
            "  · `keyLight` 要写**位置与性质**："
            "写「窗在左侧，硬光斜切」而不是「好看的光」\n"
            "  · `note` 写这场光**服务什么**："
            "写「压住她的犹豫，只照亮手里的信」而不是「营造氛围」\n"
            "  · **不要写分镜、不要写景别、不要改剧情**"
        )
        return prompt, _SYSTEM

    # ---- 调用 ----------------------------------------------------------

    def __call__(self, *, envelope: CallEnvelope,
                 runtime: Any = None) -> CallResult:
        payload = dict(envelope.payload or {})
        art = payload.get(self.consumes_key)
        if not art:
            return CallResult.fail(
                "F-INPUT", f"gaffer 需要 payload[{self.consumes_key!r}]")
        text = art if isinstance(art, str) else _as_text(art)
        if not text.strip():
            return CallResult.fail("F-INPUT", "剧本正文为空 —— 无法定光")
        try:
            prompt, system = self._render(payload)
        except Exception as e:                                # noqa: BLE001
            return CallResult.fail("F-RENDER", f"提示词构造失败: {e}")

        try:
            data = self.client.chat_json(prompt, system=system,
                                         what="灯光方案")
        except LLMError as e:
            return CallResult.fail(f"F-LLM-{e.kind}",
                                   f"gaffer 调用失败: {e}",
                                   escalate=not e.retryable)
        except Exception as e:                                # noqa: BLE001
            return CallResult.fail("F-LLM", f"gaffer 解析失败: {e}")

        lighting = normalize_lighting(data)
        return CallResult.success({self.produces_key: lighting})


# ---- 规整 --------------------------------------------------------------

def normalize_lighting(data: Mapping[str, Any]) -> dict[str, Any]:
    """把模型输出规整成可下游消费的灯光方案。

    两条规矩：

    1. **`contrast` 必须落在封闭集里** —— 自由文本（"较强"）没法对齐，
       下游拿它去做提示词会写出不一致的描述。
    2. **没有 `sceneId` 的场次丢掉** —— 无场次号的光方案无法对账。
    """
    scenes: list[dict[str, Any]] = []
    for s in (data.get("scenes") if isinstance(data.get("scenes"), list) else ()):
        if not isinstance(s, Mapping):
            continue
        sid = str(s.get("sceneId") or s.get("scene_id") or "").strip()
        if not sid:
            continue
        contrast = str(s.get("contrast") or "中").strip()
        if contrast not in CONTRAST_LEVELS:
            # 猜一下方向（"强烈"/"很硬" -> 高），猜不到给"中"
            contrast = ("高" if any(k in contrast for k in ("强", "硬", "高"))
                        else "低" if any(k in contrast for k in ("柔", "弱", "低"))
                        else "中")
        scenes.append({
            "sceneId": sid,
            "timeOfDay": str(s.get("timeOfDay") or "")[:40],
            "keyLight": str(s.get("keyLight") or "")[:120],
            "colorTemp": str(s.get("colorTemp") or "")[:40],
            "contrast": contrast,
            "note": str(s.get("note") or "")[:120],
        })

    palette = [str(c)[:30] for c in (data.get("palette") or ())
               if str(c).strip()][:4]
    return {"scenes": scenes, "palette": palette}


def _as_text(art: Any) -> str:
    """优先用项目已有的 `script_text()`（与定妆表、导演同一套）。"""
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
