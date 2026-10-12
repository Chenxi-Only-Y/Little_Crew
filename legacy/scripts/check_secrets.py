"""提交前自检脚本：扫描仓库，检测可能泄漏的密钥 / Token。

用法：
    python scripts/check_secrets.py

被忽略的目录/文件（避免误报）：.git / .venv / data / 二进制编译物等。
返回码：0=未发现疑似密钥；非0=发现可疑内容（建议提交前修复）。
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IGNORE_DIRS = {".git", ".venv", "node_modules", "data", "__pycache__", ".idea", ".pytest_cache"}
IGNORE_GLOBS = {".env", "*.db", "*.sqlite", "*.pyc", "*.png", "*.jpg"}

# 常见密钥模式：硬编码在源码里的长字符串会被识别
PATTERNS = [
    # 赋值型：key = "xx..."  /  key: "xx..."
    (re.compile(r"(?i)(sk|pk|api[-_ ]?key|token|secret|password)\s*[=:]\s*['\"][A-Za-z0-9_\-]{16,}['\"]"),
     "赋值型密钥"),
    # 前缀型：常见平台 key 前缀
    (re.compile(r"(?i)\b(sk-|ghp_|github_pat_|xox[baprs]-|AKIA|AIza|pk_(live|test)_|put_)[A-Za-z0-9_\-]{10,}\b"),
     "前缀型密钥"),
    # 环境变量模板里若填了非空也可被抓到
    (re.compile(r"(?i)^\s*[A-Z0-9_]*KEY\s*=\s*[A-Za-z0-9_\-]{16,}"), "环境变量内联密钥"),
]


def scan(path: Path) -> bool:
    found = False
    for p in path.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        parts = set(rel.parts)
        if any(d in IGNORE_DIRS for d in parts):
            continue
        if any(rel.match(g) for g in IGNORE_GLOBS):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for regex, label in PATTERNS:
                if regex.search(line):
                    print(f"[潜在泄漏] {rel}:{lineno} ({label})")
                    print(f"    {line.strip()[:90]}")
                    found = True
    return found


if __name__ == "__main__":
    print("扫描目录：", ROOT)
    hit = scan(ROOT)
    print("---")
    print("发现疑似密钥，请勿提交！" if hit else "未发现疑似密钥 ✓")
    sys.exit(1 if hit else 0)
