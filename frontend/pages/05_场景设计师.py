"""场景设计师（scene_designer）— 独立控制台页（纳入分段管线）。"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from _console import render_console_v2
from renderers import render_scene

render_console_v2(
    role_key="scene_designer", start_key="scene_designer",
    eyebrow="美术资产部 · 场景设计师", title="场景设计师",
    subtitle="生成关键场景氛围参考图，确定故事发生的“舞台”风格。",
    input_label="分场剧本 (JSON)", placeholder='{"title":"剧本","scenes":[]}', input_height=120,
    render_result=render_scene,
    dept_page="pages/20_美术资产部.py",
    existing_labels={"scene_design_profiles": "已有场景描述"},
    image_fields={"scene_reference_images": "已有场景参考图"},
    art_brief_label="美术设定包 (可选 · 无剧本时据此独立生成场景)",
)
