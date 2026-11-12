"""渲染并发策略自检（2026-11-12）。

锁住一件事：**本地永远串行**（不接受环境变量覆盖），云端才读开关。
"""
from __future__ import annotations

import sys

from little_crew.runtime.render_policy import (
    ENV_PARALLEL,
    explain,
    is_local,
    render_workers,
)

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


class _B:
    def __init__(self, endpoint="", local=False):
        self.endpoint = endpoint
        self.is_local_by_default = local


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("渲染并发策略（本地串行 / 云端可开）—— 自检")
    print("=" * 66)

    print(); print("A. 本地判定")
    check("A1 ★★★ 回环地址算本地", is_local(_B("http://127.0.0.1:8188")), "")
    check("A2 ★★★ localhost 也算本地", is_local(_B("http://localhost:8100")), "")
    check("A3 ★★ 声明 is_local_by_default 算本地", is_local(_B("", local=True)), "")
    check("A4 ★★ 公网地址不算本地", not is_local(_B("https://api.minimaxi.com")), "")
    check("A5 ★★ None 视为本地（保守）", is_local(None), "")

    print(); print("B. 并发度：本地恒为 1")
    for n in ("1", "2", "4", "999"):
        w = render_workers(_B("http://127.0.0.1:8188"), {ENV_PARALLEL: n})
        check(f"B·{n} ★★★ 本地即使设 {n} 也还是 1", w == 1, str(w))

    print(); print("C. 并发度：云端才读开关")
    cloud = _B("https://api.example.com")
    check("C1 ★★★ 云端默认 1（串行）", render_workers(cloud, {}) == 1, "")
    check("C2 ★★★ 云端设 2 -> 2", render_workers(cloud, {ENV_PARALLEL: "2"}) == 2, "")
    check("C3 ★★★ 云端设 99 -> 上限 4（不无限开）",
          render_workers(cloud, {ENV_PARALLEL: "99"}) == 4, "")
    check("C4 ★★ 非法值 -> 1（不炸）", render_workers(cloud, {ENV_PARALLEL: "abc"}) == 1, "")
    check("C5 ★★ 0 或负数 -> 1", render_workers(cloud, {ENV_PARALLEL: "0"}) == 1, "")

    print(); print("D. 说明文案（要能进启动日志）")
    check("D1 ★★★ 本地文案写明「实测 1.15×，不值得」",
          "1.15" in explain(_B("http://127.0.0.1:8188")), explain(_B("http://127.0.0.1:8188")))
    check("D2 ★★★ 云端文案必须带「未实测」（诚实标注）",
          "未实测" in explain(cloud, {ENV_PARALLEL: "2"}),
          explain(cloud, {ENV_PARALLEL: "2"}))

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
