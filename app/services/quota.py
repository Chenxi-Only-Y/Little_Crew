"""配额与限流守卫 —— 控制成本与滥用。

- rate_limit: 每（IP+用户）每分钟请求上限（内存滑动窗口）。
- daily quota: 每用户每日创作上限（基于 sessions 表当日创建数，持久化可跨重启）。

用法：
    from app.services.quota import quota
    ok, msg = quota.check(ip, user_id)   # rate + daily 都通过才 True
"""

import threading
import time
from collections import defaultdict, deque
from datetime import datetime

from app.core.config import settings
from app.services.session_store import get_session_store


class QuotaGuard:
    def __init__(self, per_minute: int = 10, daily: int = 5) -> None:
        self.per_minute = per_minute
        self.daily = daily
        self._lock = threading.Lock()
        self._hits: dict[str, deque] = defaultdict(deque)

    def _rate_ok(self, key: str, now: float | None = None) -> bool:
        """调用是否落在每分钟限制内；通过则记一次。"""
        now = now or time.time()
        with self._lock:
            dq = self._hits[key]
            while dq and now - dq[0] > 60:
                dq.popleft()
            if len(dq) >= self.per_minute:
                return False
            dq.append(now)
            return True

    def _daily_ok(self, user_id: str, now: float | None = None) -> bool:
        """该用户今日创作数是否未达上限。"""
        now = now or time.time()
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        count = 0
        for it in get_session_store().list_by_user(user_id):
            created = it.get("created")
            if created and datetime.fromtimestamp(created).strftime("%Y-%m-%d") == today:
                count += 1
        return count < self.daily

    def check(self, ip: str, user_id: str) -> tuple[bool, str]:
        """返回 (是否允许, 提示信息)。"""
        if not self._rate_ok(f"{ip}:{user_id}"):
            return False, "请求过于频繁，请稍后再试。"
        if not self._daily_ok(user_id):
            return False, "今日创作额度已用尽，请明天再来。"
        return True, "ok"


quota = QuotaGuard(
    per_minute=settings.rate_limit_per_minute,
    daily=settings.quota_daily_generations,
)
