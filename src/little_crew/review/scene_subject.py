"""**场景主体冲突**：下游（分镜）的自由发挥引入了上游（剧本）未授权的场景主体。

## 为什么需要它

2026-10-29 实测撞出来的真事：

```
剧本 [场1]：普罗旺斯·山中·午后  少女独自走在「山坡」上采花…
     ↓ 分镜师润色
提示词    ：「午后斜阳把整片「山坡」照成浅金色…薰衣草「花田」…」
     ↓ 模型照做
画面      ：金黄山坡占 2/3 + 紫花田挤在底部
```

**剧本只说了"山坡"，从没说过"花田"** —— 分镜师从"紫色花穗"推理出"花田"并写进提示词，
于是**两个主体并存**，模型只能都画，比例失衡。

> **这不是模型乱编，也不是白模的错 —— 是上下游意图在打架。**

## 这条属于哪一层

**协作层（治理）**，不是产物层。
它管的是「下游有没有越权改动上游的意图」—— 正是多智能体协作最典型的失效模式。

## 判据（刻意保守）

只认**场景名词表**里出现的词，不做语义理解 ——
宁可漏判，也不要在没把握时退回好镜头（这条教训来自 `set` 规则被三次否定）。
"""
from __future__ import annotations

from typing import Iterable, Mapping

__all__ = ["SCENE_NOUNS", "subjects_in", "check_scene_subject"]

#: 场景主体名词表。**只放可辨识的地点/地物**，不放形容词。
SCENE_NOUNS: dict[str, tuple[str, ...]] = {
    "山坡": ("山坡", "山丘", "斜坡", "土坡"),
    "花田": ("花田", "花海", "花垄", "薰衣草田", "花丛"),
    "木屋": ("木屋", "小屋", "屋内", "屋里", "室内"),
    "街道": ("街道", "街头", "巷子", "马路"),
    "海边": ("海边", "海滩", "沙滩", "海岸"),
    "森林": ("森林", "树林", "林子"),
    "岩石": ("岩石", "巨石", "崖"),
    "庭院": ("庭院", "院子", "花园"),
}


def subjects_in(text: str) -> set[str]:
    """文本里出现的场景主体（标准名集合）。"""
    got: set[str] = set()
    t = str(text or "")
    for canon, words in SCENE_NOUNS.items():
        if any(w in t for w in words):
            got.add(canon)
    return got


def check_scene_subject(
    script_text: str,
    prompt: str,
    *,
    allowed: Iterable[str] | None = None,
) -> dict:
    """比对「剧本场景主体」与「提示词场景主体」。

    返回::

        {"ok": bool, "verdict": "KEEP"|"RETRY",
         "script": [...], "prompt": [...], "intruded": [...],
         "reason": "..."}

    **RETRY 的条件（只有两条）**：
    1. 提示词引入了剧本**完全没有**的场景主体（越权新增）
    2. 剧本有主体、而提示词里**一个都没有**（丢掉场景）

    ⚠️ **两个主体同时出现但剧本里就有** → 不判（那是上游自己的问题，不是下游越权）。
    """
    s = subjects_in(script_text)
    p = subjects_in(prompt)
    allow = set(allowed) if allowed is not None else set()
    s |= allow

    intruded = sorted(p - s) if s else []
    lost = bool(s) and not p

    ok = not intruded and not lost
    if intruded:
        reason = ("提示词引入了剧本里没有的场景主体："
                  + "、".join(intruded)
                  + "（剧本只有：" + ("、".join(sorted(s)) or "无") + "）")
    elif lost:
        reason = ("剧本有场景主体（" + "、".join(sorted(s))
                  + "），但提示词里一个都没提到")
    else:
        reason = "场景主体一致"

    return {
        "ok": ok,
        "verdict": "KEEP" if ok else "RETRY",
        "script": sorted(s),
        "prompt": sorted(p),
        "intruded": intruded,
        "reason": reason,
    }
