"""视觉闸阈值标定：用**已有成片集的静帧**跑一遍，看判据怎么定才稳（2026-11-08）。

## 为什么不需要重跑渲染

`_FLR`(22) / `_FULL`(27) / `_V4`(23) 三集都已经有 `episode.master.json`
（含每镜 `prompt` 与产物名）—— 直接用它们的图跑视觉闸，**样本 72 镜**，
比"新跑 2 集"（约 40 分钟、16 镜）大得多也快得多。

## 要定的是什么

| 判据 | 现默认 | 问题 |
|---|---|---|
| `faces=broken` -> error（可重渲） | 直接判 error | 会不会误伤？ |
| `matches <= 2` -> warn | 阈值 2 | 命中比例多高？会不会变成噪声？ |

## 输出

- `raw.json`：每镜的 `{episode, shotId, faces, matches, blank}`
- 打印：matches 分布、各阈值命中率、faces 分布
"""
import json, statistics as st, sys
from pathlib import Path

P = Path(r"D:\AI\ds.8\Little_Crew")
sys.path.insert(0, str(P / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from little_crew.agent.llm import LlmClient
from little_crew.review import vision_review as VR

EPISODES = ("_FLR", "_FULL", "_V4")


class _Shot:
    def __init__(self, d):
        self.shot_id = d["shotId"]
        self.prompt = str(d.get("prompt") or "")


def main() -> int:
    client = LlmClient("deepseek-flash")
    all_rows, raw = [], []
    for tag in EPISODES:
        wd = P / "outputs" / tag
        mf = wd / "delivery" / "episode.master.json"
        if not mf.is_file():
            print(f"  跳过 {tag}（没有 master）")
            continue
        shots_raw = json.loads(mf.read_text(encoding="utf-8")).get("shots") or []
        imgs, shots = {}, []
        for s in shots_raw:
            name = (s.get("artifacts") or [""])[0]
            hit = list(wd.rglob(name)) if name else []
            if hit:
                imgs[s["shotId"]] = str(hit[0])
            shots.append(_Shot(s))
        r = VR.review(shots, imgs, client=client, max_shots=0)
        print(f"  {tag}: 审 {r['reviewedCount']}/{r['total']} · 错误 {r['errors']}")
        for row in r["rows"]:
            if row.get("reviewed"):
                all_rows.append({**row, "episode": tag})
        raw.append({"episode": tag, "result": r})

    out = Path(__file__).parent / "raw.json"
    out.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    ms = [int(r["matches"]) for r in all_rows if r.get("matches") is not None]
    faces: dict[str, int] = {}
    for r in all_rows:
        faces[str(r.get("faces"))] = faces.get(str(r.get("faces")), 0) + 1
    blank = sum(1 for r in all_rows if r.get("blank"))

    print(f"\n  === 样本 {len(all_rows)} 镜 ===")
    print(f"  matches: 中位 {st.median(ms):.0f} · 均值 {st.mean(ms):.2f} · "
          f"标准差 {st.pstdev(ms):.2f}")
    print(f"  分布: {dict(sorted({v: ms.count(v) for v in set(ms)}.items()))}")
    print(f"  faces: {faces} · blank: {blank}")
    for th in (1, 2, 3):
        hit = sum(1 for x in ms if x <= th)
        print(f"  阈值 matches<={th}: {hit}/{len(ms)} = {hit/len(ms)*100:.0f}%")

    n = len(all_rows)
    print(f"\n  === 结论 ===")
    print(f"  · faces=broken {faces.get('broken', 0)}/{n} "
          f"= {faces.get('broken', 0)/max(1,n)*100:.0f}%（可自动重渲的量级）")
    for th in (1, 2, 3):
        hit = sum(1 for x in ms if x <= th)
        verdict = "太多，当 error 会乱重渲" if hit / max(1, len(ms)) > 0.35 else "可接受"
        print(f"  · matches<={th} 命中 {hit/n*100:.0f}% -> {verdict}" if n else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
