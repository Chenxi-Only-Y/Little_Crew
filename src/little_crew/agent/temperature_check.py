"""LLM 温度配置自检（2026-11-16，按 DeepSeek 官方建议）。

依据（用户提供的 DeepSeek 官方建议温度）：
    代码/数学 0.0 · 数据清洗/分析 1.0 · 日常对话/翻译 1.3 · 创意写作/写诗 1.5
    Qwen3 论文评测：思考 0.6 / top-p 0.95；非思考 0.7 / 0.8

本项目的映射（见 `docs/01 §9.11`）：

| 类别 | 温度 | 职位 |
|---|---|---|
| 创意写作 | **1.5** | 编剧 · 导演 · 美术指导 · 场景设计师 |
| 结构化创作 | **1.3** | 分镜师（有 JSON 校验/重试兜底） |
| 分析/规划 | **1.0** | 灯光师 · 制片主任 · 白模预演师 |
| 判定/审查 | **0.2** | 剧本顾问（MiMo）· 剧评人/审片（GLM）· 文学策划 |
| 通用/视觉 | **0.7** | LlmClient 默认（含视觉闸/场记质检/封面选帧） |

另：Qwen 族**自动带 top_p**（非思考 0.8；思考 0.95 为未来开关预留），
其它厂商请求体**不发 top_p**（避免不支持该参数导致 400）。
"""
from __future__ import annotations

import sys

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def _temp(obj) -> float:
    return float(getattr(obj, "temperature", -1))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 68)
    print("LLM 温度配置 —— 自检")
    print("=" * 68)

    from little_crew.agent import (crew_llm, critic_llm, director_llm,
                                   gaffer_llm, line_producer_llm,
                                   previs_agent_llm,
                                   production_designer_llm,
                                   scene_designer_llm, storyboard_llm)
    from little_crew.agent.llm import (QWEN_TOP_P, QWEN_TOP_P_THINKING,
                                       LlmClient, model_spec_for)
    from little_crew.models.template import render_body_template

    print(); print("A. 创意写作 = 1.5")
    check("A1 ★★★ 编剧 1.5", _temp(crew_llm.ScreenwriterLlm()) == 1.5,
          str(_temp(crew_llm.ScreenwriterLlm())))
    check("A2 ★★★ 导演 1.5", _temp(director_llm.DirectorLlm()) == 1.5, "")
    check("A3 ★★★ 美术指导 1.5", _temp(production_designer_llm.ProductionDesignerLlm()) == 1.5, "")
    check("A4 ★★★ 场景设计师 1.5", _temp(scene_designer_llm.SceneDesignerLlm()) == 1.5, "")

    print(); print("B. 结构化创作 = 1.3")
    check("B1 ★★★ 分镜师 1.3", _temp(storyboard_llm.StoryboardArtistLlm()) == 1.3, "")

    print(); print("C. 分析/规划 = 1.0")
    for name, obj in (("灯光师", gaffer_llm.GafferLlm()),
                      ("制片主任", line_producer_llm.LineProducerLlm()),
                      ("白模预演师", previs_agent_llm.PrevisAgentLlm())):
        check(f"C {name} 1.0", _temp(obj) == 1.0, str(_temp(obj)))

    print(); print("D. 非 DeepSeek 族：不套用官方温度（保守 0.2）")
    d = crew_llm.DramaturgeLlm()
    check("D1 ★★★ 剧本顾问（MiMo）0.2", _temp(d) == 0.2, str(_temp(d)))
    check("D2 ★★★ 剧评人/审片（GLM/跨族）0.2",
          _temp(critic_llm.CriticLlm("review_board")) == 0.2, "")
    check("D3 ★★★ 剧本顾问默认客户端确为 MiMo",
          getattr(getattr(d, "client", None), "model_id", "") == "mimo-v2.6-flash",
          str(getattr(getattr(d, "client", None), "model_id", "")))
    deepseek_defaults = {
        "导演": director_llm.DirectorLlm(),
        "分镜": storyboard_llm.StoryboardArtistLlm(),
        "灯光": gaffer_llm.GafferLlm(),
        "美术": production_designer_llm.ProductionDesignerLlm(),
    }
    bad = [k for k, o in deepseek_defaults.items()
           if getattr(getattr(o, "client", None), "model_id", "") != "deepseek-flash"]
    check("D4 ★★★ DeepSeek 族职位默认客户端 = deepseek-flash", not bad, str(bad))

    print(); print("E. LlmClient 默认与 Qwen top_p")
    _kw = getattr(LlmClient.__init__, "__kwdefaults__", {}) or {}
    check("E1 ★★★ LlmClient 默认温度 0.7", _kw.get("temperature") == 0.7,
          str(_kw.get("temperature")))
    check("E2 ★★★ Qwen 非思考 top_p = 0.8 / 思考预留 0.95",
          QWEN_TOP_P == 0.8 and QWEN_TOP_P_THINKING == 0.95, "")
    values = {"messages": [{"role": "user", "content": "hi"}],
              "temperature": 0.7, "top_p": QWEN_TOP_P,
              "max_tokens": 100, "model_name": "qwen3-max",
              "prompt": "hi", "image_url": "http://x"}
    bq = render_body_template(
        model_spec_for("qwen3-max").backends[0].body_template, values)
    bv = render_body_template(
        model_spec_for("vlm-judge-cloud").backends[0].body_template, values)
    check("E3 ★★★ qwen3-max 请求体带 top_p", bq.get("top_p") == QWEN_TOP_P, str(bq.get("top_p")))
    check("E4 ★★★ qwen3-vl-max 请求体带 temperature+top_p",
          bv.get("temperature") == 0.7 and bv.get("top_p") == QWEN_TOP_P, str(bv))
    bd = render_body_template(
        model_spec_for("deepseek-flash").backends[0].body_template, values)
    check("E5 ★★★ 非 Qwen 厂商不发 top_p（避免 400）", "top_p" not in bd, str(bd))

    print(); print("F. 边界")
    all_temps = []
    for obj in (crew_llm.ScreenwriterLlm(), crew_llm.DramaturgeLlm(),
                critic_llm.CriticLlm("review_board"), director_llm.DirectorLlm(),
                gaffer_llm.GafferLlm(), line_producer_llm.LineProducerLlm(),
                previs_agent_llm.PrevisAgentLlm(),
                production_designer_llm.ProductionDesignerLlm(),
                scene_designer_llm.SceneDesignerLlm(),
                storyboard_llm.StoryboardArtistLlm()):
        all_temps.append(_temp(obj))
    check("F1 ★★★ 全部温度在 [0, 1.5]", all(0 <= t <= 1.5 for t in all_temps),
          str(sorted(all_temps)))

    n = sum(1 for _, ok, _ in _r if ok)
    print()
    for name, ok, detail in _r:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 68)
    print(f"结果：{n}/{len(_r)} 通过")
    return 0 if n == len(_r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
