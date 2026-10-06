"""工作流架构师 —— 用 LLM 产出**工作流变更提案**（A2/A3 的产出口）。

## 它做什么 / 不做什么

    ✅ 读当前图规格（节点、边、条件边、能力），理解用户诉求
    ✅ 产出**变更提案 JSON**（`proposal.Proposal`）：改哪几条边、关/删哪个节点
    ✅ 指出风险（如"删掉这步会让下游走不到"）

    ❌ **不直接改图** —— 提案要过人工审核（用户要求"每次参与执行需审核"）
    ❌ **不写代码** —— 新增真实业务环节需要人写实现；它只能在 `risks` 里
       说明"这需要先有人写 node_xxx"，或给 A3 的补丁留占位

## 为什么让它出 JSON 而不是"操作图对象"

照白模预演的成熟做法（LLM 出 JSON 规格、前端渲染）。理由：安全（不可能执行
任意代码）、出错可控（schema 校验、可重试）、可存（进工程一起存档）、
可复现、**人工可改**。提案的合法性由 `proposal.validate_ops()` 与
`graph_spec.validate_spec()` 两道校验守。
"""

from __future__ import annotations

import json
import logging

from app.workflows import graph_spec as gs
from app.workflows.proposal import Proposal, parse_proposal

logger = logging.getLogger("little_crew")

#: 给模型的系统提示。刻意把"能做什么/不能做什么"写进去 ——
#: 否则模型会兴奋地提议"新增字幕生成环节"，而那是做不到的（没有实现函数）。
_SYSTEM = """你是影视制作工作流的架构师。

你的任务：读懂用户对流程的诉求，输出一份**工作流变更提案**（JSON）。

## 你只能做这几件事（别的做不到，不要提）

1. `set_edge` / `remove_edge`：在**已有节点**之间增删连线（重排顺序）
2. `add_node` / `remove_node`：挂上或摘掉**已有实现**的节点
   - 允许新增的只有**人工确认点**（kind="review"），它们由通用实现支持
   - **不允许**新增业务环节（那需要先写代码，你没有这个能力）
3. `set_branch` / `remove_branch`：改条件边的分支目标
4. `set_entry` / `set_finish`：改入口/结束点

## 输出格式（只输出 JSON，不要解释）

{
  "summary": "一句话说清这次改什么",
  "ops": [
    {"op": "remove_node", "key": "critics", "why": "用户不需要剧评"},
    {"op": "set_edge", "from": "screenwriter", "to": "review_critics"},
    {"op": "set_branch", "from": "review_critics", "router": "simple_review",
     "branches": {"continue": "review_script", "redo": "screenwriter"}}
  ],
  "risks": ["删掉 critics 后，剧评产物不再生成"]
}

## 硬性要求

- `ops` 里每一步都要带 `why`（人要审核，得看懂你为什么这么改）
- **改了图的连通性就必须检查**：删掉一个节点后，它原来的上下游要接上，
  否则下游会"从入口走不到"（校验会拒绝，你也白改）
- 具体不要删节点就尽量别删（改完要用户二次确认，多一道手续）
- 拿不准就在 `risks` 里写清楚，不要硬改
"""


def _spec_digest(spec: dict) -> str:
    """把规格压成给模型看的紧凑描述（省 token，也更不容易看漏）。"""
    lines = [f"入口={spec.get('entry')}  结束={spec.get('finish')}", "", "节点："]
    for n in spec.get("nodes") or []:
        lines.append(f"  - {n['key']}（{n.get('kind')}）")
    lines.append("")
    lines.append("普通边：")
    for e in spec.get("edges") or []:
        lines.append(f"  {e['from']} -> {e['to']}")
    lines.append("")
    lines.append("条件边：")
    for e in spec.get("conditional") or []:
        br = "、".join(f"{k}={v}" for k, v in (e.get("branches") or {}).items())
        lines.append(f"  {e['from']} [{e['router']}]：{br}")
    return "\n".join(lines)


def propose(user_request: str, *, spec: dict | None = None,
            author: str = "") -> Proposal:
    """让架构师针对 `user_request` 出一份提案。

    **LLM 不可用时给确定性兜底**（与项目其它地方一致）：返回一份
    "无法自动生成"的提案并说明原因，而不是抛异常 —— 上层可以如实告诉用户，
    也可以走 A1（人工看建议自己改代码）那条路。

    Args:
        user_request: 用户的话（"我们不做配音了" / "把分镜提前到角色设计之前"）
        spec:         基于哪份规格提案（默认当前生产图）
        author:       记录提出者
    """
    spec = spec or gs.default_spec()
    text = (user_request or "").strip()
    if not text:
        return Proposal(summary="（没有说明要改什么）", ops=[],
                        risks=["请先说明想怎么改工作流"], author=author or "none")

    try:
        from app.services.llm import get_llm
    except Exception as exc:  # noqa: BLE001
        logger.warning("[architect] 取 LLM 失败：%s", exc)
        return Proposal(summary="（LLM 不可用，无法生成提案）", ops=[],
                        risks=[f"取 LLM 失败：{type(exc).__name__}"],
                        author=author or "none")

    try:
        llm = get_llm()
    except Exception as exc:  # noqa: BLE001
        return Proposal(summary="（LLM 不可用，无法生成提案）", ops=[],
                        risks=[f"初始化 LLM 失败：{type(exc).__name__}"],
                        author=author or "none")

    if getattr(llm, "mock", False):
        # mock 模式：给一份**明确标注是 mock** 的提案，不假装是真的分析
        return Proposal(
            summary="（mock 模式）示例提案：把「文学策划」从链上摘掉",
            ops=[
                {"op": "remove_node", "key": "critics",
                 "why": "mock 示例：演示「删节点需二次确认」这条流程"},
            ],
            risks=["这是 mock 数据，不是针对你的诉求做的分析"],
            author=author or "mock",
        )

    user = (
        "当前工作流：\n" + _spec_digest(spec)
        + "\n\n用户的诉求：\n" + text
        + "\n\n请输出变更提案 JSON。"
    )
    try:
        raw = llm.chat(_SYSTEM, user, role="workflow_architect")
    except TypeError:
        # 兼容只有 chat_json 的实现
        try:
            obj = llm.chat_json(_SYSTEM, user, role="workflow_architect")
            raw = json.dumps(obj, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[architect] 调用 LLM 失败：%s", exc)
            return _failure(exc, author)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[architect] 调用 LLM 失败：%s", exc)
        return _failure(exc, author)

    return parse_proposal(str(raw or ""), author=author or "llm")


def _failure(exc: Exception, author: str) -> Proposal:
    """把"调模型失败"变成一个**能指导下一步**的提案。

    ⚠️ 原先只写"（调用模型失败）"，用户看不到**为什么**，也不知道该怎么办。
    实测就踩到过：真实原因是"没有用户上下文 → 未选择 LLM 提供者"，
    而界面上只显示一句"调用模型失败"，完全查不出问题。
    所以这里把异常文本原样带进 `risks`。
    """
    msg = f"{type(exc).__name__}: {exc}"
    hint = ""
    if "未选择" in str(exc) or "提供者" in str(exc):
        hint = "（去设置页把「文字模型」的来源选上，架构师要调用它才能分析工作流）"
    return Proposal(
        summary="（未能生成提案：调用模型失败）",
        ops=[],
        risks=[msg + hint],
        author=author or "llm",
    )


def preview(prop: Proposal, *, spec: dict | None = None) -> dict:
    """把提案**预演**成"会变成什么样"（不生效），供审核界面展示。

    Returns:
        {"ok": bool, "changes": [人话变更日志], "problems": [拒绝原因],
         "before": {nodes, edges}, "after": {nodes, edges},
         "needs_second_confirm": bool}
    """
    from app.workflows.proposal import validate_ops, apply_ops

    base = spec or gs.default_spec()
    op_problems = validate_ops(prop.ops)

    before = {
        "nodes": len(base.get("nodes") or []),
        "edges": len(base.get("edges") or []),
        "conditional": len(base.get("conditional") or []),
    }

    if op_problems:
        return {
            "ok": False,
            "changes": [],
            "problems": [p.as_dict() for p in op_problems],
            "before": before, "after": before,
            "needs_second_confirm": prop.needs_second_confirm,
        }

    new_spec, log = apply_ops(base, prop.ops)
    spec_problems = gs.validate_spec(new_spec)
    errs = gs.errors_of(spec_problems)

    after = {
        "nodes": len(new_spec.get("nodes") or []),
        "edges": len(new_spec.get("edges") or []),
        "conditional": len(new_spec.get("conditional") or []),
    }

    return {
        "ok": not errs,
        "changes": log,
        "problems": [p.as_dict() for p in spec_problems],
        "before": before,
        "after": after,
        "needs_second_confirm": prop.needs_second_confirm,
        # 带上应用后的规格，审核通过时直接用，避免"审核的"与"生效的"不是同一份
        "spec": new_spec,
    }


__all__ = ["propose", "preview", "_SYSTEM"]
