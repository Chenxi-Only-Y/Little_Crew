"""职业依赖与顺序（2026-11-17）：**必需性 + 先后顺序**两层。

平台已有的事实：
- `AgentSpec.produces/requires` 声明产物依赖 → 编制解析会自动补生产者（依赖闭包）
- `MANDATORY = ("screenwriter", "storyboard_artist")`：必需职业不可移除
- 阶段顺序（Stage）存在，但没有把"必须在谁之后"统一成一张可校验的图

本模块把它统一成**一张依赖图**：
    edges = 产物依赖（consumer -> producer） ∪ 显式 after 约束
    order = 拓扑序；有环/未知 -> F-ORDER-*
    mandatory 检查 -> F-POSITION-MISSING

注意：这里只做**声明与校验**，调度仍由流水线/编制解析完成。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

__all__ = ["EXPLICIT_AFTER", "dependency_edges", "order_positions",
           "validate_dependencies", "describe", "F_POSITION_MISSING", "F_ORDER"]

F_POSITION_MISSING = "F-POSITION-MISSING"
F_ORDER = "F-ORDER-CYCLE"

#: 显式顺序约束：产物关系推不出来、但业务上必须先做的关系
EXPLICIT_AFTER: dict[str, tuple[str, ...]] = {
    "director": ("screenwriter",),
    "storyboard_artist": ("director", "character_designer", "scene_designer"),
    "gaffer": ("scene_designer",),
    "camera_operator": ("storyboard_artist", "gaffer"),
    "continuity_supervisor": ("camera_operator",),
    "review_board": ("camera_operator",),
    "post_production": ("camera_operator", "continuity_supervisor"),
    "cover_designer": ("camera_operator",),
    "compliance_officer": ("post_production",),
}


def _specs(roster: Sequence[Any] | None = None) -> list[Any]:
    if roster is not None:
        return list(roster)
    from little_crew.agent.roster import DEFAULT_ROSTER

    return list(DEFAULT_ROSTER)


def _pid(s: Any) -> str:
    return str(getattr(s, "agent_id", "") or getattr(s, "position_id", "") or "")


def dependency_edges(roster: Sequence[Any] | None = None) -> dict[str, set[str]]:
    """返回 `consumer -> {producers}`（consumer 必须排在 producer 之后）。"""
    specs = _specs(roster)
    producer_of: dict[str, str] = {}
    for s in specs:
        for art in (getattr(s, "produces", ()) or ()):
            producer_of.setdefault(str(art), _pid(s))
    edges: dict[str, set[str]] = {}
    for s in specs:
        me = _pid(s)
        deps: set[str] = set()
        _ids = {_pid(x) for x in specs}
        if me in EXPLICIT_AFTER:
            # 显式 after **优先**：像导演这种"审阅型 requires"会 require 下游产物
            # （shot.list/shot.image），若与产物边合并会形成环；显式 after 是业务真序。
            deps = {d for d in EXPLICIT_AFTER[me] if d in _ids}
        else:
            for art in (getattr(s, "requires", ()) or ()):
                p = producer_of.get(str(art))
                if p and p != me:
                    deps.add(p)
        edges[me] = deps
    return edges


def order_positions(roster: Sequence[Any] | None = None) -> list[str]:
    """拓扑序（被依赖者在前）；有环 -> ValueError(F-ORDER-CYCLE)。"""
    specs = _specs(roster)
    ids = [_pid(s) for s in specs]
    edges = dependency_edges(roster)
    unknown = {d for deps in edges.values() for d in deps} - set(ids)
    if unknown:
        raise ValueError(f"{F_ORDER}: 依赖不存在的职业 {sorted(unknown)}")
    out: list[str] = []
    done: set[str] = set()
    while len(out) < len(ids):
        ready = [i for i in ids if i not in done and edges.get(i, set()) <= done]
        if not ready:
            raise ValueError(f"{F_ORDER}: 职业依赖有环")
        for i in ready:
            if i not in done:
                done.add(i)
                out.append(i)
    return out


def validate_dependencies(profile_ids: Sequence[str] | None = None,
                          roster: Sequence[Any] | None = None,
                          mandatory: Sequence[str] | None = None) -> dict[str, Any]:
    """校验：必需职业齐全、依赖可拓扑、顺序正确。"""
    from little_crew.runtime.crew_profile import MANDATORY as _CORE_MANDATORY

    specs = _specs(roster)
    all_ids = {_pid(s) for s in specs}
    want = list(profile_ids) if profile_ids is not None else sorted(all_ids)
    must = list(mandatory) if mandatory is not None else list(_CORE_MANDATORY)
    errors: list[str] = []
    missing = [m for m in must if m not in want]
    if missing:
        errors.append(f"{F_POSITION_MISSING}: 缺必需职业 {missing}")
    unknown = [x for x in want if x not in all_ids]
    if unknown:
        errors.append(f"未知职业：{unknown}")
    try:
        order = order_positions(roster)
    except ValueError as e:
        errors.append(str(e))
        order = []
    # 顺序检查：want 内每对依赖都必须出现（不要求连续）
    if order:
        rank = {p: i for i, p in enumerate(order)}
        edges = dependency_edges(roster)
        for me in want:
            for dep in edges.get(me, ()):
                if dep in want and rank.get(dep, -1) > rank.get(me, -1):
                    errors.append(f"F-ORDER: {me} 必须排在 {dep} 之后")
    return {"ok": not errors, "errors": errors, "order": order,
            "mandatory": must, "positions": want}


def describe(roster: Sequence[Any] | None = None) -> dict[str, Any]:
    specs = _specs(roster)
    edges = dependency_edges(roster)
    return {"positions": len(specs),
            "edges": sum(len(v) for v in edges.values()),
            "mandatory": list(__import__("little_crew.runtime.crew_profile",
                                        fromlist=["MANDATORY"]).MANDATORY),
            "order": order_positions(roster) if specs else []}
