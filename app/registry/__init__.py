"""小剧组 (Little Crew) — 注册表（唯一数据源）。

对外只暴露 `api` 里的查询函数与 `types` 里的类型；数据本身在 `roles.py`。

用法：
    from app.registry import api
    api.snapshot()            # 供 GET /api/registry
    api.execution_order()     # 由依赖推导的执行顺序
    api.validate_topology([...])
"""

from app.registry import api  # noqa: F401  便于 `from app.registry import api`

__all__ = ["api"]
