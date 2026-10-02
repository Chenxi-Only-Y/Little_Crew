"""小剧组 (Little Crew) — 运行期事件广播（供 SSE 实时通道）。

设计取舍：
    `app_workflow.stream()` 是**一次性迭代器**，无法被多个消费者共享。
    因此运行器（task_runner）把中途的关键节点**广播**出来，
    任意多个 SSE 订阅者各自从队列里取，互不影响。

事件类型：
    status    会话整体状态变化（running / waiting_review / done / failed / cancelled）
    stage     某个环节状态变化（running / done / skipped / review / failed）
    thinking  「AI 思考过程」增量
    review    到达人工确认点（带审核内容）
    error     出错
    done      正常结束

范围与限制（写清楚，避免误用）：
    - **进程内**：多 worker 部署时事件不跨进程（与 `task_runner._JOBS` 同性质）。
      单进程下完全可用；跨进程需要换成 Redis pub/sub 之类，属 v2 后续。
    - 队列有上限：慢消费者不会拖垮生产者，超出后丢弃最旧事件并置 `dropped` 标记。
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator

#: 单个订阅者队列上限（条）。慢消费者丢最旧的，避免内存无限增长。
_QUEUE_MAX = 512

#: 没有事件时 SSE 长连接的保活间隔（秒）
HEARTBEAT_SECONDS = 20.0


@dataclass
class Event:
    """一条运行期事件。"""

    type: str
    session_id: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    seq: int = 0

    def to_sse(self) -> str:
        """序列化为 SSE 帧（`event:` + `data:`）。"""
        import json

        payload = {
            "type": self.type,
            "session_id": self.session_id,
            "ts": self.ts,
            "seq": self.seq,
            **self.data,
        }
        return f"event: {self.type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


class EventHub:
    """进程内事件广播中心。

    订阅者通过 `subscribe()` 拿到一个独立队列；`publish()` 会扇出到所有在线队列。
    提供 `history()` 让后来者能补上已发生的事件（避免"晚连上看不到进度"）。
    """

    def __init__(self, history_size: int = 200) -> None:
        self._lock = threading.Lock()
        self._subs: dict[str, set[queue.Queue]] = {}
        self._history: dict[str, list[Event]] = {}
        self._seq: dict[str, int] = {}
        self._closed: set[str] = set()
        self._history_size = history_size

    # ---------------- 发布 ----------------

    def publish(self, session_id: str, type_: str, **data: Any) -> Event:
        """广播一条事件，并记入历史。"""
        with self._lock:
            seq = self._seq.get(session_id, 0) + 1
            self._seq[session_id] = seq
            ev = Event(type=type_, session_id=session_id, data=data, seq=seq)

            hist = self._history.setdefault(session_id, [])
            hist.append(ev)
            if len(hist) > self._history_size:
                del hist[: len(hist) - self._history_size]

            # 新一轮事件到来即视为"未结束"（支持同一 session 反复运行）
            self._closed.discard(session_id)
            targets = list(self._subs.get(session_id, ()))

        for q in targets:
            try:
                q.put_nowait(ev)
            except queue.Full:
                # 慢消费者：丢最旧的，再放新的
                try:
                    q.get_nowait()
                    q.put_nowait(ev)
                except (queue.Empty, queue.Full):
                    pass
        return ev

    # ---------------- 订阅 ----------------

    def subscribe(self, session_id: str, *, replay: bool = True) -> queue.Queue:
        """订阅某会话的事件。

        Args:
            replay: 是否先把已发生的事件补进队列（晚连上也能看到进度）
        """
        q: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
        with self._lock:
            if replay:
                for ev in self._history.get(session_id, ()):
                    try:
                        q.put_nowait(ev)
                    except queue.Full:
                        break
            self._subs.setdefault(session_id, set()).add(q)
            # 已结束的会话：立刻放哨兵，避免订阅者永久等待
            if session_id in self._closed:
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass
        return q

    def unsubscribe(self, session_id: str, q: queue.Queue) -> None:
        with self._lock:
            subs = self._subs.get(session_id)
            if subs:
                subs.discard(q)
                if not subs:
                    self._subs.pop(session_id, None)

    def stream(self, session_id: str, *, replay: bool = True,
               heartbeat: float = HEARTBEAT_SECONDS) -> Iterator[str]:
        """产出 SSE 文本帧的生成器（含保活注释行）。

        结束条件（两者满足其一即结束，**不依赖哨兵一定送达**）：
          1. 队列里取到 None 哨兵
          2. 会话已被标记 closed **且** 队列已排空（防止哨兵因队列满丢失后永久等待）
        """
        q = self.subscribe(session_id, replay=replay)
        try:
            yield ": connected\n\n"
            while True:
                try:
                    ev = q.get(timeout=heartbeat)
                except queue.Empty:
                    # 空闲：若不是"已结束且排空"，就发保活
                    if self.is_closed(session_id):
                        break
                    yield ": keep-alive\n\n"
                    continue
                if ev is None:  # 结束哨兵
                    break
                yield ev.to_sse()
        finally:
            self.unsubscribe(session_id, q)

    # ---------------- 辅助 ----------------

    def close_stream(self, session_id: str) -> None:
        """标记会话结束，并尽力通知所有订阅者（放 None 哨兵）。

        注意：**先置 closed 再放哨兵**——哨兵可能因为队列满而放不进去，
        此时 `stream()` 依靠 closed 标记 + 队列排空来结束，不会永久阻塞。
        """
        with self._lock:
            self._closed.add(session_id)
            targets = list(self._subs.get(session_id, ()))
        for q in targets:
            try:
                q.put_nowait(None)
            except queue.Full:
                # 队列满：哨兵放不进去也没关系，stream() 会靠 closed 标记退出
                pass

    def is_closed(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._closed

    def history(self, session_id: str) -> list[Event]:
        with self._lock:
            return list(self._history.get(session_id, ()))

    def subscriber_count(self, session_id: str) -> int:
        with self._lock:
            return len(self._subs.get(session_id, ()))

    def reset(self, session_id: str) -> None:
        """清空某会话的历史与序号（新一轮运行时调用）。"""
        with self._lock:
            self._history.pop(session_id, None)
            self._seq.pop(session_id, None)
            self._closed.discard(session_id)


_hub: EventHub | None = None
_hub_lock = threading.Lock()


def get_hub() -> EventHub:
    """获取（单例）事件中心。"""
    global _hub
    if _hub is None:
        with _hub_lock:
            if _hub is None:
                _hub = EventHub()
    return _hub


def new_event_id() -> str:
    """给需要唯一标识的事件用。"""
    return uuid.uuid4().hex[:12]
