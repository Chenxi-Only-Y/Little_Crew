"""编剧（screenwriter）— 独立控制台页。"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from _console import render_console_v2
from renderers import render_script

render_console_v2(
    role_key="screenwriter", start_key="screenwriter",
    eyebrow="创作策划部 · 编剧", title="编剧",
    subtitle="把大纲细化成含场景、对话、情绪的完整分场剧本。",
    input_label="故事大纲 (JSON)", placeholder='{"title":"标题","scenes":[]}', input_height=120,
    render_result=render_script,
    dept_page="pages/20_创作策划部.py",
    existing_labels={"script": "已有剧本"},
)
