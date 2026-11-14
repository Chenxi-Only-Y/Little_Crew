"""真实 LLM 剧组的公共绑定层（2026-11-14，A1 续）。

## 为什么抽出来

`episode_demo` 里有一大段"把规则桩换成真实 LLM 实现"的代码；
产品 CLI（`little-crew episode`）此前**只有规则桩**——于是产品入口跑出来的
剧本/分镜是桩写的，而 demo 能出真内容。两份实现必然漂移，这里收敛成一份：

```python
info = bind_llm_crew(stubs, llm=True, llm_model="deepseek-flash",
                     llm_reviewer="mimo-v2.6-flash", ...)
if not info["ok"]:
    print(info["error"]); return 1
```

## 纪律（都来自实测）

1 **编剧与剧评人必须不同族**：同族会让门禁退化成自我签收，且**不报错**。
2 **换实现要成对换**：分镜师按规则桩的产出形态（正则解析 `[场N]`/`@角色`）设计，
   LLM 编剧产出结构化 `scenes` —— 所以 `screenwriter` 换真实现时必须同时换
   `storyboard_artist`，否则"跑满 3 轮但分镜 0 个"。
3 **密钥缺失要明确失败**：报出 `apiKeyEnv`，不让用户猜。
4 可选职位（导演/预演/美术/排期/灯光）**按开关启用**，不默认全开（每个都花钱）。
"""

from __future__ import annotations

from typing import Any, Mapping

#: 审核方默认模型（与生成方不同族）
DEFAULT_REVIEWER = "mimo-v2.6-flash"
#: 文学策划（文风批评家）默认模型
DEFAULT_CRITIC = "GLM-5.3-flash"


def bind_llm_crew(
    stubs: dict[str, Any],
    *,
    llm: bool = False,
    llm_model: str = "deepseek-flash",
    llm_reviewer: str = DEFAULT_REVIEWER,
    llm_critic_model: str = DEFAULT_CRITIC,
    director: bool = False,
    previs: bool = False,
    art: bool = False,
    schedule: bool = False,
    gaffer: bool = False,
    critics: bool = False,
    qc: bool = False,
) -> dict[str, Any]:
    """把 `stubs` 里的桩替换成真实 LLM 实现（原地修改并返回摘要）。

    返回 `{"ok", "error", "bound": [...], "clients": {"writer":..,"reviewer":..},
    "writer": 摘要, "reviewer": 摘要}`。
    """
    out: dict[str, Any] = {"ok": True, "error": "", "bound": [], "clients": {}}
    if not llm:
        return out

    from little_crew.agent.crew_llm import DramaturgeLlm, ScreenwriterLlm
    from little_crew.agent.llm import LlmClient

    try:
        writer = LlmClient(str(llm_model or "deepseek-flash"))
        reviewer = LlmClient(str(llm_reviewer or DEFAULT_REVIEWER))
    except Exception as exc:                                  # noqa: BLE001
        out["ok"] = False
        out["error"] = f"无法创建 LLM 客户端：{type(exc).__name__}: {exc}"
        return out
    out["clients"] = {"writer": writer, "reviewer": reviewer}

    for label, cl in (("编剧", writer), ("剧评人", reviewer)):
        d = cl.describe()
        out["writer" if cl is writer else "reviewer"] = d
        if not d.get("configured"):
            out["ok"] = False
            out["error"] = (f"{label}未配置密钥（{d.get('apiKeyEnv')}）—— "
                            f"进程环境 / 秘密文件 / config/.env 三处都取不到")
            return out

    # 规则 1：跨族
    if writer.model_id == reviewer.model_id:
        out["ok"] = False
        out["error"] = (f"编剧与剧评人用了同一个模型（{writer.model_id}）—— "
                        f"跨模型族盲读要求两者不同族")
        return out

    stubs["screenwriter"] = ScreenwriterLlm(client=writer)
    stubs["dramaturge"] = DramaturgeLlm(client=reviewer)
    # 规则 2：编剧换真实现 -> 分镜师必须同时换
    from little_crew.agent.storyboard_llm import StoryboardArtistLlm

    stubs["storyboard_artist"] = StoryboardArtistLlm(client=writer)
    out["bound"].extend(["screenwriter", "dramaturge", "storyboard_artist"])

    # ---- 可选：创作类职位（生成方同族）----
    if director:
        from little_crew.agent.director_llm import DirectorLlm

        stubs["director"] = DirectorLlm(client=writer)
        out["bound"].append("director")
    if previs:
        from little_crew.agent.previs_agent_llm import PrevisAgentLlm

        stubs["previs"] = PrevisAgentLlm(client=writer)
        out["bound"].append("previs")
    if art:
        from little_crew.agent.production_designer_llm import ProductionDesignerLlm

        stubs["production_designer"] = ProductionDesignerLlm(client=writer)
        out["bound"].append("production_designer")
    if schedule:
        from little_crew.agent.line_producer_llm import LineProducerLlm

        stubs["line_producer"] = LineProducerLlm(client=writer)
        out["bound"].append("line_producer")
    if gaffer:
        from little_crew.agent.gaffer_llm import GafferLlm

        stubs["gaffer"] = GafferLlm(client=writer)
        out["bound"].append("gaffer")

    # ---- 可选：审查类职位（审核方族）----
    if critics:
        from little_crew.agent.critic_llm import CriticLlm

        critic = LlmClient(str(llm_critic_model or DEFAULT_CRITIC))
        c_d = critic.describe()
        out["critic"] = c_d
        if not c_d.get("configured"):
            out["ok"] = False
            out["error"] = (f"文风批评家未配置密钥（{c_d.get('apiKeyEnv')}）")
            return out
        stubs["story_editor"] = CriticLlm("story_editor", client=critic)
        out["bound"].append("story_editor")
        out["clients"]["critic"] = critic
    if qc:
        try:
            from little_crew.review.qc_inspector import QcInspector

            stubs["qc_inspector"] = QcInspector(client=reviewer)
            out["bound"].append("qc_inspector")
        except Exception as exc:                              # noqa: BLE001
            out["qcError"] = f"{type(exc).__name__}: {exc}"

    # ---- 电影整体审（review_board）用审核方 ----
    try:
        from little_crew.agent.critic_llm import CriticLlm

        stubs["review_board"] = CriticLlm("review_board", client=reviewer)
        out["bound"].append("review_board")
    except Exception:                                         # noqa: BLE001
        pass
    return out


def describe_binding(info: Mapping[str, Any]) -> list[str]:
    """给终端打印用的摘要行（不含密钥明文）。"""
    lines: list[str] = []
    w = info.get("writer") or {}
    r = info.get("reviewer") or {}
    if w:
        lines.append(f"编剧：{w.get('displayName')} / {w.get('apiKeyMasked')}")
    if r:
        lines.append(f"剧评人：{r.get('displayName')} / {r.get('apiKeyMasked')}")
    c = info.get("critic") or {}
    if c:
        lines.append(f"文风批评家：{c.get('displayName')} / {c.get('apiKeyMasked')}")
    b = info.get("bound") or []
    if b:
        lines.append("真实 LLM 绑定：" + "、".join(b))
    return lines
