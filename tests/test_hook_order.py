"""静态检查：Hook 不能被"提前 return"隔在后面。

## 为什么要有这条测试

用户报"设置页打不开 / 黑屏"。根因是我把一个 `useMemo` 写在了
`if (settings === null) return ...` **之后**：

  · 首次渲染 `settings` 还是 `null` → 走早返回 → 这个 hook **不执行**；
  · 设置拉回来后重渲染 → 走到它 → hook **执行**。

React 立刻抛：

    Rendered more hooks than during the previous render.

并**把整页渲染成黑屏**。

这个错误有两个恶劣之处：

  1. **整页黑屏**，不是局部报错 —— 用户完全不知道发生了什么；
  2. **只在"能拿到设置"时出现**（即已登录）。用未登录状态测页面
     根本碰不到它，因为那条路走的是早返回 —— 我就是这么漏掉的。

`tsc` 也查不出来（类型完全正确）。所以只能用静态检查守住。

## 做法

按行扫描组件函数体，找出**顶格的** `if (...) { return` / `if (...) return`
（早返回），然后要求所有顶格 hook 调用都出现在第一个早返回之前。

只认**顶格（缩进 2 空格）**的语句：`useMemo(() => { ... if (!x) return ... })`
里那种 return 是回调内部的，不是组件的早返回 —— 第一版没区分，误报了。
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
#: 检查这些文件（有"早返回 + 条件渲染"模式的组件）
TARGETS = (
    "web/src/pages/SettingsPage.tsx",
)

HOOK_RE = re.compile(r"^\s{2}(?:const|let)?\s*.*?\buse(State|Memo|Callback|Effect|Ref|Reducer|Context)\s*\(")
# 顶格的早返回：`  if (...) {` 开头，或 `  if (...) return`
EARLY_RETURN_RE = re.compile(r"^\s{2}if\s*\(.*$")


def _component_body(text: str) -> list[tuple[int, str]]:
    """取出组件函数体（从 `export default function` 到文件末尾）。"""
    lines = text.splitlines()
    start = 0
    for i, ln in enumerate(lines):
        if re.match(r"^export default function\s+\w+", ln):
            start = i
            break
    return [(i + 1, ln) for i, ln in enumerate(lines[start:], start=start)]


def test_no_hook_after_early_return():
    for rel in TARGETS:
        path = ROOT / rel
        assert path.is_file(), f"找不到 {rel}"
        body = _component_body(path.read_text(encoding="utf-8"))

        first_early: int | None = None
        i = 0
        while i < len(body):
            ln_no, ln = body[i]
            if first_early is None and EARLY_RETURN_RE.match(ln):
                # 确认它**真的是**早返回：本行或紧接的两行里出现 `return`
                window = "\n".join(x[1] for x in body[i:i + 3])
                if re.search(r"\breturn\b", window):
                    first_early = ln_no
            i += 1
        assert first_early is not None, f"{rel} 里没找到早返回（检查逻辑可能失效了）"

        offenders = [
            (ln_no, ln.strip()[:70])
            for ln_no, ln in body
            if HOOK_RE.match(ln) and ln_no > first_early
        ]
        assert not offenders, (
            f"{rel}: 这些 hook 写在第 {first_early} 行的早返回**之后**，"
            "会导致 hook 数量在两次渲染间变化 → React 抛 "
            '"Rendered more hooks than during the previous render." → **整页黑屏**。\n'
            "把它们移到所有早返回之前，或抽成模块级纯函数：\n  "
            + "\n  ".join(f"行{a}: {b}" for a, b in offenders)
        )
