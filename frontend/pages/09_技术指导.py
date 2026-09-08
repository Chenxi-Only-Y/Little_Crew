"""技术指导（director）— 独立控制台页。"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from _console import render_console_v2
from renderers import render_prompts

render_console_v2(
    role_key="director", start_key="director",
    eyebrow="技术制作部 · 技术指导", title="技术指导",
    subtitle="把文学描述翻译成 SD/FLUX 等模型可理解的结构化指令，并注入角色锚定。",
    input_label="分镜脚本 (JSON)", placeholder='[{"shot_id":"S1-01","description":""}]', input_height=120,
    render_result=render_prompts,
    dept_page="pages/20_技术制作部.py",
    existing_labels={"prompts": "已有提示词"},
)
