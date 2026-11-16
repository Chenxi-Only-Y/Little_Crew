"""剧组编制（职业可选）：**选配剧组，而不是改编排**（2026-11-16）。

## 这是什么

一套片子不必 19 个职位全上。用户选一个**编制模板**（preset）或自定义
`--crew-add/--crew-remove`，系统按名册的 `requires -> produces` 契约：

1. **依赖闭包**：选了「灯光师」但没选「场景设计师」→ 自动补齐（并记录 `autoAdded`）
2. **拒绝非法**：必需核心（编剧/分镜）不可删；商用（commercial）必须有合规官
3. **如实报告**：`active / skipped / autoAdded / hash` 全部进 `run.report.crew`
4. **跳过 ≠ 通过**：没启用的质检/审片/合规，交付状态只能标"未验证"

## 三档预设

| preset | 包含 | 用途 |
|---|---|---|
`minimal` | 编剧、分镜师 | 快速试拍（无质检/审片/合规，报告标未验证） |
`standard` | minimal + 美术/场景/角色/灯光/审片/场记/后期/作曲/文学策划/剧本顾问 | 日常交付 |
`full` | 全部 19 位 | 商用/答辩演示 |

**边界**：本模块只决定"哪些职位允许绑定/运行"；确定性执行（渲染/合成/混流）
和功能开关（`--cover` 等）仍按各自逻辑走 —— 它们不是职位。
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping, Sequence

#: 必需核心（删了就无法产出可执行对象）
MANDATORY: tuple[str, ...] = ("screenwriter", "storyboard_artist")

#: 三档预设（顺序即报告里的展示顺序）
PRESETS: dict[str, tuple[str, ...]] = {
    "minimal": ("screenwriter", "storyboard_artist"),
    "standard": (
        "screenwriter", "dramaturge", "story_editor",
        "production_designer", "scene_designer", "character_designer",
        "storyboard_artist", "gaffer",
        "composer", "post_production",
        "review_board", "continuity_supervisor",
    ),
    "full": (),  # 空 = 全部（运行时展开）
}

#: 质量/合规关键位：**不是必需核心，但缺席必须如实标"未验证"**
QUALITY_CRITICAL: dict[str, str] = {
    "review_board": "reviewed",            # 审片
    "continuity_supervisor": "qualityChecked",  # 质检（单镜+跨镜）
    "compliance_officer": "complianceAudited",  # 合规
}

#: 外部输入：没有职位生产者、也不需要职位（剧本来源/代码产物）
EXTERNAL_INPUTS: frozenset[str] = frozenset({
    "source.novel",
    "shot.image",        # 渲染通道（L3 工具）产出
    "episode.master",    # 合成通道产出
    "delivery.evidence", # 证据链由代码产出
})

#: 编制影响的功能开关（按职位名 -> 说明）；这些开关在演示/CLI 侧联动关闭
FEATURE_OF: dict[str, str] = {
    "character_designer": "--char-sheets",
    "cover_designer": "--cover",
    "dialogue_editor": "--voice",
    "composer": "配乐",
    "camera_operator": "--video-mode",
    "previs": "--llm-previs",
}


def _roster_maps(roster: Sequence[Any]) -> tuple[dict[str, Any], dict[str, list[str]]]:
    by_id = {str(a.agent_id): a for a in roster}
    producers: dict[str, list[str]] = {}
    for a in roster:
        for art in (a.produces or ()):
            producers.setdefault(str(art), []).append(str(a.agent_id))
    return by_id, producers


def _hash_of(preset: str, active: Sequence[str]) -> str:
    blob = "|".join([str(preset)] + sorted(str(x) for x in active))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def resolve(*, profile: str = "standard",
            add: Iterable[str] = (), remove: Iterable[str] = (),
            commercial: bool = False,
            roster: Sequence[Any] | None = None) -> dict[str, Any]:
    """把预设/自定义选择解析成最终编制（含依赖闭包与拒绝规则）。

    返回：`{ok, preset, active, skipped, autoAdded, errors, commercial, hash}`。
    **坏输入不抛异常**：返回 `ok=False` + `errors`（调用方负责报错）。
    """
    if roster is None:
        from little_crew.agent.roster import DEFAULT_ROSTER

        roster = DEFAULT_ROSTER
    by_id, producers = _roster_maps(roster)
    all_ids = [str(a.agent_id) for a in roster]
    add_set = {str(x).strip() for x in add if str(x).strip()}
    remove_set = {str(x).strip() for x in remove if str(x).strip()}
    name = str(profile or "standard").strip().lower()
    errors: list[str] = []

    if name in ("full", ""):
        base = set(all_ids)
    elif name in PRESETS:
        base = set(PRESETS[name])
    elif name == "custom":
        base = set(MANDATORY)
    else:
        return {"ok": False, "preset": name, "active": [], "skipped": all_ids,
                "autoAdded": [], "commercial": bool(commercial), "hash": "",
                "errors": [f"未知编制模板 {name!r}；可用：{sorted(PRESETS)} / full / custom"]}

    unknown = (add_set | remove_set) - set(all_ids)
    if unknown:
        errors.append(f"未知职位：{sorted(unknown)}")
    bad_remove = remove_set & set(MANDATORY)
    if bad_remove:
        errors.append(f"必需核心职位不可移除：{sorted(bad_remove)}")

    active = (base | (add_set & set(all_ids))) - remove_set
    active |= set(MANDATORY) - remove_set          # 核心始终在（除非被错误移除，上面已报错）
    active &= set(all_ids)

    # ---- 依赖闭包：缺哪个产物就补它的生产者（可迭代到稳定） ----
    auto_added: list[str] = []
    changed = True
    guard = 0
    while changed and guard < 100:
        changed = False
        guard += 1
        for aid in sorted(active):
            spec = by_id.get(aid)
            if spec is None:
                continue
            for art in (spec.requires or ()):
                art = str(art)
                if art in EXTERNAL_INPUTS:
                    continue
                if any(str(art) in (by_id[x].produces or ()) for x in active if x in by_id):
                    continue
                cands = [p for p in producers.get(art, []) if p in by_id]
                if not cands:
                    # 没有生产者、也不是外部输入 -> 契约断裂（如源小说），交给门禁/来源校验
                    continue
                pick = cands[0]
                if pick not in active and pick not in remove_set:
                    active.add(pick)
                    auto_added.append(pick)
                    changed = True

    # ---- 商用必须带合规官 ----
    if commercial and "compliance_officer" not in active:
        if "compliance_officer" in remove_set:
            errors.append("商用模式不可移除合规官（版权/授权/标识必须有归口）")
        else:
            active.add("compliance_officer")
            auto_added.append("compliance_officer")

    ordered = [a for a in all_ids if a in active]
    skipped = [a for a in all_ids if a not in active]

    # ---- "可选" ≠ "悄悄降质"：缺席的质量关键位必须显式标未验证 ----
    verification = {key: (agent in active)
                    for agent, key in QUALITY_CRITICAL.items()}
    unverified = [agent for agent, key in QUALITY_CRITICAL.items()
                  if not verification[key]]
    missing_keys = [k for k, v in verification.items() if not v]
    if not missing_keys:
        level = "full"
    elif name == "minimal":
        level = "draft"
    else:
        level = "reduced"

    return {
        "ok": not errors,
        "preset": name,
        "active": ordered,
        "skipped": skipped,
        "autoAdded": sorted(set(auto_added)),
        "commercial": bool(commercial),
        "hash": _hash_of(name, ordered),
        "errors": errors,
        "verification": verification,
        "unverified": unverified,
        "level": level,
    }


def apply_to_stubs(stubs: Mapping[str, Any], resolved: Mapping[str, Any]) -> dict[str, Any]:
    """按编制裁剪已构造的桩/实现表（返回被移除的 agent_id 列表）。"""
    active = set(resolved.get("active") or ())
    removed = sorted(k for k in stubs if k not in active)
    for k in removed:
        stubs.pop(k, None)                            # type: ignore[union-attr]
    return {"removed": removed, "kept": sorted(stubs)}


def feature_allowed(resolved: Mapping[str, Any], agent_id: str) -> bool:
    """功能开关是否允许（职位被跳过 -> 对应功能也关）。"""
    return str(agent_id) in set(resolved.get("active") or ())


def summary(resolved: Mapping[str, Any]) -> dict[str, Any]:
    """给报告用的摘要（不含完整职位表，避免 run.report 膨胀）。"""
    return {
        "preset": resolved.get("preset"),
        "hash": resolved.get("hash"),
        "commercial": bool(resolved.get("commercial")),
        "activeCount": len(resolved.get("active") or ()),
        "skippedCount": len(resolved.get("skipped") or ()),
        "autoAdded": list(resolved.get("autoAdded") or ()),
        "ok": bool(resolved.get("ok")),
        "errors": list(resolved.get("errors") or ()),
        # 缺席的质量关键位 -> 报告只能标"未验证"，不能标通过
        "verification": dict(resolved.get("verification") or {}),
        "unverified": list(resolved.get("unverified") or ()),
        "level": resolved.get("level", "full"),
    }
