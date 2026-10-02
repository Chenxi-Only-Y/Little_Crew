"""日志与脱敏 —— 记录请求，且不回显任何密钥。

- redact(): 把常见的密钥/Token 形态替换为 ***，用于任何输出到日志/前端错误的信息。
- setup_logging(): 配置根 logger 级别（读 settings.log_level）。

⚠️ 这个函数的用途是"兜底"：调用点往往是把 **provider 返回的错误体**或异常文本
回给前端/写进日志，而那些内容里**经常带着 key**（各家错误体格式不一）。
所以宁可多遮一点，也不要漏——漏一次就是把凭据写进日志文件。
"""

import json
import logging
import re

from app.core.config import settings

#: 参数名里出现这些词，就认为它的值敏感
_SECRET_KEY_HINTS = ("api_key", "apikey", "api-key", "token", "secret",
                     "password", "passwd", "authorization", "credential",
                     "access_key", "private_key")

#: 与参数名无关的**独立**密钥形态
_SECRET_RE = re.compile(
    # 各家前缀
    r"sk-[A-Za-z0-9_\-]{8,}"
    r"|sk_[A-Za-z0-9_\-]{8,}"
    r"|AKIA[A-Za-z0-9]{16}"
    r"|gh[pousr]_[A-Za-z0-9]{16,}"
    r"|xox[baprs]-[A-Za-z0-9\-]{10,}"
    # JWT（三段 base64url）——provider 错误体里很常见
    r"|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{5,}"
    # Authorization 头
    r"|Bearer\s+[A-Za-z0-9._\-]{8,}",
    re.IGNORECASE,
)

#: `key=value` / `key: value` —— 值**可带引号也可不带**
#: 原先只匹配带引号的值，于是 `api_key=minimax-secret-value` 原样漏出。
_KV_RE = re.compile(
    r"(?P<name>[\"']?(?:api[_-]?key|apikey|token|secret|password|passwd|"
    r"authorization|credential|access[_-]?key|private[_-]?key)[\"']?\s*[=:]\s*)"
    r"(?P<val>\"[^\"]{4,}\"|'[^']{4,}'|[^\s,;&}\)\]\}]{4,})",
    re.IGNORECASE,
)


def _is_secret_key(name: str) -> bool:
    n = name.lower().replace("-", "_")
    return any(h.replace("-", "_") in n for h in _SECRET_KEY_HINTS)


def _scrub_obj(obj, depth: int = 0):
    """递归按**键名**遮蔽 dict 里的敏感值。

    只做正则是不够的：`{"api_key": "minimax-secret-..."}` 里那个值没有任何
    可识别的"密钥前缀"，只能靠键名判定。

    ⚠️ 超过深度上限时**必须保守遮蔽**，不能原样返回。
    这里原先写的是 `if depth > 6: return obj` —— 于是第 7 层往里的
    `{"api_key": "..."}` 会**原样通过**（实测确认漏出）。而 provider 的错误体
    恰恰是深度嵌套的 JSON，正好落在被放弃的那一段。
    现在改为返回 `"***"`：宁可把无关键也遮掉，也不能漏一个密钥。
    """
    if depth > 6:
        return "***" if obj is not None else obj
    if isinstance(obj, dict):
        return {
            k: ("***" if _is_secret_key(str(k)) and v else _scrub_obj(v, depth + 1))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_scrub_obj(v, depth + 1) for v in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj


def redact(text) -> str:
    """把文本里的密钥/Token 脱敏。

    覆盖四类（都是实测漏过的形态）：
      1. 结构化对象（dict/list）→ 按**键名**递归遮蔽；
      2. JSON 字符串 → 先按 JSON 解析再递归遮蔽，失败才退回纯文本；
      3. `key=value` / `key: "value"`（**含无引号的值**）；
      4. 独立形态：`sk-*`、`AKIA*`、JWT、`Bearer *` 等。
    """
    if isinstance(text, (dict, list)):
        try:
            return json.dumps(_scrub_obj(text), ensure_ascii=False)
        except (TypeError, ValueError):
            return "***"
    if not isinstance(text, str):
        return str(text)

    out = text
    # 2) 整串是 JSON 时先结构化处理（能按键名精确遮蔽）
    stripped = out.strip()
    if stripped[:1] in ("{", "[") and stripped[-1:] in ("}", "]"):
        try:
            parsed = json.loads(stripped)
            return json.dumps(_scrub_obj(parsed), ensure_ascii=False)
        except (ValueError, TypeError):
            pass  # 不是合法 JSON，继续走文本路径

    # 3) key=value（含无引号值）；用函数保留参数名，只替换值
    out = _KV_RE.sub(lambda m: f"{m.group('name')}***", out)
    # 4) 独立形态
    out = _SECRET_RE.sub("***", out)
    return out


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
