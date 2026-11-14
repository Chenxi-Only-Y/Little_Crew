"""真实 LLM 剧组绑定层自检（2026-11-14，A1 续）。

锁住五件事：
1 `llm=False`：**不动 stubs**（默认规则桩，不花钱）
2 `llm=True`：编剧/剧评人/分镜师/审片被换成真实实现；**分镜师必须成对换**
3 **跨族纪律**：编剧与剧评人同模型 -> 明确失败（不让门禁退化成自我签收）
4 密钥/模型缺失 -> 明确失败（不静默回退规则桩）
5 摘要行**不含密钥明文**（只有掩码）
"""
from __future__ import annotations

import sys

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("真实 LLM 剧组绑定（llm_crew）")
    print("=" * 66)

    from little_crew.agent.crew_stubs import default_crew_stubs
    from little_crew.runtime import llm_crew as LC

    print(); print("A. 默认不换（不花钱）")
    stubs = default_crew_stubs()
    before = dict(stubs)
    info = LC.bind_llm_crew(stubs, llm=False)
    check("A1 ★★★ llm=False 不改 stubs", stubs == before and not info["bound"], "")
    check("A2 ★★ llm=False 返回 ok 且无错误", info["ok"] and not info["error"], "")

    print(); print("B. 换真实实现")
    stubs2 = default_crew_stubs()
    info2 = LC.bind_llm_crew(stubs2, llm=True)
    check("B1 ★★★ 核心三职位被替换",
          all(k in info2["bound"] for k in
              ("screenwriter", "dramaturge", "storyboard_artist")), str(info2["bound"]))
    check("B2 ★★★ 分镜师成对换（否则 LLM 剧本读成 0 镜）",
          "storyboard_artist" in info2["bound"], "")
    check("B3 ★★ 审片也用真实审核方", "review_board" in info2["bound"], "")
    check("B4 ★★ 返回的客户端可打印摘要", bool(info2.get("writer", {}).get("displayName")), "")

    print(); print("C. 纪律与失败语义")
    stubs3 = default_crew_stubs()
    info3 = LC.bind_llm_crew(stubs3, llm=True,
                             llm_model="deepseek-flash", llm_reviewer="deepseek-flash")
    check("C1 ★★★ 编剧与剧评人同模型 -> 明确失败（跨族纪律）",
          info3["ok"] is False and "跨模型族" in info3["error"], info3["error"][:70])
    info4 = LC.bind_llm_crew(default_crew_stubs(), llm=True,
                             llm_model="no-such-llm-model")
    check("C2 ★★★ 未注册模型 -> 明确失败（不静默回退桩）",
          info4["ok"] is False and "无法创建 LLM 客户端" in info4["error"],
          info4["error"][:70])

    print(); print("D. 摘要不泄露密钥")
    lines = LC.describe_binding(info2)
    text = " | ".join(lines)
    import os

    raw_key = os.environ.get("LLM_API_KEY") or ""
    check("D1 ★★★ 摘要行有模型名", "deepseek" in text or "编剧" in text, text[:80])
    check("D2 ★★★ 摘要不含完整密钥", (not raw_key) or (raw_key not in text), "")
    check("D3 ★★★ 密钥是掩码形式（含 …）",
          ("…" in text) or (not raw_key), text[:100])

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
