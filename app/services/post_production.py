"""发行总监 —— 工作流的最后一环。

## 为什么这两个节点要"能跳过"

视频生成是**独立的异步后台任务**（几分钟 + 轮询，见 `/api/crew/video/{sid}`），
而工作流节点是**线性同步**的。把"等视频生成完"塞进图里会**阻塞整张图好几分钟**。

用户的决定："**剪辑师不是必需的，可以点跳过**"。

所以这里的做法是：

  · 节点在**输入还不存在时优雅跳过**（不是失败、不阻塞、如实说明怎么补上）；
  · 用户想用就先把视频跑出来，再单独跑这两步；不想用就直接跳过。

这样既把它接进了图（注册表声明与真实执行一致），又不改变执行模型。

## 为什么只有发行总监、没有单独的剪辑师

注册表里 `editor`（剪辑师）的 `workflow_node` **本来就是 `aggregator`**
（历史命名，`legacy_keys=('aggregator',)`）—— 也就是**聚合节点就是剪辑师**。

我第一版另外写了个 `edit_final_video` 想单独做剪辑，结果等于**两个剪辑师**
（标签还撞名）。撤掉重复节点后，那个函数就**没有调用者了**（真死代码），
已删除。

## 发行总监做什么

按大纲生成发布物料（标题/描述/标签）+ 各平台发布状态。

**发行总监不做真实上传**：那需要各平台 API 与 OAuth。
它产出 `publish_results`，其中未接入的平台如实标 `mock`
（实现见 `app/agents/distributor.py`，它一开始就是这么设计的）。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("little_crew")


def _session_payload(state: dict) -> dict:
    """取当前会话已落盘的产出（视频结果不在图状态里，在会话 payload）。"""
    sid = str(state.get("session_id") or "")
    if not sid:
        return {}
    try:
        from app.services.session_store import get_session_store

        row = get_session_store().get(sid) or {}
        return dict(row.get("payload") or {})
    except Exception as exc:  # noqa: BLE001
        logger.warning("[editor] 读取会话产出失败：%s", exc)
        return {}


def publish(state: dict) -> dict:
    """发行总监节点：生成发布物料 + 各平台状态。

    跳过条件与剪辑师一致（没有成片就没什么可发的）。
    """
    phase = "distributor"
    final = state.get("final_video") or {}
    if not final:
        logger.info("[distributor] 没有成片，跳过发行")
        return {
            "publish_results": {},
            "current_phase": phase,
            "distributor_note": ("还没有成片，已跳过发行。"
                                 "发行不是必需的 —— 生成视频（并剪辑）后再跑，"
                                 "或直接跳过。"),
        }

    # ⚠️ `Distributor.run()` 读的是 `state["video"]`（见它的 docstring：
    # "video: dict 最终视频（dir_or_path / produced，来自 payload.video）"），
    # 而图状态里那玩意儿叫 `final_video` —— 名字不一致。
    # 所以这里把 `final_video` **补成 `video`** 再交给它，否则它会当成
    # "没有视频"（我第一版就漏了这层，会被它的内部逻辑静默跳过）。
    payload_video = _session_payload(state).get("video") or {}
    run_state = dict(state)
    if not run_state.get("video"):
        run_state["video"] = (
            {"dir_or_path": final.get("dir_or_path", ""),
             "produced": final.get("segments", 0)}
            if not payload_video else payload_video)

    try:
        from app.agents.distributor import Distributor

        res = Distributor().run(run_state)
        if isinstance(res, dict) and "publish_results" in res:
            return {**res, "current_phase": phase}
        return {"publish_results": res if isinstance(res, dict) else {},
                "current_phase": phase}
    except Exception as exc:  # noqa: BLE001  发行失败不该让流程挂掉
        logger.warning("[distributor] 发行失败：%s", exc)
        return {
            "publish_results": {},
            "current_phase": phase,
            "distributor_note": f"发行失败：{type(exc).__name__}: {exc}",
        }
