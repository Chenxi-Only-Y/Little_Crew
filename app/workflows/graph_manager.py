"""工作流的**可替换图**管理器（A2 运行时改图的基座）。

## 为什么需要它

原先是一句模块级常量：

    app_workflow = compile_graph()          # 每个图**自带一个新的 MemorySaver**
    task_runner: from app.workflows.main_workflow import app_workflow   # 按值导入

于是"运行时换一张图"会同时踩三个坑：

  1. **换图 = 换 checkpointer** —— 所有在途会话的断点全丢。
     正在确认点等用户点"通过"的会话会突然变成"检查点丢失"。
  2. **按值导入看不到替换** —— 就算改了 `main_workflow.app_workflow`，
     `task_runner` 里那个名字早已绑定到旧对象。
  3. **旧检查点是按旧图的节点名存的** —— 新图没有同名节点时，
     `get_state()` / `resume_run` 会炸。

## 这个模块怎么解决

  · **一个 checkpointer 实例贯穿始终**（`_checkpointer`），换图只换"图"不换"记忆"；
  · 图放在这里，`task_runner` 通过 `current_graph()` **每次取**，
    而不是在导入时绑定；
  · 每次替换都**记版本 + 保留历史**，可回滚、可追溯；
  · `swap()` 只接受**已经编译好**的图 —— 拓扑校验等前置检查由调用方负责，
    这里不做业务判断（保持单一职责，也便于测试）。

## 与 `main_workflow.app_workflow` 的关系

那个模块级名字**保留**（很多地方 `import` 它、测试也 monkeypatch 它），
但语义降级为"**初始那张图**"。真正要在运行时用图的代码必须走
`current_graph()`，否则拿到的永远是第一版。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("little_crew")


@dataclass
class GraphVersion:
    """一张图的版本记录（供追溯与回滚）。"""

    version: int
    reason: str
    #: 这次替换由什么触发（如 `"architect_proposal:p-1234"` / `"initial"`）
    source: str = ""
    #: 提案快照（JSON 可序列化，便于事后审计"当时为什么这么改"）
    proposal: dict | None = None
    #: 被替换掉的那张图（用于回滚）
    previous: Any = None
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "reason": self.reason,
            "source": self.source,
            "proposal": self.proposal,
            "meta": self.meta,
            "has_previous": self.previous is not None,
        }


class GraphManager:
    """持有**唯一** checkpointer 与当前图；负责替换、版本、回滚。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._graph: Any = None
        self._version = 0
        self._history: list[GraphVersion] = []
        self._checkpointer: Any = None

    # ------------------------------------------------------------------
    # checkpointer：**唯一实例**，整个进程生命周期内不替换
    # ------------------------------------------------------------------
    def checkpointer(self) -> Any:
        """取共享 checkpointer（惰性创建）。"""
        with self._lock:
            if self._checkpointer is None:
                from langgraph.checkpoint.memory import MemorySaver

                self._checkpointer = MemorySaver()
                logger.info("[graph] 创建共享 checkpointer（%s）",
                            type(self._checkpointer).__name__)
            return self._checkpointer

    # ------------------------------------------------------------------
    # 安装初始图
    # ------------------------------------------------------------------
    def install_initial(self, graph: Any, *, source: str = "initial") -> None:
        """装入第一张图。只应在进程启动时调一次。"""
        with self._lock:
            if self._graph is not None:
                raise RuntimeError(
                    "已经装过初始图了；运行时换图请用 swap()，"
                    "否则会丢掉版本历史"
                )
            self._graph = graph
            self._version = 1
            self._history.append(GraphVersion(
                version=1, reason="进程启动时的初始图", source=source))
            logger.info("[graph] 初始图已装入（version=1）")

    # ------------------------------------------------------------------
    # 取当前图
    # ------------------------------------------------------------------
    def current_graph(self) -> Any:
        """**每次都要走这里**，不要 `from ... import` 绑定。

        ## 为什么要惰性安装（而不是靠导入副作用）

        初始图原本是靠 `main_workflow` 模块底部那句
        `app_workflow = _install_initial_graph()` 装进来的 —— 也就是**导入副作用**。

        于是有个坑：**如果没有任何代码 import `main_workflow`，图就永远没装**。
        实测就撞上了：`tests/test_checkpoint_resume.py` 只导入 `task_runner`，
        而 `task_runner` 现在只导入 `graph_manager`（不导入 main_workflow），
        注册表那边又用了 `TYPE_CHECKING` 以免循环导入 ——
        结果三个条件凑齐，`current_graph()` 抛"图还没装入"。

        所以这里补一手**惰性安装**：真要用图而它还没装时，主动去把
        `main_workflow` 导进来（它的模块级代码会调 `install_initial`）。
        这样无论谁先导入、有没有人导入 main_workflow，都能拿到图。
        """
        with self._lock:
            if self._graph is None:
                self._lazy_install_locked()
            if self._graph is None:
                raise RuntimeError("图还没装入（install_initial 未生效）")
            return self._graph

    def _lazy_install_locked(self) -> None:
        """在持锁状态下惰性装入初始图。调用方必须是 `current_graph` 等持锁者。"""
        import importlib

        # 导入即触发 main_workflow 底部的 install_initial（见其模块说明）
        importlib.import_module("app.workflows.main_workflow")

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    # ------------------------------------------------------------------
    # 替换（A2 的核心动作）
    # ------------------------------------------------------------------
    def swap(self, new_graph: Any, *, reason: str, source: str = "",
             proposal: dict | None = None, meta: dict | None = None) -> int:
        """把当前图换成 `new_graph`，返回新版本号。

        **不换 checkpointer** —— 这是"在途会话不断"的关键。

        Args:
            new_graph: **已编译**的图。调用方负责先做拓扑校验等前置检查。
            reason:    人话说明（会进日志与历史，供事后追溯）。
            source:    触发来源标识，如 `architect_proposal:p-1234`。
            proposal:  提案快照（落盘审计用）。
            meta:      其它要记录的元信息。
        """
        if new_graph is None:
            raise ValueError("new_graph 不能为空")
        with self._lock:
            old = self._graph
            self._graph = new_graph
            self._version += 1
            rec = GraphVersion(
                version=self._version, reason=reason, source=source,
                proposal=proposal, previous=old, meta=dict(meta or {}),
            )
            self._history.append(rec)
            logger.warning("[graph] 换图 → version=%s  原因=%s  来源=%s",
                           self._version, reason, source or "(未标)")
            return self._version

    # ------------------------------------------------------------------
    # 回滚
    # ------------------------------------------------------------------
    def can_rollback(self) -> bool:
        with self._lock:
            return bool(self._history and self._history[-1].previous is not None)

    def rollback(self, *, reason: str = "手动回滚") -> int:
        """回到上一张图（各版本自带 `previous`，所以用的是**当时那张**）。"""
        with self._lock:
            if not self._history or self._history[-1].previous is None:
                raise RuntimeError("没有可回滚的上一版图")
            prev = self._history[-1].previous
            self._graph = prev
            self._version += 1
            self._history.append(GraphVersion(
                version=self._version, reason=reason, source="rollback",
                previous=None,
            ))
            logger.warning("[graph] 已回滚 → version=%s（%s）", self._version, reason)
            return self._version

    # ------------------------------------------------------------------
    # 观测
    # ------------------------------------------------------------------
    def history(self) -> list[dict]:
        with self._lock:
            return [h.as_dict() for h in self._history]

    def snapshot(self) -> dict:
        """给 API/界面用的当前状态。

        ⚠️ 会**先惰性装入**（若还没装）—— 否则在"图尚未被任何人取用"的时刻
        查询，会得到 `version=0 / nodes=0` 这种**看起来像故障**的结果
        （我自己在独立脚本里查时就先被它误导了一次）。
        """
        with self._lock:
            if self._graph is None:
                self._lazy_install_locked()
            nodes: list[str] = []
            if self._graph is not None:
                try:
                    nodes = sorted(self._graph.nodes)
                except Exception:  # noqa: BLE001  图对象形态异常时不影响状态查询
                    nodes = []
            return {
                "version": self._version,
                "nodes": len(nodes),
                "checkpointer": (type(self._checkpointer).__name__
                                 if self._checkpointer is not None else ""),
                "can_rollback": bool(self._history
                                     and self._history[-1].previous is not None),
                # 最近 5 条，新的在前
                "history": [h.as_dict() for h in self._history[-5:]][::-1],
            }


#: 进程级唯一实例
MANAGER = GraphManager()


def current_graph():
    """取当前图。**运行时用图的代码一律走这个函数。**"""
    return MANAGER.current_graph()


def swap_graph(new_graph, *, reason: str, source: str = "",
               proposal: dict | None = None, meta: dict | None = None) -> int:
    return MANAGER.swap(new_graph, reason=reason, source=source,
                        proposal=proposal, meta=meta)
