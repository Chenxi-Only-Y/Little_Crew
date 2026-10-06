"""工作流**变更提案**（proposal）—— 架构师产出、人工审核、才允许生效。

## 为什么是"提案"而不是"直接改图"

参考本项目已有的成功模式（白模预演）：

    不让 LLM 写 Three.js 代码，而是**生成 JSON 规格**，前端据此渲染体块

理由（当时列了 5 条，这里同样成立）：安全（不可能执行任意代码）、
出错可控（schema 校验、可重试）、可存（能进工程一起存档）、
可复现（同一份 spec 结果一致）、**人工可改**。

所以架构师产出的是一份 **JSON 变更提案**，而不是直接操作图对象。

## 提案能做什么（与用户确认过的边界）

    · **开关现有节点**（把某个环节从链上摘掉/挂上）
    · **重排连线**（改边与条件分支）
    · **删节点** —— 允许，但需要**显式二次确认**（见 `needs_second_confirm`）

    ✗ **新增真实业务环节**不在能力内：节点背后必须有真实函数
      （`node_xxx` / `review_xxx_node`）。这类需求架构师只能**指出要做什么**，
      代码仍需人来写 —— `validate_spec` 会拦住引用未知函数的提案。
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.workflows import graph_spec as gs


# ---------------------------------------------------------------------------
# 提案的数据结构
# ---------------------------------------------------------------------------
@dataclass
class Proposal:
    """一份工作流变更提案。

    `ops` 是**顺序执行**的操作列表，每条形如：

        {"op": "remove_node", "key": "critics", "why": "..."}
        {"op": "add_node",    "key": "review_extra", "kind": "review", "why": "..."}
        {"op": "set_edge",    "from": "screenwriter", "to": "review_critics", "why": "..."}
        {"op": "remove_edge", "from": "critics", "to": "review_critics", "why": "..."}
        {"op": "set_branch",  "from": "review_critics", "router": "simple_review",
                              "branches": {...}, "why": "..."}
        {"op": "set_entry",   "key": "story_planner", "why": "..."}

    允许的操作名是**封闭集合**（`ALLOWED_OPS`）—— 提案里出现别的操作一律拒绝，
    避免"提案里塞一个我没想过的操作"。
    """

    summary: str
    ops: list[dict] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    #: 提案 id（落盘审计用）
    id: str = field(default_factory=lambda: f"p-{uuid.uuid4().hex[:10]}")
    #: 提出者（架构师的模型名 / "mock"）
    author: str = ""
    #: 原始 LLM 输出（排错用；可能不是 JSON）
    raw: str = ""

    # ---- 判定 ----
    @property
    def removes_nodes(self) -> list[str]:
        return [o["key"] for o in self.ops
                if o.get("op") == "remove_node" and o.get("key")]

    @property
    def needs_second_confirm(self) -> bool:
        """是否需要**显式二次确认**。

        用户明确要求："允许删节点，但需显式二次确认"。
        所以只要提案里有 `remove_node`，审批就必须走两步：
        第一次通过只表示"我看过这个提案"，还要再确认一次删除。
        """
        return bool(self.removes_nodes)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "summary": self.summary,
            "ops": self.ops,
            "risks": self.risks,
            "author": self.author,
            "removes_nodes": self.removes_nodes,
            "needs_second_confirm": self.needs_second_confirm,
        }


#: 允许的操作（封闭集合）
ALLOWED_OPS: frozenset[str] = frozenset({
    "add_node", "remove_node", "set_edge", "remove_edge",
    "set_branch", "remove_branch", "set_entry", "set_finish",
})


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
@dataclass
class OpProblem:
    where: str
    message: str

    def as_dict(self) -> dict:
        return {"where": self.where, "message": self.message}


def validate_ops(ops: list[dict]) -> list[OpProblem]:
    """**逐条**校验操作本身是否合法（还不管应用后的图）。

    之后还要把应用结果交给 `graph_spec.validate_spec()` 做**结构**校验 ——
    两道都要过，缺一不可：
      这里管"操作说不说得通"，那里管"改完的图跑不跑得起来"。
    """
    out: list[OpProblem] = []
    if not isinstance(ops, list):
        return [OpProblem("ops", "ops 必须是数组")]

    for i, o in enumerate(ops):
        where = f"ops[{i}]"
        if not isinstance(o, dict):
            out.append(OpProblem(where, "操作必须是对象"))
            continue
        op = o.get("op")
        if op not in ALLOWED_OPS:
            out.append(OpProblem(
                where,
                f"不支持的操作 {op!r}（可用：{sorted(ALLOWED_OPS)}）"))
            continue

        if op in ("add_node", "remove_node", "set_entry", "set_finish"):
            if not o.get("key"):
                out.append(OpProblem(where, f"{op} 缺少 key"))
            if op == "add_node":
                kind = o.get("kind")
                if kind not in ("business", "review"):
                    out.append(OpProblem(
                        where, f"add_node 的 kind 必须是 business/review，收到 {kind!r}"))
                fn = o.get("fn")
                if fn is not None and fn not in gs.all_node_fns():
                    out.append(OpProblem(
                        where,
                        f"add_node 引用了未实现的函数 {fn!r} —— "
                        f"新增业务环节需要先写代码，提案阶段不能凭空造节点"))
        elif op in ("set_edge", "remove_edge"):
            if not o.get("from") or not o.get("to"):
                out.append(OpProblem(where, f"{op} 需要 from 与 to"))
        elif op in ("set_branch", "remove_branch"):
            if not o.get("from"):
                out.append(OpProblem(where, f"{op} 需要 from"))
            if op == "set_branch":
                if o.get("router") not in gs.ROUTERS:
                    out.append(OpProblem(
                        where, f"未知路由 {o.get('router')!r}（可用：{sorted(gs.ROUTERS)}）"))
                br = o.get("branches")
                if not isinstance(br, dict) or not br:
                    out.append(OpProblem(where, "set_branch 需要非空的 branches"))
                else:
                    allowed = set(gs.ROUTERS.get(o.get("router"), {}))
                    for rv in br:
                        if rv not in allowed:
                            out.append(OpProblem(
                                where,
                                f"路由 {o.get('router')!r} 没有返回值 {rv!r}"
                                f"（可用：{sorted(allowed)}）"))
    return out


# ---------------------------------------------------------------------------
# 应用：把提案打到一份规格上
# ---------------------------------------------------------------------------
def apply_ops(spec: dict, ops: list[dict]) -> tuple[dict, list[str]]:
    """把 `ops` 应用到 `spec` 的**副本**上，返回 `(新规格, 变更日志)`。

    只改数据、不碰代码 —— 这正是 A2 能"运行时换图"的原因。
    应用完**必须**再交给 `validate_spec()`；本函数不做结构校验
    （万一应用出断链的图，由那道校验拦下并给出人话原因）。
    """
    s = copy.deepcopy(spec)
    s.setdefault("nodes", [])
    s.setdefault("edges", [])
    s.setdefault("conditional", [])
    log: list[str] = []

    for o in ops:
        op = o.get("op")
        why = o.get("why") or ""

        if op == "add_node":
            key = o["key"]
            kind = o.get("kind", "business")
            node = {"key": key, "kind": kind}
            if o.get("fn"):
                node["fn"] = o["fn"]
            s["nodes"] = [n for n in s["nodes"] if n.get("key") != key] + [node]
            log.append(f"新增节点 {key}（{kind}）{_why(why)}")

        elif op == "remove_node":
            key = o["key"]
            s["nodes"] = [n for n in s["nodes"] if n.get("key") != key]
            s["edges"] = [e for e in s["edges"]
                          if e.get("from") != key and e.get("to") != key]
            s["conditional"] = [e for e in s["conditional"] if e.get("from") != key]
            for e in s["conditional"]:
                e["branches"] = {k: v for k, v in (e.get("branches") or {}).items()
                                 if v != key}
            log.append(f"删除节点 {key}{_why(why)}")

        elif op == "set_edge":
            a, b = o["from"], o["to"]
            s["edges"] = [e for e in s["edges"]
                          if not (e.get("from") == a and e.get("to") == b)]
            s["edges"].append({"from": a, "to": b})
            log.append(f"连线 {a} → {b}{_why(why)}")

        elif op == "remove_edge":
            a, b = o["from"], o["to"]
            before = len(s["edges"])
            s["edges"] = [e for e in s["edges"]
                          if not (e.get("from") == a and e.get("to") == b)]
            if len(s["edges"]) < before:
                log.append(f"断开连线 {a} → {b}{_why(why)}")
            else:
                log.append(f"（注意）要断开的连线 {a} → {b} 本来就不存在")

        elif op == "set_branch":
            a = o["from"]
            s["conditional"] = [e for e in s["conditional"] if e.get("from") != a]
            s["conditional"].append({
                "from": a, "router": o["router"],
                "branches": dict(o["branches"]),
            })
            log.append(f"条件边 {a}（{o['router']}）→ {o['branches']}{_why(why)}")

        elif op == "remove_branch":
            a = o["from"]
            s["conditional"] = [e for e in s["conditional"] if e.get("from") != a]
            log.append(f"移除条件边 {a}{_why(why)}")

        elif op == "set_entry":
            s["entry"] = o["key"]
            log.append(f"入口改为 {o['key']}{_why(why)}")

        elif op == "set_finish":
            s["finish"] = o["key"]
            log.append(f"结束点改为 {o['key']}{_why(why)}")

    return s, log


def _why(why: str) -> str:
    return f"（{why}）" if why else ""


# ---------------------------------------------------------------------------
# 解析 LLM 输出
# ---------------------------------------------------------------------------
def parse_proposal(text: str, *, author: str = "") -> Proposal:
    """把 LLM 的输出解析成 `Proposal`。

    **宽松但可拒绝**：模型可能包一层 markdown 代码块、或多说几句。
    这里只负责"取出 JSON"；字段缺失走默认值，**不因为格式小瑕疵就丢掉整份提案**
    （否则用户看到的是"没反应"）。真正不合法的操作会被 `validate_ops` 拦下并说明原因。
    """
    import json
    import re

    raw = text or ""
    data: dict[str, Any] = {}

    # 去掉 ```json ... ``` 包裹
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    cand = m.group(1) if m else raw
    # 取第一个平衡的 { ... }
    start = cand.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(cand)):
            if cand[i] == "{":
                depth += 1
            elif cand[i] == "}":
                depth -= 1
                if depth == 0:
                    cand = cand[start:i + 1]
                    break
    try:
        data = json.loads(cand)
    except Exception:  # noqa: BLE001
        return Proposal(summary="（模型没有返回可解析的 JSON）", ops=[],
                        risks=["模型输出不是合法 JSON，无法应用"], author=author,
                        raw=raw[:4000])

    if not isinstance(data, dict):
        return Proposal(summary="（模型返回的不是对象）", ops=[],
                        risks=["模型输出不是 JSON 对象"], author=author,
                        raw=raw[:4000])

    ops = data.get("ops") or data.get("operations") or []
    if not isinstance(ops, list):
        ops = []
    risks = data.get("risks") or []
    if isinstance(risks, str):
        risks = [risks]

    return Proposal(
        summary=str(data.get("summary") or data.get("title") or "（未给出摘要）"),
        ops=[o for o in ops if isinstance(o, dict)],
        risks=[str(r) for r in risks],
        author=author,
        raw=raw[:4000],
    )


__all__ = [
    "Proposal", "ALLOWED_OPS", "OpProblem", "validate_ops", "apply_ops",
    "parse_proposal",
]
