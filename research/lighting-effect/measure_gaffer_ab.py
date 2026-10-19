"""用 **`gaffer` 真跑产出的**灯光方案重做 A/B。

## 与上一个实验的区别

`measure_lighting_effect.py` 里的灯光块是**我手写的**。那只证明了
"**文字能改画面**"，没证明"**模型写的文字也能改画面**"。

这两件事不是一回事：

| | 已证 | 本实验 |
|---|---|---|
| 文字能改画面 | ✅ | — |
| **LLM 写的灯光能不能改画面** | ❌ | ← 这个 |

差别在哪：我手写的灯光块是**为这一镜量身定做**的（"只照亮她的手与竹篮"）。
而 `gaffer` 是**看着整部剧本**给每场定光 —— 它可能给出通用的、
与具体镜头无关的描述。**那种描述能不能落进画面，是另一个问题。**

## 做法

1. 真调 `GafferLlm` 拿一份 `lighting.plan`
2. 取出对应那场的光
3. 拼成提示词里的灯光块（**格式与 `storyboard_llm` 注入时一致**）
4. A（无灯光）/ B（有灯光）各出一图，同 seed
5. 像素统计 + 落盘原图供人眼复核
"""
from __future__ import annotations

import json
import pathlib
import sys
import time
import urllib.request

sys.path.insert(0, r"D:\AI\ds.8\Little_Crew\src")
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

BASE = "http://127.0.0.1:8100"
OUT = pathlib.Path(r"D:\AI\ds.8\Little_Crew\research\lighting-effect")
OUT.mkdir(parents=True, exist_ok=True)

#: 一段两场的小剧本 —— 让灯光师有东西可看，且要给两场各定光
SCRIPT = ("第一场：内景。少女独自坐在屋里，窗外的雨下了一整夜，"
          "桌上摊着一封没写完的信。\n"
          "第二场：外景。清晨的薰衣草田，雾还没散，她背着竹篮走进田里。")

#: 我们要测的那一场（取它对应的光）
TARGET_SCENE = "S01"

#: 这一镜的画面描述（与灯光无关，两组相同）
SHOT = ("中近景。少女坐在窗前的桌边，手里捏着那封信，"
        "抬头看向窗外的雨。神情安静而疲惫。")
STYLE = "日式动画风格，胶片质感。"
SEED = 777
W, H = 640, 384


def render(prompt: str, tag: str) -> pathlib.Path:
    body = {"model": "z-image-turbo", "prompt": prompt,
            "negative_prompt": "", "width": W, "height": H,
            "num_inference_steps": 8, "seed": SEED}
    req = urllib.request.Request(
        f"{BASE}/v1/images/generations",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=600) as r:
        d = json.loads(r.read())
    url = (d.get("data") or [{}])[0].get("url") or ""
    if not url:
        raise RuntimeError(f"没有产物 url: {d}")
    dst = OUT / f"{tag}.png"
    with urllib.request.urlopen(url, timeout=300) as r:
        dst.write_bytes(r.read())
    print(f"    ✅ {tag}  {time.perf_counter()-t0:.1f}s  "
          f"{dst.stat().st_size // 1024} KB  (prompt {len(prompt)} 字)")
    return dst


def stats(path: pathlib.Path) -> dict:
    try:
        from PIL import Image
    except Exception:                                         # noqa: BLE001
        return {}
    im = Image.open(path).convert("RGB").resize((160, 96))
    px = list(im.getdata())
    n = len(px)
    lum = [(0.299 * r + 0.587 * g + 0.114 * b) for r, g, b in px]
    mean = sum(lum) / n
    var = sum((x - mean) ** 2 for x in lum) / n
    return {
        "meanLuma": round(mean, 2),
        "contrastStd": round(var ** 0.5, 2),
        "warmBias": round(sum(p[0] for p in px) / n
                          - sum(p[2] for p in px) / n, 2),
    }


def lighting_block(L: dict) -> str:
    """**与 `storyboard_llm` 注入时的格式保持一致的写法。**
    不一致的话，测的就不是主流程里真正发生的那件事。
    """
    bits = [x for x in (L.get("timeOfDay"), L.get("keyLight"),
                        L.get("colorTemp")) if x]
    line = "；".join(bits) + f"；影调 {L.get('contrast')}"
    if L.get("note"):
        line += f"；{L['note']}"
    return line


def main() -> int:
    print("=" * 76)
    print("用 gaffer 真跑产出的灯光方案 —— A/B")
    print("=" * 76)

    # ---- 1) 真调灯光师 ----
    from little_crew.agent.gaffer_llm import GafferLlm, normalize_lighting
    from little_crew.agent.llm import LlmClient
    from little_crew.agent.protocol import Budget, CallEnvelope

    g = GafferLlm(client=LlmClient("deepseek-flash", timeout_s=180))
    t0 = time.perf_counter()
    out = g(envelope=CallEnvelope.root(
        "gaffer", "lighting.plan", {"script.draft": SCRIPT},
        Budget.unlimited(), callee_kind="agent"), runtime=None)
    print(f"  灯光师 {time.perf_counter()-t0:.1f}s  ok={out.ok}")
    if not out.ok:
        print(f"    ❌ {getattr(out, 'error_message', '')}")
        return 1
    plan = out.output["lighting.plan"]
    print(f"  色板: {plan.get('palette')}")
    for s in plan["scenes"]:
        print(f"    {s['sceneId']}: {lighting_block(s)}")

    target = next((s for s in plan["scenes"]
                   if s["sceneId"] == TARGET_SCENE), None)
    if target is None:
        print(f"  ⚠️ 方案里没有 {TARGET_SCENE}，取第一场")
        target = plan["scenes"][0] if plan["scenes"] else None
    if target is None:
        print("  ❌ 灯光方案是空的")
        return 1

    block = lighting_block(target)
    print(f"\n  用「{target['sceneId']}」的光做 A/B：")
    print(f"    {block}")

    # ---- 2) 出两图 ----
    a_prompt = f"{SHOT}{STYLE}"
    b_prompt = (f"{SHOT}{STYLE}\n光线：{block}"
                + (f"。主色板：{'、'.join(plan['palette'])}"
                   if plan.get("palette") else ""))
    print()
    a = render(a_prompt, "gaffer_a_no_lighting")
    b = render(b_prompt, "gaffer_b_with_lighting")

    # ---- 3) 统计 ----
    sa, sb = stats(a), stats(b)
    print()
    print("=" * 76)
    print("  像素统计（同 seed，唯一差别是灯光块 —— 这一块是 LLM 写的）")
    print("=" * 76)
    result = {"lightingPlan": plan, "target": target, "block": block,
              "promptA": a_prompt, "promptB": b_prompt, "seed": SEED,
              "statsA": sa, "statsB": sb}
    if sa and sb:
        for k, cn in (("meanLuma", "平均亮度"), ("contrastStd", "对比(std)"),
                      ("warmBias", "色温 R-B")):
            d = sb[k] - sa[k]
            arrow = "↑" if d > 0 else ("↓" if d < 0 else "=")
            print(f"  {cn:<12} A {sa[k]:>8.2f}   B {sb[k]:>8.2f}   {arrow}{abs(d):.2f}")
        changed = (abs(sb["meanLuma"] - sa["meanLuma"]) > 3
                   or abs(sb["contrastStd"] - sa["contrastStd"]) > 3)
        print()
        print(f"  变化是否显著（亮度或对比 > 3）：{'是' if changed else '否'}")
        result["changed"] = changed
    print()
    print("  ⚠️ 代理指标只说「变了没有」。**「变得对不对」必须看图。**")

    (OUT / "gaffer-ab.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  证据写入 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
