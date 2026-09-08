"""分镜师（storyboarder）— 独立控制台页。"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from _console import render_console_v2
from renderers import render_storyboard

render_console_v2(
    role_key="storyboarder", start_key="storyboarder",
    eyebrow="导演分镜部 · 分镜师", title="分镜师",
    subtitle="把剧本翻译成镜头语言，规划景别、角度、运镜。",
    input_label="分场剧本 (JSON)", placeholder='{"title":"剧本","scenes":[]}', input_height=120,
    render_result=render_storyboard,
    dept_page="pages/20_导演分镜部.py",
    existing_labels={"storyboard": "已有分镜"},
)
