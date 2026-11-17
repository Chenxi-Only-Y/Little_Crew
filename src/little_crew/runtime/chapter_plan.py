"""章节契约与总导演上下文（2026-11-17，0.6.0 系列层）。

总导演（Showrunner）是**系列层角色**，不进单集 19 职位名册：
- 它读：logline + 卷名 + 章名 + 三字段章卡（目标/冲突/钩子）+ 边界契约
- 它**永远不读章节正文**（`assert_no_script` 守门）
- 它产出：`chapter_plan`（章卡/契约/依赖）、`boundary_report`
- 子导演（现有 19 职位里的导演）只读：本章 brief（entry/exit + 前章摘要）+ 本章剧本
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

__all__ = [
    "CARD_FIELDS", "CONTRACT_FIELDS", "PLAN_SCHEMA",
    "validate_plan", "topo_order", "showrunner_context", "assert_no_script",
    "build_brief", "contract_diff", "plan_summary",
]

CARD_FIELDS = ("goal", "conflict", "hook")
CONTRACT_FIELDS = ("placeKey", "time", "characters", "props", "openThreads")
PLAN_SCHEMA = "little_crew.chapter_plan/v1"

_TEXT_KEYS = {"text", "script", "body", "fulltext", "fullText", "content",
              "scriptText", "rawText", "manuscript"}


def assert_no_script(obj: Any, *, path: str = "") -> None:
    """守门：总导演上下文里**不允许出现整章正文**。"""
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            ks = str(k)
            if ks.lower() in {x.lower() for x in _TEXT_KEYS}:
                # chapter card 的 goal/conflict/hook 是允许的；正文 key 一律拒绝
                raise ValueError(f"总导演上下文禁止包含正文/剧本字段：{path}{ks}")
            assert_no_script(v, path=f"{path}{ks}.")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            assert_no_script(v, path=f"{path}[{i}].")


def _chapters(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(x) for x in (plan.get("chapters") or ())
            if isinstance(x, Mapping)]


def validate_plan(plan: Mapping[str, Any]) -> list[str]:
    """校验 chapter_plan；返回错误列表（空=通过）。"""
    errs: list[str] = []
    if not isinstance(plan, Mapping):
        return ["plan 不是对象"]
    if str(plan.get("schema") or "") and plan.get("schema") != PLAN_SCHEMA:
        errs.append(f"schema 不是 {PLAN_SCHEMA}")
    if not str(plan.get("logline") or "").strip():
        errs.append("缺 logline")
    chapters = _chapters(plan)
    if not chapters:
        errs.append("缺 chapters")
        return errs
    ids = [str(c.get("chapterId") or "") for c in chapters]
    if any(not x for x in ids):
        errs.append("有章缺 chapterId")
    if len(set(ids)) != len(ids):
        errs.append("chapterId 重复")
    for c in chapters:
        cid = str(c.get("chapterId") or "?")
        name = str(c.get("name") or "").strip()
        if not name:
            errs.append(f"{cid}: 缺章名")
        card = c.get("card") if isinstance(c.get("card"), Mapping) else {}
        for f in CARD_FIELDS:
            if not str(card.get(f) or "").strip():
                errs.append(f"{cid}: 章卡缺 {f}")
            elif len(str(card.get(f))) > 160:
                errs.append(f"{cid}: 章卡 {f} 超 160 字")
        _check_contract(errs, cid, "entry", c.get("entry"))
        _check_contract(errs, cid, "exit", c.get("exit"))
        deps = [str(x) for x in (c.get("dependsOn") or ())]
        for d in deps:
            if d not in ids:
                errs.append(f"{cid}: dependsOn 指向不存在的 {d}")
        if cid in deps:
            errs.append(f"{cid}: 依赖自己")
        try:
            assert_no_script({"card": card, "entry": c.get("entry"),
                              "exit": c.get("exit"), "name": name})
        except ValueError as e:
            errs.append(f"{cid}: {e}")
    # 顺序/依赖环
    try:
        topo_order(plan)
    except ValueError as e:
        errs.append(str(e))
    return errs


def _check_contract(errs: list[str], cid: str, kind: str, obj: Any) -> None:
    if not isinstance(obj, Mapping):
        errs.append(f"{cid}: 缺 {kind} 契约")
        return
    for f in CONTRACT_FIELDS:
        if f not in obj:
            errs.append(f"{cid}: {kind} 缺字段 {f}")


def _blocks(chapters: Sequence[Mapping[str, Any]]) -> dict[str, set[str]]:
    ids = [str(c.get("chapterId") or "") for c in chapters]
    return {str(c.get("chapterId") or ""):
            {str(d) for d in (c.get("dependsOn") or ()) if str(d) in ids}
            for c in chapters}


def topo_order(plan: Mapping[str, Any]) -> list[str]:
    """依赖拓扑序；有环/未知依赖抛 ValueError。"""
    chapters = _chapters(plan)
    ids = [str(c.get("chapterId") or "") for c in chapters]
    blocks = _blocks(chapters)
    out: list[str] = []
    done: set[str] = set()
    while len(out) < len(ids):
        ready = [i for i in ids if i not in done and blocks[i] <= done]
        if not ready:
            raise ValueError("章节依赖有环（或依赖不存在）")
        for i in ready:
            if i not in done:
                done.add(i)
                out.append(i)
    return out


def showrunner_context(plan: Mapping[str, Any], *,
                       chapter_ids: Sequence[str] | None = None) -> dict[str, Any]:
    """总导演唯一可读上下文：**章名+章卡+契约**，无正文。"""
    chapters = []
    want = set(chapter_ids) if chapter_ids else None
    for c in _chapters(plan):
        cid = str(c.get("chapterId") or "")
        if want is not None and cid not in want:
            continue
        chapters.append({
            "chapterId": cid,
            "name": str(c.get("name") or ""),
            "card": {f: str((c.get("card") or {}).get(f) or "") for f in CARD_FIELDS},
            "entry": dict(c.get("entry") or {}),
            "exit": dict(c.get("exit") or {}),
            "dependsOn": list(c.get("dependsOn") or ()),
        })
    ctx = {"logline": str(plan.get("logline") or ""),
           "volumes": [dict(v) for v in (plan.get("volumes") or ())
                       if isinstance(v, Mapping)],
           "chapters": chapters}
    assert_no_script(ctx)      # 守门：这里绝不允许出现 text/script 字段
    return ctx


def build_brief(plan: Mapping[str, Any], chapter_id: str) -> dict[str, Any]:
    """子导演 brief：本章 entry/exit + 章卡 + 上一章摘要（章卡级，不是正文）。"""
    chapters = {str(c.get("chapterId") or ""): c for c in _chapters(plan)}
    cur = chapters.get(str(chapter_id))
    if not cur:
        raise KeyError(f"没有章节 {chapter_id}")
    deps = [str(x) for x in (cur.get("dependsOn") or ())]
    prev = None
    if deps:
        prev = chapters.get(deps[0])
    return {
        "chapterId": chapter_id,
        "name": str(cur.get("name") or ""),
        "card": dict(cur.get("card") or {}),
        "entry": dict(cur.get("entry") or {}),
        "exit": dict(cur.get("exit") or {}),
        "prevRecap": ({"chapterId": str(prev.get("chapterId") or ""),
                       "name": str(prev.get("name") or ""),
                       "card": dict(prev.get("card") or {}),
                       "exit": dict(prev.get("exit") or {})}
                      if prev else None),
    }


def contract_diff(prev_exit: Any, cur_entry: Any) -> list[str]:
    """上一章 exit vs 本章 entry 的硬差异（F-CONT）。"""
    pe = dict(prev_exit or {}) if isinstance(prev_exit, Mapping) else {}
    ce = dict(cur_entry or {}) if isinstance(cur_entry, Mapping) else {}
    out: list[str] = []
    for f in ("placeKey", "time"):
        a, b = pe.get(f), ce.get(f)
        if a is not None and b is not None and str(a) != str(b):
            out.append(f"{f}: 上一章 exit={a!r} ≠ 本章 entry={b!r}")
    # 人物/道具/悬念：集合比较（允许本章新增，不允许无声消失）
    for f in ("characters", "props", "openThreads"):
        a = pe.get(f)
        b = ce.get(f)
        if a is None or b is None:
            continue
        sa = {str(x) for x in a} if isinstance(a, (list, tuple, set)) else {str(a)}
        sb = {str(x) for x in b} if isinstance(b, (list, tuple, set)) else {str(b)}
        missing = sa - sb
        if missing:
            out.append(f"{f}: 上一章有 {sorted(missing)}，本章 entry 消失")
    return out


def plan_summary(plan: Mapping[str, Any]) -> dict[str, Any]:
    chapters = _chapters(plan)
    return {"schema": PLAN_SCHEMA,
            "logline": str(plan.get("logline") or ""),
            "volumes": len(plan.get("volumes") or ()),
            "chapters": len(chapters),
            "order": topo_order(plan) if chapters else [],
            "tierBudget": plan.get("tierBudget") or {}}
