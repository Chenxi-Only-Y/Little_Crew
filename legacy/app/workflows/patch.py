"""A3：把生效过的图**落成代码补丁**，让改动进版本库、可回滚、可 review。

## 为什么需要它（A2 不够）

A2 换的是**运行时内存里的图** —— 服务一重启就回到代码里那张。
所以"能立刻试"和"改动留得住"是两件事：

    A2（运行时换图）  → 立刻生效，但重启即失效
    A3（代码补丁）    → 落成 diff，人工 merge 后才永久生效

用户要的是 **A2 + A3**：先用 A2 立刻试，满意了再用 A3 固定下来。

## 补丁长什么样

`data/proposals/<id>.patch`，内含：

  1. 一份**可读的规格快照**（JSON）—— 这是"变更后的真相"
  2. 一份 **unified diff**（`default_spec()` 的 JSON 表示 before/after）
     —— 给人 review 用
  3. 头部注释：提案 id、摘要、审批人、时间、图版本、如何应用

## 为什么用"数据 diff"而不是直接改 `main_workflow.py` 的源码

`main_workflow.py` 里的 `build_graph()` 已经改成**读规格**了，真正的拓扑在
`graph_spec.default_spec()` 这份**数据**里。所以：

  · 改数据 = 改图，**不需要动任何逻辑代码**；
  · 生成的 diff 是**数据**的 diff，比源码 diff 更小、更不容易误伤；
  · 合并方式明确：把新规格替换掉 `default_spec()` 的返回值。

（这也是当初"把拓扑搬进数据"的收益之一。）
"""

from __future__ import annotations

import difflib
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger("little_crew")


def _canon(spec: dict) -> str:
    """把规格规范化成稳定文本（排序键），这样 diff 只反映**真实变化**。"""
    clean = {k: v for k, v in spec.items() if not k.startswith("_")}
    return json.dumps(clean, ensure_ascii=False, indent=2, sort_keys=True)


def make_patch(rec: dict, spec: dict, *, approved_by: str = "",
               graph_version: int | None = None) -> tuple[str, Path]:
    """为一份**已批准**的提案生成补丁。

    Args:
        rec:           提案记录（`proposal_store` 里的那份）
        spec:          生效后的图规格
        approved_by:   审批人
        graph_version: 生效后的图版本号

    Returns:
        `(补丁文本, 落盘路径)`
    """
    from app.workflows import graph_spec as gs

    pid = rec.get("id") or "unknown"
    before = _canon(gs.default_spec())
    after = _canon(spec)

    diff = "".join(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile="a/app/workflows/graph_spec.py::default_spec",
        tofile=f"b/{pid}.json",
        n=3,
    ))

    header = [
        f"# 工作流变更补丁 —— {pid}",
        "#",
        f"# 摘要      : {rec.get('summary') or ''}",
        f"# 用户诉求  : {rec.get('request') or ''}",
        f"# 审批人    : {approved_by or rec.get('author') or ''}",
        f"# 生成时间  : {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"# 图版本    : {graph_version if graph_version is not None else '（只批准、未生效）'}",
        f"# 风险      : {'; '.join(rec.get('risks') or []) or '（未声明）'}",
        "#",
        "# 如何应用（改动**只在数据里**，不需要动逻辑代码）：",
        "#   1. 打开 app/workflows/graph_spec.py 的 default_spec()",
        "#   2. 用下面 SPEC 段落的内容替换它的返回值",
        "#   3. 重导前端快照：.venv\\Scripts\\python.exe scripts/export_registry.py",
        "#   4. 跑测试：.venv\\Scripts\\python.exe -m pytest tests -q",
        "#   5. 重启后端",
        "#",
        "# 回退：这个文件不改任何东西；删掉它不是回退，回退是**不用**它。",
        "",
        "# ======================== 变更日志 ========================",
    ]
    for line in rec.get("preview", {}).get("changes") or []:
        header.append(f"#   · {line}")
    header += [
        "",
        "# ===================== DIFF（人看这个）=====================",
        "",
    ]

    body = [
        diff or "（无变化）",
        "",
        "# ================ SPEC（机读：变更后的完整图）================",
        "# 这段就是 default_spec() 应该返回的内容。",
        "",
        after,
        "",
    ]

    text = "\n".join(header) + "\n".join(body)

    from app.workflows.proposal_store import proposals_dir

    path = Path(proposals_dir()) / f"{pid}.patch"
    tmp = path.with_suffix(".patch.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    logger.info("[patch] 已生成 %s（%d 字节）", path.name, len(text))
    return text, path


__all__ = ["make_patch"]
