"""汇总剧本闭环的逐轮数据 → E3 的正式表。

## 数据来源（两类）

1. **新格式**：`outputs/*/delivery/script.rounds.json` —— 带每轮 issues 文本（2026-11-01 起）
2. **旧格式**：`research/retry-cost/raw-runs.json` —— 只有分数，没有原因（10 次）

## 要回答的问题

> **是返工没用，还是退回指令写得差？**

指标：**「退回时给了可执行建议」的比例**，与**下一轮分数变化**的关系。

## 注意

「通过」不等于「没问题」：实测 68.5 分 PASS 的那轮仍带着 4 条 MAJOR issues。
所以"最佳轮"与"是否 PASS"要分开看。
"""
from __future__ import annotations

import glob
import json
import re
import sys
from pathlib import Path

ROOT = Path(r"D:\AI\ds.8\Little_Crew")
HAS_SUGGESTION = re.compile(r"suggestion='([^']+)'")


def _suggestion_ratio(issues) -> float | None:
    """issues 里「带非空 suggestion」的比例。**没有 issues 返回 None**。"""
    items = [str(x) for x in (issues or ())]
    if not items:
        return None
    good = 0
    for s in items:
        m = HAS_SUGGESTION.search(s)
        if m and m.group(1).strip():
            good += 1
    return good / len(items)


def load_new() -> list[dict]:
    """**优先扫 `research/script-rounds/`**（不会被清理），再补 `outputs/`。"""
    rows = []
    seen = set()
    files = sorted(glob.glob(str(ROOT / "research" / "script-rounds" / "*.json")))
    files += sorted(glob.glob(str(ROOT / "outputs" / "*" / "delivery" / "script.rounds.json")))
    for f in files:
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:                                    # noqa: BLE001
            continue
        rounds = d.get("rounds") or []
        if not rounds:
            continue
        ep = d.get("episodeId") or Path(f).parts[-3]
        if ep in seen:
            continue
        seen.add(ep)
        rows.append({"episode": ep, "kind": "new", "rounds": rounds,
                     "accepted": d.get("accepted")})
    return rows


def load_old() -> list[dict]:
    p = ROOT / "research" / "retry-cost" / "raw-runs.json"
    if not p.is_file():
        return []
    d = json.loads(p.read_text(encoding="utf-8"))
    return [{"episode": f"legacy-{r.get('run')}", "kind": "old",
             "rounds": [{"verdict": x.get("verdict"), "total": x.get("total")}
                        for x in (r.get("detail") or [])],
             "accepted": r.get("accepted")} for r in (d.get("runs") or [])]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")             # type: ignore[attr-defined]
    new, old = load_new(), load_old()
    allr = new + old

    print(f"  新格式（有原因）：{len(new)} 集")
    print(f"  旧格式（无原因）：{len(old)} 次")
    print(f"  合计：{len(allr)} 集")
    print()

    lines, deltas_with, deltas_without = [], [], []
    for e in allr:
        rs = e["rounds"]
        print(f"  {e['episode']:22s} {len(rs)} 轮  "
              + " -> ".join(str(r.get("total")) for r in rs)
              + ("  ✅" if e.get("accepted") else ""))
        for i, r in enumerate(rs[:-1]):
            cur, nxt = r.get("total"), rs[i + 1].get("total")
            if cur is None or nxt is None:
                continue
            ratio = _suggestion_ratio(r.get("issues"))
            d = nxt - cur
            row = {"episode": e["episode"], "round": i + 1, "from": cur,
                   "to": nxt, "delta": round(d, 2),
                   "suggestionRatio": None if ratio is None else round(ratio, 2)}
            lines.append(row)
            if ratio is None:
                continue
            (deltas_with if ratio > 0 else deltas_without).append(d)

    print()
    if lines:
        print("  返工对（上一轮 → 下一轮）")
        print(f"    {'集':22s} {'轮':>3s} {'分':>7s} → {'分':>7s} {'Δ':>7s}  建议比")
        for r in lines:
            sr = "—" if r["suggestionRatio"] is None else f"{r['suggestionRatio']:.2f}"
            print(f"    {r['episode']:22s} {r['round']:3d} {r['from']:7.1f} → "
                  f"{r['to']:7.1f} {r['delta']:+7.2f}  {sr}")

    print()
    def avg(xs):
        return f"{sum(xs)/len(xs):+.2f}（n={len(xs)}）" if xs else "—"
    print("  ★ 按「退回时是否给了可执行建议」分组：")
    print(f"    有建议 → 下一轮平均变化：{avg(deltas_with)}")
    print(f"    无建议 → 下一轮平均变化：{avg(deltas_without)}")
    if not (deltas_with or deltas_without):
        print("    （样本还不够：先跑几集，让 script.rounds.json 累积起来）")

    outp = ROOT / "research" / "retry-cost" / "TABLE-v2.md"
    outp.write_text(
        "# E3 返工数据（v2：带退回原因）\n\n"
        f"- 新格式（有原因）：{len(new)} 集\n- 旧格式（无原因）：{len(old)} 次\n"
        f"- 有建议组平均变化：{avg(deltas_with)}\n- 无建议组平均变化：{avg(deltas_without)}\n\n"
        + "\n".join(f"| {r['episode']} | {r['round']} | {r['from']} | {r['to']} | {r['delta']:+.2f} | "
                    f"{'—' if r['suggestionRatio'] is None else r['suggestionRatio']} |"
                    for r in lines)
        + "\n", encoding="utf-8")
    print(f"\n  写出：{outp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
