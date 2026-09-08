"""摄影师（photographer）— 独立控制台页。"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from _console import render_console_v2
from renderers import render_images

render_console_v2(
    role_key="photographer", start_key="photographer",
    eyebrow="技术制作部 · 摄影师", title="摄影师",
    subtitle="根据技术指导的指令和角色参考图，拍摄出实际画面。",
    input_label="结构化提示词 (JSON)", placeholder='[{"shot_id":"S1-01","prompt":""}]', input_height=120,
    render_result=render_images,
    dept_page="pages/20_技术制作部.py",
    image_fields={"images": "已有画面"},
)
