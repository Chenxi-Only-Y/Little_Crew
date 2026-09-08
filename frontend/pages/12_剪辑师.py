"""剪辑师（editor/aggregator）— 独立控制台页。"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from _console import render_console_v2
from renderers import render_video

render_console_v2(
    role_key="editor", start_key="photographer",
    eyebrow="后期合成分发部 · 剪辑师", title="剪辑师",
    subtitle="整合图片、字幕成统一成片，并生成视频。",
    input_label="已生成画面 (JSON，可留空由 AI 生成)", placeholder='[{"shot_id":"S1-01","local_path":"..."}]',
    input_height=120,
    render_result=render_video,
    dept_page="pages/20_后期合成分发部.py",
)
