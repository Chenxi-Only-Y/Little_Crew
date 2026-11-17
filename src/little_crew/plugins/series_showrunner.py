"""系列总导演（Showrunner）· **可选插件**（0.6.0）。

新职业不挤占单集 19 职位名册，也不被核心 import：
- 插件提供能力：`series.plan`（把大纲/章名 → chapter_plan：章卡/边界契约/依赖）
- 核心只认 `chapter_plan` 数据结构（`runtime/chapter_plan.py`），不认这个插件
- 卸载后：series 功能提示"未加载 showrunner 插件"，单集流水线不受影响

总导演上下文纪律（R2）：
- 只读 logline + 卷名 + 章名 + 每章一句 synopsis + 已有契约
- **不读章节正文**（`chapter_plan.assert_no_script` 守门）
"""
from __future__ import annotations

from typing import Any, Mapping

from little_crew.plugin import PluginManifest
from little_crew.runtime.chapter_plan import (CARD_FIELDS, CONTRACT_FIELDS,
                                              PLAN_SCHEMA, validate_plan)

__all__ = ["plan_series", "PLUGIN"]

_SYSTEM = (
    "你是《小剧组》的**总导演（Showrunner）**。你只输出 JSON，不写正文。"
    "你的职责：给一部多集短剧做章节计划——每章一张三字段章卡（目标/冲突/钩子），"
    "以及章与章之间的边界契约（entry/exit）。你只看到章名与一句梗概，"
    "**不要编造完整剧情正文**；契约必须能让下一章的导演接上。"
)


def _outline_chapters(outline: Mapping[str, Any]) -> list[dict[str, Any]]:
    out = []
    for c in (outline.get("chapters") or ()):
        if isinstance(c, Mapping):
            out.append({"chapterId": str(c.get("chapterId") or c.get("id") or ""),
                        "name": str(c.get("name") or c.get("title") or ""),
                        "synopsis": str(c.get("synopsis") or c.get("oneLine") or "")[:200]})
    return out


def _fallback_plan(outline: Mapping[str, Any]) -> dict[str, Any]:
    chapters = []
    prev_id = ""
    for i, c in enumerate(_outline_chapters(outline), 1):
        cid = c["chapterId"] or f"CH{i:02d}"
        chapters.append({
            "chapterId": cid,
            "name": c["name"] or f"第 {i} 章",
            "card": {"goal": c["synopsis"] or c["name"] or f"第 {i} 章目标",
                     "conflict": "（待人工补充）", "hook": "（待人工补充）"},
            "entry": {"placeKey": "", "time": "", "characters": [],
                      "props": [], "openThreads": []},
            "exit": {"placeKey": "", "time": "", "characters": [],
                     "props": [], "openThreads": []},
            "dependsOn": [prev_id] if prev_id else [],
        })
        prev_id = cid
    return {"schema": PLAN_SCHEMA,
            "seriesId": str(outline.get("seriesId") or ""),
            "logline": str(outline.get("logline") or ""),
            "volumes": list(outline.get("volumes") or []),
            "chapters": chapters,
            "source": "fallback"}


def plan_series(outline: Mapping[str, Any], *, client: Any = None,
                hero_max: int = 2) -> dict[str, Any]:
    """总导演：大纲/章名 → chapter_plan。LLM 不可用 → 结构化占位（人工补）。"""
    err = ""
    plan: dict[str, Any] = {}
    if client is not None:
        try:
            chapters = _outline_chapters(outline)
            prompt = (
                "## 系列大纲（只含章名+一句梗概，不含正文）\n"
                f"logline: {outline.get('logline') or ''}\n"
                f"卷: {outline.get('volumes') or []}\n"
                + "\n".join(f"- {c['chapterId']}｜{c['name']}｜梗概：{c['synopsis']}"
                            for c in chapters)
                + "\n\n## 输出（严格 JSON）\n"
                '{"schema":"' + PLAN_SCHEMA + '","logline":"...","volumes":[...],'
                '"chapters":[{"chapterId":"CH01","name":"...",'
                '"card":{"goal":"...","conflict":"...","hook":"..."},'
                '"entry":{"placeKey":"...","time":"...","characters":[...],'
                '"props":[...],"openThreads":[...]},'
                '"exit":{"placeKey":"...","time":"...","characters":[...],'
                '"props":[...],"openThreads":[...]},'
                '"dependsOn":[...]}]}\n'
                f"规则：每章三字段章卡齐全；Tier3 预算 ≤ {hero_max}/章；"
                "第 2 章起 entry 必须能与上一章 exit 对上；只规划，不写正文。"
            )
            obj = client.chat_json(prompt, system=_SYSTEM, what="chapter.plan")
            if isinstance(obj, Mapping) and obj.get("chapters"):
                plan = dict(obj)
                plan.setdefault("schema", PLAN_SCHEMA)
                plan["source"] = "llm-showrunner"
        except Exception as e:                               # noqa: BLE001
            err = f"{type(e).__name__}: {str(e)[:160]}"
    if not plan:
        plan = _fallback_plan(outline)
    errs = validate_plan(plan)
    return {"plan": plan, "errors": errs, "ok": not errs,
            "source": plan.get("source", "fallback"), "error": err}


PLUGIN = PluginManifest(
    name="series_showrunner",
    version="0.1.0",
    description="系列总导演：章卡/边界契约/章节计划（可选插件；不进 19 职位名册）",
    provides={"series.plan": plan_series},
    optional=True,
)
