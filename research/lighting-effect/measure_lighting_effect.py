"""**灯光方案到底改没改画面？** —— A/B 实测。

## 为什么必须量，不能看着像

前三轮验的都是"**接线对不对**"（plan 有没有传过去、顺序对不对）。
但那是**机制**，不是**效果**。

一个"接线全对但画面没变"的结果完全可能：提示词里加了灯光描述，
而模型忽略了它。**这种情况下所有测试都是绿的，而功能是空的。**

## 怎么做

**只改一个变量**：同一镜描述、同一 seed、同一分辨率 ——
唯一差别是提示词里有没有那段灯光描述。

| 组 | 提示词 |
|---|---|
| A（对照） | 镜头描述 + 外观 |
| B（有灯光） | 镜头描述 + 外观 + **灯光块** |

## 量什么（不用视觉模型，纯像素统计）

| 指标 | 含义 | 灯光"该"带来的方向 |
|---|---|---|
| 平均亮度 | 整体明暗 | 「夜」应该**更低** |
| 对比（std） | 影调硬度 | 「高对比」应该**更高** |
| 色温代理（R−B） | 冷暖 | 「冷白」应该**更低** |

⚠️ **这些是代理指标，不是严格的色彩科学。** 它们能回答"变了没有、
往哪个方向变"，不能回答"变得对不对" —— 后者要人看。
所以本脚本同时落盘两图，供人眼复核。
"""
from __future__ import annotations

import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, r"D:\AI\ds.8\Little_Crew\src")
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

BASE = "http://127.0.0.1:8100"
OUT = pathlib.Path(r"D:\AI\ds.8\Little_Crew\research\lighting-effect")
OUT.mkdir(parents=True, exist_ok=True)

#: 固定的镜头描述 —— 两组的**唯一差别**是后面挂不挂灯光块
SHOT = ("中景。少女站在薰衣草田边，穿浅蓝粗布连衣裙，背着一只竹篮，"
        "抬头望向远处，神情有些不安。")
STYLE = "日式动画风格，胶片质感。"

#: 灯光块（模拟 `lighting.plan` 被写进提示词的样子）
LIGHTING = ("夜；窗在左侧，硬光斜切；冷白；影调 高；"
            "只照亮她的手与竹篮，其余沉在暗里。主色板：靛蓝、灰白。")

SEED = 4242
W, H = 640, 384


def _post(url: str, body: dict, timeout: int = 300) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def render(prompt: str, tag: str) -> pathlib.Path:
    body = {"model": "z-image-turbo", "prompt": prompt,
            "negative_prompt": "", "width": W, "height": H,
            "num_inference_steps": 8, "seed": SEED}
    t0 = time.perf_counter()
    d = _post(f"{BASE}/v1/images/generations", body)
    url = (d.get("data") or [{}])[0].get("url") or ""
    if not url:
        raise RuntimeError(f"没有产物 url: {d}")
    dst = OUT / f"{tag}.png"
    with urllib.request.urlopen(url, timeout=300) as r:
        dst.write_bytes(r.read())
    print(f"    ✅ {tag}  {time.perf_counter()-t0:.1f}s  "
          f"{dst.stat().st_size//1024} KB  (prompt {len(prompt)} 字)")
    return dst


def stats(path: pathlib.Path) -> dict:
    """纯像素统计：亮度 / 对比 / 色温代理。**不依赖第三方库**。"""
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
    r_ = sum(p[0] for p in px) / n
    b_ = sum(p[2] for p in px) / n
    return {
        "meanLuma": round(mean, 2),
        "contrastStd": round(var ** 0.5, 2),
        "warmBias": round(r_ - b_, 2),     # 越大越暖
    }


def main() -> int:
    print("=" * 76)
    print("灯光方案有没有改动画面 —— A/B")
    print("=" * 76)
    try:
        h = json.loads(urllib.request.urlopen(f"{BASE}/health", timeout=8).read())
        print(f"  local-gen loaded={h.get('loaded_model')}")
    except Exception as e:                                    # noqa: BLE001
        print(f"  ❌ 服务不可用: {e}")
        return 1

    a_prompt = f"{SHOT}{STYLE}"
    b_prompt = f"{SHOT}{STYLE}\n光线：{LIGHTING}"
    print(f"\n  A 对照（{len(a_prompt)} 字）")
    a = render(a_prompt, "a_no_lighting")
    print(f"\n  B 有灯光（{len(b_prompt)} 字）")
    b = render(b_prompt, "b_with_lighting")

    sa, sb = stats(a), stats(b)
    print()
    print("=" * 76)
    print("  像素统计（同 seed，唯一差别是灯光块）")
    print("=" * 76)
    if not sa or not sb:
        print("  ⚠️ 没有 PIL，跳过统计（图已落盘，可用人眼复核）")
    else:
        print(f"  {'指标':<14} {'A 对照':>10} {'B 有灯光':>10} {'变化':>10}")
        names = {"meanLuma": "平均亮度", "contrastStd": "对比(std)",
                 "warmBias": "色温 R-B"}
        for k, cn in names.items():
            d = sb[k] - sa[k]
            arrow = "↑" if d > 0 else ("↓" if d < 0 else "=")
            print(f"  {cn:<14} {sa[k]:>10.2f} {sb[k]:>10.2f} {arrow}{abs(d):>9.2f}")
        print()
        # 期望方向：夜 -> 变暗；高对比 -> 对比升高；冷白 -> R-B 下降
        dark_ok = sb["meanLuma"] < sa["meanLuma"]
        contrast_ok = sb["contrastStd"] > sa["contrastStd"]
        cool_ok = sb["warmBias"] < sa["warmBias"]
        hits = sum([dark_ok, contrast_ok, cool_ok])
        print(f"  方向正确项：{hits}/3  "
              f"(变暗 {'✓' if dark_ok else '✗'} / "
              f"对比升 {'✓' if contrast_ok else '✗'} / "
              f"转冷 {'✓' if cool_ok else '✗'})")
        print()
        if hits >= 2:
            print("  ✅ **灯光描述确实改动了画面，且方向大体正确**")
        else:
            print("  ⚠️ 方向不符 —— 可能是代理指标不适用，也可能是模型忽略了灯光。")
            print("      **看图判断**：两图并排看，光是否真的不一样。")

    (OUT / "ab.json").write_text(json.dumps({
        "shot": SHOT, "style": STYLE, "lighting": LIGHTING, "seed": SEED,
        "size": [W, H],
        "promptA": a_prompt, "promptB": b_prompt,
        "statsA": sa, "statsB": sb,
        "note": ("代理指标：亮度/对比/色温。能回答「变了没有、往哪变」，"
                 "不能回答「变得对不对」—— 后者要人看图。"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  证据写入 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
