"""日志与脱敏 —— 记录请求，且不回显任何密钥。

- redact(): 把常见的密钥/Token 形态替换为 ***，用于任何输出到日志/前端错误的信息。
- setup_logging(): 配置根 logger 级别（读 settings.log_level）。
"""

import logging
import re

from app.core.config import settings

# 常见密钥形态
_SECRET_RE = re.compile(
    r"sk-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._-]{8,}|AKIA[A-Za-z0-9]{16}|"
    r"(?:api[_-]?key|token|secret)\s*[=:]\s*['\"][A-Za-z0-9_\-]{8,}['\"]",
    re.IGNORECASE,
)


def redact(text) -> str:
    """把文本里的密钥/Token 脱敏。"""
    if not isinstance(text, str):
        return str(text)
    return _SECRET_RE.sub("***", text)


def setup_logging() -> logging.Logger:
    """配置日志并返回应用 logger。"""
    level = getattr(logging, (settings.log_level or "INFO").upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(h)
    return logging.getLogger("little-crew")
