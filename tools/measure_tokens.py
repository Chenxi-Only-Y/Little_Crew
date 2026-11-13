"""实测 token 用量（B2 成本测算的输入，2026-11-13）。

## 为什么单独做一个测量脚本

厂商价目表按 **百万 token** 计价（GLM-5.3-Flash：输入 ¥0.8/M、输出 ¥2.8/M；
DeepSeek-V4.1-Flash：输入 ¥1~2/M、输出 ¥4~8/M；mimo-v2.6-flash：¥1/M 输入、¥2/M 输出）。
要把"一集花多少钱"算出来，必须知道**每次调用真实用掉多少 token** ——
而这个数字只有服务端 `usage` 里有。之前没记（`LLMReply.usage` 拿到就丢了）。

本脚本跑一组**代表性调用**（文本编剧 / 分镜 / 看图质检 / 封面选帧），
打印每次的 input/output token 与按当前价目表算出的现金成本。

用法：

```powershell
$env:PYTHONPATH="<项目>\\src"; $env:LITTLE_CREW_ALLOW_UNVERIFIED="1"
python tools/measure_tokens.py [--image outputs/.../shot.png] [--grid outputs/.../cover_candidates.jpg]
```
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

#: 价目表（元 / 百万 token）—— 用户提供的官方价（2026-11-13）
PRICES = {
    "GLM-5.3-flash": {"in": 0.8, "out": 2.8, "in_cached": 0.23},
    "deepseek-flash": {"in": 1.0, "out": 4.0, "in_cached": 0.04},   # 取空闲时段
    "mimo-v2.6-flash": {"in": 1.0, "out": 2.0, "in_cached": 0.02},
}


def _cost(model: str, usage: dict) -> float:
    p = PRICES.get(model)
    if not p:
        return 0.0
    inp = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    out = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    cached = int(usage.get("prompt_cache_hit_tokens") or usage.get("cached_tokens") or 0)
    miss = max(0, inp - cached)
    return (miss * p["in"] + cached * p["in_cached"] + out * p["out"]) / 1e6


def _run(label: str, fn) -> dict:
    from little_crew.agent.llm import reset_usage, usage_totals

    reset_usage()
    try:
        fn()
    except Exception as exc:                                  # noqa: BLE001
        print(f"  ⚠️ {label} 调用失败：{type(exc).__name__}: {exc}")
    tot = usage_totals()
    cny = sum(_cost(m, {}) + sum(0 for _ in ()) for m in ())  # 占位，下面按模型算
    cny = 0.0
    for m, d in tot["models"].items():
        cny += _cost(m, {"prompt_tokens": d["input"], "completion_tokens": d["output"],
                         "prompt_cache_hit_tokens": d["cached"]})
    print(f"  {label:22s} 调用 {tot['totalCalls']} 次 · 输入 {tot['totalInput']} tok "
          f"· 输出 {tot['totalOutput']} tok · 估算 ¥{cny:.4f}")
    return {"label": label, "models": {k: dict(v) for k, v in tot["models"].items()},
            "calls": tot["totalCalls"], "input": tot["totalInput"],
            "output": tot["totalOutput"], "cny": round(cny, 5)}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default="", help="一张镜头图（测看图）")
    ap.add_argument("--grid", default="", help="封面候选拼图（测选帧）")
    ap.add_argument("--json-out", default="outputs/_cost/token_usage.json")
    args = ap.parse_args()

    from little_crew.agent.llm import LlmClient

    rows = []

    def _screen():
        from little_crew.agent.llm import LlmClient as _L
        from little_crew.runtime.episode_demo import LAVENDER_LEGEND

        c = _L("deepseek-flash")
        c.chat("你是短剧编剧。把下面的传说改写成 3 分钟短剧剧本：8 场，"
               "每场 4~6 句台词，带场景/人物/动作提示。只输出剧本正文。\n\n"
               + LAVENDER_LEGEND)

    rows.append(_run("编剧（代表性题面）", _screen))

    def _storyboard():
        from little_crew.agent.llm import LlmClient as _L

        c = _L("deepseek-flash")
        c.chat("请把这段故事拆成 8 个镜头，每镜给出 shotId、景别、画面描述"
               "（60~120 字）、时长（4~8 秒）。只输出 JSON。\n\n"
               "故事：山村少女阿蘅在瀑布边救下一个受伤的陌生旅人，两人在木屋与"
               "集市之间走过了三个季节，最终旅人离开，阿蘅留在村里种下薰衣草。")

    rows.append(_run("分镜（8 镜）", _storyboard))

    if args.image and Path(args.image).is_file():
        def _vision():
            from little_crew.agent.llm import LlmClient as _L

            c = _L("mimo-v2.6-flash")
            data = base64.b64encode(Path(args.image).read_bytes()).decode()
            c.chat_image("判断这帧画面：人物是否完整、有无崩坏、是否与描述一致。"
                         "只输出一行 JSON：{\"ok\":true|false,\"why\":\"20字内\"}",
                         data, what="成本测量", max_tokens=120)

        rows.append(_run("看图质检", _vision))

    if args.grid and Path(args.grid).is_file():
        def _grid():
            from little_crew.review.cover_designer import judge_grid

            judge_grid(args.grid, n=4, cols=2)

        rows.append(_run("封面选帧（2x2 拼图）", _grid))

    out = ROOT / args.json_out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"prices": PRICES, "rows": rows}, ensure_ascii=False,
                              indent=2), encoding="utf-8")
    print(f"  已写 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
