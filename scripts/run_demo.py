"""命令行 MVP：跑通「一句话创意 -> 漫剧图文序列」，无需前端与服务。

用法：
    python scripts/run_demo.py "一只会说话的小猫想当侦探"
"""

import json
import pathlib
import sys

# 保证可从项目根目录 import app
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.workflows.main_workflow import run_once


def main() -> None:
    user_input = sys.argv[1] if len(sys.argv) > 1 else "一只会说话的小猫想当侦探"
    session_id = sys.argv[2] if len(sys.argv) > 2 else "demo"
    state = {"user_input": user_input, "session_id": session_id}

    result = run_once(state)
    output = result.get("result", result)
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
