"""`review` 层 —— 确定性验收门禁（核心创新点 C3）。

职责边界（由依赖方向强制，不靠约定）
------------------------------------
| 包 | 做什么 | **不做什么** |
|---|---|---|
| `runtime` | 生成、提交、回收产物 | **不做判定** |
| `review` | **判定产物是否合格** | **不做生成** —— 拿不到通道，也没有提交作业的能力 |

两者同层（[4]）且互不 import。这不是洁癖：**生成方自己判定合格 = 自我签收**，
而自我签收正是 C3 要消灭的问题（见策划书 §3.3）。

三层把关
--------
| 层 | 机制 | 落在哪 |
|---|---|---|
| **L1** | **确定性断言算子** | 本包 `ops.py` + `gate.py` |
| **L2** | 一致性嵌入（连续可度量，看漂移趋势） | 本包 `embedding.py` |
| **L3** | 跨模型族盲读（生成方 ≠ 审核方） | `agent/orchestrator.py`（已有） |

L3 不在本包，因为它是**协作机制**（谁审谁、模型族怎么配对），
属于 `agent` 的职责。本包只提供 L1 与 L2 这两个**判定原语**。

公开 API
--------
```python
from little_crew.review import Gate, Observation, GateVerdict

gate = Gate()
result = gate.evaluate(ir["constraints"], Observation(
    artifact_id="SH-E100-001-001",
    values={"similarity": 0.71},
    source="embedder:v1"))

result.verdict     # GateVerdict.RETRY
result.repairs     # ('[cr_face_007] 身份相似度 0.710 不在 [0.820, 1.000] 内…',)
```
"""

from __future__ import annotations

from little_crew.review.constraint import (
    REGISTRY,
    AssertionError_,
    AssertionOutcome,
    ConstraintAssertion,
    Enforcement,
    OperatorRegistry,
    UnknownOperatorError,
)
from little_crew.review.gate import Gate, GateResult, GateVerdict, Observation

# 让 `review` 的门禁能力有一个统一入口 —— 它被 runtime 层
# （`runtime/gate_runner.py`）作为模块使用。放在这里而不是让 runtime
# 深入 import 子模块，是为了让"review 的公开面"只有一处定义。
from little_crew.review.gate_runner import GateRunner, ShotGateReport

# 导入即注册内置算子（`ops` 模块在 import 时执行 @register 装饰器）。
#
# ⚠️ 这一行**不能用"懒加载"替代**：门禁若在算子未注册时运行，
# 会把所有约束判成"未知算子 -> ESCALATE"，而那是**静默的** ——
# 表现为"门禁突然全部升级人工"，看不出原因。
from little_crew.review import ops as _ops  # noqa: E402,F401

__all__ = [
    "Gate",
    "GateRunner",
    "ShotGateReport",
    "GateResult",
    "GateVerdict",
    "Observation",
    "ConstraintAssertion",
    "AssertionOutcome",
    "Enforcement",
    "OperatorRegistry",
    "UnknownOperatorError",
    "AssertionError_",
    "REGISTRY",
]
