"""启动 FastAPI 服务。

用法：
    python scripts/run_server.py
"""

import pathlib
import sys

import uvicorn

# 保证可从项目根目录 import app
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.config import settings


def main() -> None:
    uvicorn.run(
        "app.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=True,
    )


if __name__ == "__main__":
    main()
