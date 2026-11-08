"""**按任务类型路由模型**：让合适的模型做合适的事（2026-11-08）。

## 为什么不是"用更强的模型"

2026-11-08 实测：同一个分镜任务，`deepseek-flash` / `mimo` / `GLM` 各跑 5 次，
**产物完全一样**（11 镜、100% 遵守）—— 差别只有速度（27s / 72s / 273s）。

**结论：当任务被提示词与约束写死时，模型强弱不体现。**

真正体现差异的是**任务类型**：

| 任务类型 | 吃什么能力 | 例子 |
|---|---|---|
| `long_structure` | **长上下文 + 结构完整性** | 一集 8 场的剧本大纲、整集结构 |
| `strict_json` | **格式服从** | 分镜表、导演方案（字段多、易缺） |
| `prose` | **语言质感** | 台词润色、旁白 |
| `critique` | **跨族视角**（故意换家） | 剧本质询 |
| `vision` | 看得见图 | 画面质量判定（未接） |

**路由 = 给每种任务指定一个模型**，而不是给"所有活"配同一个。

## 三条纪律

1 **可测**：每种任务类型必须能拿出可数判据（否则不许说"它更适合"）
2 **可退**：某模型不可用时**退回默认**，并在结果里记 `routedBy: "fallback"`
3 **可审计**：每次路由写进指纹（provider/model/servedModel）
"""
from __future__ import annotations

#: 任务类型 -> 默认模型（可以在配置里覆盖；**没有证据前不要乱填**）
#:
#: 现状（2026-11-08）：
#:   · `strict_json` / `critique` 有实测；`long_structure` 正在测；`prose` 未测
#:   · 未测的一律指向默认模型 —— **不假装知道**
DEFAULT_MODEL = "deepseek-flash"

#: 各任务类型的**默认模型**与环境变量（覆盖优先级：环境变量 > 表）
ROUTES: dict[str, dict] = {
    "long_structure": {
        "model": DEFAULT_MODEL,
        "env": "LITTLE_CREW_MODEL_LONG",
        "why": "长剧本要一次给全 8 场，怕截断；用场数齐全率衡量",
        "evidence": "§6.18（实测中）",
    },
    "strict_json": {
        "model": DEFAULT_MODEL,
        "env": "LITTLE_CREW_MODEL_JSON",
        "why": "分镜/方案字段多，最怕结构缺字段；用配额遵守率衡量",
        "evidence": "§6.17（三模型产物一致）",
    },
    "prose": {
        "model": DEFAULT_MODEL,
        "env": "LITTLE_CREW_MODEL_PROSE",
        "why": "台词质感**尚无自动判据**，先不特殊化",
        "evidence": "未测",
    },
    "critique": {
        "model": "GLM-5.3-flash",
        "env": "LITTLE_CREW_MODEL_CRITIQUE",
        "why": "**刻意跨族**：同族模型审自己的稿 = 自我验证盲区",
        "evidence": "§3.10（设计决定，非调优）",
    },
}


def route(task: str) -> str:
    """任务类型 -> 模型 id。**未知任务回默认**（不猜、不报错）。"""
    import os

    spec = ROUTES.get(str(task or "").strip())
    if not spec:
        return DEFAULT_MODEL
    return str(os.environ.get(spec["env"]) or spec["model"])


def explain(task: str) -> dict:
    """给审计/文档用：为什么这个任务用这个模型。"""
    import os

    t = str(task or "").strip()
    spec = ROUTES.get(t)
    if not spec:
        return {"task": t, "model": DEFAULT_MODEL, "why": "未登记任务类型",
                "evidence": "", "routedBy": "default"}
    env_val = os.environ.get(spec["env"])
    return {"task": t, "model": str(env_val or spec["model"]),
            "why": spec["why"], "evidence": spec["evidence"],
            "routedBy": "env" if env_val else "table"}


def table() -> list[dict]:
    """当前路由表（诊断用）。"""
    return [explain(t) for t in ROUTES]
