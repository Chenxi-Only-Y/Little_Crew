"""provider 指纹自检 —— 接云 API 前的可证明性基础设施。"""
from __future__ import annotations

import sys

from little_crew.governance.provider_fingerprint import (
    make, note, note_reply, reset, summary, taken,
)

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("provider 指纹 —— 自检")
    print("=" * 66)

    print(); print("A. 一条指纹里有什么")
    r = make(kind="llm", provider="deepseek", model="deepseek-chat",
             served_model="deepseek-flash", params={"temperature": 0.7},
             request="写一个分镜")
    check("A1 ★★ 记下请求 hash（不是明文）", r["requestHash"].startswith("sha256:"), r["requestHash"][:20])
    check("A2 ★★★ 记录里不含明文请求", "写一个分镜" not in str(r), str(r)[:80])
    check("A3 ★★★ servedModel 与 model 分开记",
          r["servedModel"] == "deepseek-flash" and r["model"] == "deepseek-chat", "")
    check("A4 同请求同 hash（可对账）",
          make(kind="llm", request="abc")["requestHash"]
          == make(kind="llm", request="abc")["requestHash"], "")
    check("A5 不同请求不同 hash",
          make(kind="llm", request="abc")["requestHash"]
          != make(kind="llm", request="abd")["requestHash"], "")

    print(); print("B. 汇总")
    reset()
    note_reply(kind="llm.json", provider="deepseek", model="deepseek-chat",
               served_model="deepseek-flash", request="a")
    note_reply(kind="llm", provider="deepseek", model="deepseek-chat",
               served_model="deepseek-flash", request="b")
    note_reply(kind="image", provider="local", model="zimage", request="c")
    s = summary()
    check("B1 ★★ 按 provider+model 聚合", s["total"] == 3 and len(s["providers"]) == 2,
          str([(p["provider"], p["model"], p["calls"]) for p in s["providers"]]))
    ds = next(p for p in s["providers"] if p["model"] == "deepseek-chat")
    check("B2 ★★ 同一模型两次调用合成一行（calls=2）", ds["calls"] == 2, str(ds["calls"]))
    check("B3 ★★★ 记下「服务端实际模型」分布",
          ds["servedModels"].get("deepseek-flash") == 2, str(ds["servedModels"]))
    check("B4 ★★ 按 kind 分类计数",
          ds["kinds"].get("llm.json") == 1 and ds["kinds"].get("llm") == 1, str(ds["kinds"]))
    check("B5 ★★★ 缺 servedModel 要计数（不静默）",
          summary([{"provider": "x", "model": "y", "servedModel": ""}])["missingServedModel"] == 1, "")

    print(); print("C. 记录器行为")
    reset()
    check("C1 清空后为空", taken() == [], "")
    note(make(kind="llm", request="z"))
    check("C2 note 后能取回", len(taken()) == 1, "")
    got = taken()
    got.append({"hack": 1})
    check("C3 ★★ taken() 返回副本（外部改不到内部）", len(taken()) == 1, str(len(taken())))
    check("C4 空汇总不崩", summary([])["total"] == 0, "")
    reset()

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
