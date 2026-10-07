"""工作流的**图规格**（graph spec）—— 把拓扑从代码里搬进数据。

## 为什么需要它

原先 `build_graph()` 是**命令式硬编码**：23 个 `add_node` + 41 个 `add_edge`
一行行写死在函数里。后果是**改一条边就得改代码**，于是

    "让架构师在运行时改图"（A2）根本没有落点。

这个模块把拓扑变成**数据**：

    GRAPH_SPEC = {
      "nodes":       [{"key", "kind", "fn"}...],        # 节点及其实现函数名
      "edges":       [{"from", "to"}...],               # 普通边
      "conditional": [{"from", "router", "branches"}],  # 条件边（含路由函数名）
      "entry":       "story_planner",
      "finish":      "aggregator",
    }

于是「开关节点 / 重排连线」就是**改这份数据**（可以来自 LLM 提案），
再用 `build_graph(spec)` 搭出来 —— 这是 A2 运行时换图的前提。

## 边界（与用户确认过）

**A2 只能重新组装 `nodes` 里已有的实现函数** —— 因为一个节点要能跑，
背后必须有真实函数（`node_xxx` / `review_xxx_node`）。
**新增真实业务环节**不在 A2 能力内：那需要先有人写实现，A3 只能出骨架提案。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable

# ---------------------------------------------------------------------------
# 1. 节点实现函数名 → 实际可调用对象
#
#    这里是"能被挂进图的全部零件"。规格里的 `fn` 必须取自这张表
#    （校验会拦住未知名字，避免提案引用一个不存在的函数）。
# ---------------------------------------------------------------------------
#: 业务节点：`_ctx("node_xxx")` 包一层，把阶段名注入 state
BUSINESS_NODE_FNS: tuple[str, ...] = (
    "story_planner", "screenwriter", "critics", "art_brief_parser",
    "character_designer", "scene_designer", "consistency_auditor",
    "storyboarder", "rhythm_director", "continuity_checker",
    "prompt_compiler", "image_generator", "quality_assessor",
    # 白模预演师：出 3D 预演**初稿**（人工在 #/previs 微调）。
    # 加进这里之后，"预演"才算真的属于这个环节（原先只是注册表声明）。
    "previs_artist",
    # 配音指导：逐镜头对白音频（TTS）。原先注册表声明了、但不在图里。
    "voice_director",
    # 发行总监：**可跳过** —— 它的输入（成片）来自独立的异步视频任务，
    # 不在图里，所以输入缺失时优雅跳过而不是阻塞。
    #
    # ⚠️ 注意**没有**单独的 editor 节点：注册表里 `editor`（剪辑师）的
    # `workflow_node` 本来就是 `aggregator`（历史命名，legacy_keys=('aggregator',)）
    # —— 也就是**聚合节点就是剪辑师**。我第一版另外加了个 `editor` 节点，
    # 等于两个剪辑师（标签还撞名），已撤掉。
    "distributor",
    "aggregator",
)

#: 人工确认点节点：各自一个独立函数（复用 `_review`）
REVIEW_NODE_FNS: tuple[str, ...] = (
    "review_script", "review_critics", "review_assets", "review_storyboard",
    "review_rhythm", "review_continuity", "review_prompts",
    "review_quality", "review_images",
)

#: 条件路由函数名 → 允许的分支目标（`route` 的返回值 → 下一个节点）
ROUTERS: dict[str, dict[str, str]] = {
    "simple_review": {"continue": "", "redo": ""},   # 目标由各条边各自声明
    "review_script": {"continue": "", "redo": "", "back": ""},
    "review_assets": {"continue": "", "redo": "", "back": ""},
    "review_storyboard": {"continue": "", "redo": "", "back": ""},
    "review_images": {"continue": "", "redo": "", "back": ""},
    "quality": {"retry": "", "continue": ""},
}


def all_node_fns() -> set[str]:
    return set(BUSINESS_NODE_FNS) | set(REVIEW_NODE_FNS)


# ---------------------------------------------------------------------------
# 2. 默认规格 = **当前在跑的这张图**（与硬编码版本逐边对应）
# ---------------------------------------------------------------------------
def default_spec() -> dict:
    """当前生产图的规格。

    ⚠️ 这份数据必须与重构前的硬编码图**完全等价** ——
    有测试逐条对比（`test_graph_spec.py`），改错一条边就会被抓到。
    """
    return {
        "version": 1,
        "description": "当前生产工作流（重构前的硬编码拓扑，逐边搬进来）",
        "nodes": [
            # ---- 业务节点 ----
            {"key": "story_planner", "kind": "business"},
            {"key": "screenwriter", "kind": "business"},
            {"key": "critics", "kind": "business"},
            {"key": "art_brief_parser", "kind": "business"},
            {"key": "character_designer", "kind": "business"},
            {"key": "scene_designer", "kind": "business"},
            {"key": "consistency_auditor", "kind": "business"},
            {"key": "storyboarder", "kind": "business"},
            # 白模预演师：注册表里它本来就声明在第 11 步、能力 render_3d、
            # 产出 previs —— 但 `workflow_node` 为空，**从没进过执行图**。
            # 用户要求"把「预演」构成「白模预演师」一部分"，故正式接入：
            # 它在分镜确认之后出**预演初稿**，人工再到 #/previs 微调。
            {"key": "previs_artist", "kind": "business"},
            # 配音指导：注册表声明它在第 12 步、产出 voice_track ——
            # 但 `workflow_node` 为空，从没进过执行图。用户要求"1、2 先做"。
            {"key": "voice_director", "kind": "business"},
            {"key": "rhythm_director", "kind": "business"},
            {"key": "continuity_checker", "kind": "business"},
            {"key": "prompt_compiler", "kind": "business"},
            {"key": "image_generator", "kind": "business"},
            {"key": "quality_assessor", "kind": "business"},
            # 发行总监（可跳过）。剪辑师 = 上面的 aggregator，不重复声明。
            {"key": "distributor", "kind": "business"},
            {"key": "aggregator", "kind": "business"},
            # ---- 人工确认点 ----
            {"key": "review_critics", "kind": "review"},
            {"key": "review_script", "kind": "review"},
            {"key": "review_assets", "kind": "review"},
            {"key": "review_storyboard", "kind": "review"},
            {"key": "review_rhythm", "kind": "review"},
            {"key": "review_continuity", "kind": "review"},
            {"key": "review_prompts", "kind": "review"},
            {"key": "review_quality", "kind": "review"},
            {"key": "review_images", "kind": "review"},
        ],
        #: 入口 / 结束
        "entry": "story_planner",
        "finish": "aggregator",
        #: 普通边
        "edges": [
            # ⚠️ 这条**差点漏掉**：硬编码版里是 `g.add_edge("story_planner", "screenwriter")`，
            # 逐边搬进数据时我漏了它 —— 幸好核对脚本（节点/边逐条对比）当场抓到。
            # 漏了它的后果：策划总监跑完后**没有出边**，执行直接卡死在第一步。
            {"from": "story_planner", "to": "screenwriter"},
            {"from": "screenwriter", "to": "critics"},
            {"from": "critics", "to": "review_critics"},
            {"from": "art_brief_parser", "to": "character_designer"},
            {"from": "art_brief_parser", "to": "scene_designer"},
            {"from": "character_designer", "to": "consistency_auditor"},
            {"from": "scene_designer", "to": "consistency_auditor"},
            {"from": "consistency_auditor", "to": "review_assets"},
            {"from": "storyboarder", "to": "review_storyboard"},
            # 分镜确认 → 预演初稿 → **配音** → 节奏/连贯性
            {"from": "previs_artist", "to": "voice_director"},
            {"from": "voice_director", "to": "rhythm_director"},
            {"from": "rhythm_director", "to": "review_rhythm"},
            {"from": "continuity_checker", "to": "review_continuity"},
            {"from": "prompt_compiler", "to": "review_prompts"},
            {"from": "image_generator", "to": "quality_assessor"},
            # 剪辑师（aggregator）→ 发行总监 → 结束
            {"from": "aggregator", "to": "distributor"},
            {"from": "distributor", "to": "__END__"},
        ],
        #: 条件边：`branches` 是 路由返回值 → 目标节点
        "conditional": [
            {"from": "review_critics", "router": "simple_review",
             "branches": {"continue": "review_script", "redo": "critics"}},
            {"from": "review_script", "router": "review_script",
             "branches": {"continue": "art_brief_parser",
                          "redo": "screenwriter", "back": "story_planner"}},
            {"from": "review_assets", "router": "review_assets",
             "branches": {"continue": "storyboarder",
                          "redo": "character_designer", "back": "screenwriter"}},
            {"from": "review_storyboard", "router": "review_storyboard",
             "branches": {"continue": "previs_artist",
                          "redo": "storyboarder", "back": "character_designer"}},
            {"from": "review_rhythm", "router": "simple_review",
             "branches": {"continue": "continuity_checker", "redo": "rhythm_director"}},
            {"from": "review_continuity", "router": "simple_review",
             "branches": {"continue": "prompt_compiler", "redo": "continuity_checker"}},
            {"from": "review_prompts", "router": "simple_review",
             "branches": {"continue": "image_generator", "redo": "prompt_compiler"}},
            {"from": "quality_assessor", "router": "quality",
             "branches": {"retry": "image_generator", "continue": "review_quality"}},
            {"from": "review_quality", "router": "simple_review",
             "branches": {"continue": "review_images", "redo": "quality_assessor"}},
            {"from": "review_images", "router": "review_images",
             "branches": {"continue": "aggregator", "redo": "image_generator",
                          "back": "storyboarder"}},
        ],
    }


# ---------------------------------------------------------------------------
# 3. 校验：提案改完的规格必须"能搭出一张合理的图"
# ---------------------------------------------------------------------------
@dataclass
class SpecProblem:
    kind: str          # error | warning
    message: str
    where: str = ""

    def as_dict(self) -> dict:
        return {"kind": self.kind, "message": self.message, "where": self.where}


def validate_spec(spec: dict) -> list[SpecProblem]:
    """**结构性**校验（不涉及业务语义）。

    检查项（每条都对应一种"图会直接坏掉"的情况）：
      1. 节点 key 不重复、非空
      2. 节点 `kind` 合法、`fn`（若指定）在允许表里
      3. 边的两端都存在于节点集合（或 `__END__`）
      4. 入口节点存在；`finish` 存在
      5. 每个节点至少有一条出边（含条件边）—— 否则执行会**卡死在那**
         （除了 `finish` 本身指向 END）
      6. 除入口外，每个节点至少有一条入边 —— 否则那个节点**永远不会被走到**
      7. 条件边的 `router` 已知，且 `branches` 的键属于该路由的合法返回值
    """
    out: list[SpecProblem] = []
    nodes = spec.get("nodes") or []
    keys = [n.get("key") for n in nodes]

    # 1. key
    seen: set[str] = set()
    for k in keys:
        if not k:
            out.append(SpecProblem("error", "节点 key 为空"))
        elif k in seen:
            out.append(SpecProblem("error", f"节点 key 重复：{k}", where=k))
        else:
            seen.add(k)

    # 2. kind / fn
    for n in nodes:
        k = n.get("key") or "?"
        kind = n.get("kind")
        if kind not in ("business", "review"):
            out.append(SpecProblem(
                "error", f"节点 kind 非法（应为 business/review）：{kind!r}", where=k))
        fn = n.get("fn")
        if fn is not None and fn not in all_node_fns():
            out.append(SpecProblem(
                "error",
                f"节点引用了未实现的函数 {fn!r} —— A2 只能重新组装"
                f"**已有**的实现函数（新增业务环节需要先写代码）",
                where=k))

    node_set = set(seen)
    edge_targets: set[str] = set()
    has_out: set[str] = set()

    # 3/5. 普通边
    for e in spec.get("edges") or []:
        a, b = e.get("from"), e.get("to")
        if a not in node_set:
            out.append(SpecProblem("error", f"边的起点不存在：{a!r}", where=str(a)))
        if b != "__END__" and b not in node_set:
            out.append(SpecProblem("error", f"边的终点不存在：{b!r}", where=str(a)))
        if a in node_set:
            has_out.add(a)
        if b and b != "__END__":
            edge_targets.add(b)

    # 7. 条件边
    for e in spec.get("conditional") or []:
        a, r = e.get("from"), e.get("router")
        if a not in node_set:
            out.append(SpecProblem("error", f"条件边的起点不存在：{a!r}", where=str(a)))
        if r not in ROUTERS:
            out.append(SpecProblem(
                "error",
                f"未知路由函数 {r!r}（可选：{sorted(ROUTERS)}）", where=str(a)))
        else:
            allowed = set(ROUTERS[r])
            branches = e.get("branches") or {}
            if not branches:
                out.append(SpecProblem("error", f"条件边没有分支：{a!r}", where=str(a)))
            for rv, tgt in branches.items():
                if rv not in allowed:
                    out.append(SpecProblem(
                        "error",
                        f"路由 {r!r} 没有返回值 {rv!r}（可选：{sorted(allowed)}）",
                        where=str(a)))
                if tgt != "__END__" and tgt not in node_set:
                    out.append(SpecProblem(
                        "error", f"条件分支指向不存在的节点：{tgt!r}", where=str(a)))
                if tgt and tgt != "__END__":
                    edge_targets.add(tgt)
        if a in node_set:
            has_out.add(a)

    # 4. 入口 / 结束
    entry = spec.get("entry")
    finish = spec.get("finish")
    if entry not in node_set:
        out.append(SpecProblem("error", f"入口节点不存在：{entry!r}"))
    if finish and finish not in node_set:
        out.append(SpecProblem("error", f"finish 节点不存在：{finish!r}"))

    # 5. 出边缺失 → 会卡死
    for k in node_set:
        if k not in has_out:
            out.append(SpecProblem(
                "error",
                f"节点 {k!r} 没有任何出边 —— 执行到这里会**卡死**",
                where=k))

    # 6. 入边缺失 → 永远走不到
    for k in node_set:
        if k == entry:
            continue
        if k not in edge_targets:
            out.append(SpecProblem(
                "error",
                f"节点 {k!r} 没有任何入边 —— 它**永远不会被执行**",
                where=k))

    # 8. **可达性**：从入口顺着边能不能走到它
    #
    # ⚠️ 这条是**补上来的**：上面第 6 条只看"有没有入边"，会漏掉一种情况 ——
    # 某个节点自己有出边（所以它出现在边的"起点"里），于是第 6 条不报它，
    # 但从**入口**已经走不到它了（它的所有前驱被删掉）。
    # 实测：删掉 `image_generator` 后，`quality_assessor` 自己还是 `retry` 的起点，
    # 于是"有入边"的检查判它没问题，可它已经成了进不去的死环节。
    reachable = _reachable_from(entry, node_set, spec)
    for k in sorted(node_set - reachable):
        out.append(SpecProblem(
            "error",
            f"节点 {k!r} 从入口 {entry!r} **走不到**（前驱被摘掉了）—— "
            f"它会成为永远不执行的死环节",
            where=k))

    return out


def _reachable_from(entry: str, node_set: set[str], spec: dict) -> set[str]:
    """从入口做一次图遍历，返回可达的节点集合。"""
    if entry not in node_set:
        return set()
    # 邻接表：包含普通边与条件分支
    adj: dict[str, set[str]] = {k: set() for k in node_set}
    for e in spec.get("edges") or []:
        a, b = e.get("from"), e.get("to")
        if a in adj and b in node_set:
            adj[a].add(b)
    for e in spec.get("conditional") or []:
        a = e.get("from")
        if a not in adj:
            continue
        for tgt in (e.get("branches") or {}).values():
            if tgt in node_set:
                adj[a].add(tgt)

    seen: set[str] = set()
    stack = [entry]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(adj.get(cur, ()))
    return seen


def errors_of(problems: list[SpecProblem]) -> list[SpecProblem]:
    return [p for p in problems if p.kind == "error"]


def clone_spec(spec: dict) -> dict:
    return copy.deepcopy(spec)


__all__ = [
    "BUSINESS_NODE_FNS", "REVIEW_NODE_FNS", "ROUTERS",
    "all_node_fns", "default_spec", "validate_spec", "errors_of",
    "clone_spec", "SpecProblem",
]
