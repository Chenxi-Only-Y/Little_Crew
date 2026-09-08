"""Agent 基类：约定统一的输入/输出契约。

每个 Agent 实现 `run(state) -> dict`：
  - 输入：从共享的 `CrewState` 读取所需字段。
  - 输出：返回增量 `dict`，由 LangGraph 合并进全局 State。

`.cursorrules` 要求每个 Agent 写明清晰的输入输出注释。
"""

from abc import ABC, abstractmethod

from app.core.state import CrewState


class BaseAgent(ABC):
    name: str = "base"

    @abstractmethod
    def run(self, state: CrewState) -> dict:
        """执行本 Agent 任务并返回状态增量。

        Args:
            state: 当前全局状态。

        Returns:
            dict: 本阶段产出的字段（如 story_outline / script / ...）。
        """
        raise NotImplementedError
