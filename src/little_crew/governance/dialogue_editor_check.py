"""对白编辑自检：断句 · 停顿 · 估算 · 建议。"""
from __future__ import annotations

import sys

from little_crew.governance.dialogue_editor import (
    ONSET_SECONDS, PER_CHAR_SECONDS, advise, estimate, estimate_segment,
    segments, speech_plan,
)

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("对白编辑（断句 / 停顿 / 估算 / 建议）—— 自检")
    print("=" * 66)

    print(); print("A. 断句")
    s = segments("他是外乡人。你留他，村里人怎么看你。")
    check("A1 ★★ 句号处必切", len(s) == 2, str([x["text"] for x in s]))
    check("A2 ★★★ 短句里的逗号不切（避免碎成词）",
          len(segments("我跟你走，你别拦我。")) == 1,
          str([x["text"] for x in segments("我跟你走，你别拦我。")]))
    check("A3 ★★ 长句里的逗号会切",
          len(segments("她说，那天雨很大，路很滑，她还是去了，因为信上写着明天就到。")) >= 2,
          str(len(segments("她说，那天雨很大，路很滑，她还是去了，因为信上写着明天就到。"))))
    check("A4 空串返回空", segments("") == [], "")
    check("A5 ★ 纯标点不崩", segments("。。。") != [], str(segments("。。。")))

    print(); print("B. 停顿")
    s2 = segments("走。别回头。")
    check("B1 ★★ 中间段有时长、末段为 0",
          s2[0]["pause"] > 0 and s2[-1]["pause"] == 0.0, str(s2))
    check("B2 ★ 问号也有停顿",
          segments("你去吗？我去。")[0]["pause"] > 0, str(segments("你去吗？我去。")))
    check("B3 省略号停得更久",
          segments("他……走了。")[0]["pause"] >= 0.4, str(segments("他……走了。")))

    print(); print("C. 估算与建议")
    e = estimate(segments("他是外乡人。你留他，村里人怎么看你。"))
    check("C1 ★★ 估算为正且随字数增长",
          e > 0 and estimate(segments("我走。" * 10)) > e, f"{e}")
    # 2026-11-06 用 8 句真合成标定：时长 ≈ 1.08 + 0.094 × 字数
    check("C2 ★★★ 时长模型带「起音」，不是纯字数除法",
          0.5 <= ONSET_SECONDS <= 2.0 and 0.05 <= PER_CHAR_SECONDS <= 0.2,
          f"{ONSET_SECONDS} / {PER_CHAR_SECONDS}")
    check("C2b ★★★ 短句不再被低估（「走。」应 > 1s）",
          estimate_segment("走。") > 1.0, str(estimate_segment("走。")))
    check("C2c ★★ 标定误差在容差内（对照实测 1.20/1.60/2.16）",
          all(abs(estimate_segment(s) - r) < 0.25
              for s, r in (("走。", 1.20), ("他是外乡人。", 1.60),
                           ("你留他，村里人怎么看你。", 2.16))), "")
    a = advise("走。", 5.0)
    check("C3 ★★ 明显短于镜头 -> 提示留白/加停顿",
          "留白" in a["advice"] or "短" in a["advice"], a["advice"])
    a2 = advise("他" * 40 + "。", 2.0)
    check("C4 ★★★ 明显长于镜头 -> split",
          "split" in a2["advice"], a2["advice"])
    # 「我走。」标定约 1.27s：镜头 1.5s 应在容差内 -> ok / tight
    a3 = advise("我走。", 1.5)
    check("C5 ★★ 接近时给 ok 或 tight", "ok" in a3["advice"] or "tight" in a3["advice"],
          a3["advice"])
    # 镜头 2.2s 差 0.9s，必须提示「短于镜头」——这条防的是「容差被写太宽」
    a4 = advise("我走。", 2.2)
    check("C5b ★★★ 明显短于镜头时不许说 ok", "短于镜头" in a4["advice"], a4["advice"])
    check("C6 没给镜头时长不崩", advise("走。", 0)["advice"] != "", "")

    print(); print("D. 完整计划")
    p = speech_plan("他是外乡人。你留他，村里人怎么看你。", 4.0)
    check("D1 ★★ 计划含 segments / estimate / advice",
          p["kind"] == "dialogue.plan" and len(p["segments"]) == 2
          and p["estimate"] > 0 and p["advice"]["advice"], "")
    check("D2 总停顿 = 各段之和",
          abs(p["totalPause"] - sum(s["pause"] for s in p["segments"])) < 1e-6, "")

    n = sum(1 for _, ok, _ in _r if ok)
    print()
    for name, ok, detail in _r:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 66)
    print(f"结果：{n}/{len(_r)} 通过")
    return 0 if n == len(_r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
