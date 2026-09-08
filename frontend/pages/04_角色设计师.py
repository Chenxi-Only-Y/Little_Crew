"""角色设计师（character_designer）— 独立控制台页（纳入分段管线）。"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from _console import render_console_v2
from renderers import render_characters

render_console_v2(
    role_key="character_designer", start_key="character_designer",
    eyebrow="美术资产部 · 角色设计师", title="角色设计师",
    subtitle="根据剧本描述，生成角色标准参考图与外貌锚定。",
    input_label="故事大纲 (JSON)", placeholder='{"title":"标题","characters":[]}', input_height=120,
    render_result=render_characters,
    dept_page="pages/20_美术资产部.py",
    existing_labels={"character_profiles": "已有角色描述"},
    image_fields={"character_reference_images": "已有角色参考图"},
    art_brief_label="美术设定包 (可选 · 无剧本时据此独立生成角色)",
)
