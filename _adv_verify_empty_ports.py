"""对抗验证：注册表声明但实现不产出的产物（空端口）。

验证目标：
  A. validate_topology 是否真的把 control_inputs / project_brief / adaptation_plan /
     scene_whitebox / consistent_takes 当成"依赖已满足"。
  B. 这些 key 在实现里到底有没有写入点（静态扫描 + 实际跑节点）。
"""
from __future__ import annotations

import ast
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.registry import api  # noqa: E402

TARGETS = ["control_inputs", "project_brief", "adaptation_plan",
           "scene_whitebox", "consistent_takes", "fixed_images"]

print("=" * 78)
print("1) 谁声明 produces 这些产物")
print("=" * 78)
declared = {t: [] for t in TARGETS}
for n in api.all_nodes():
    for t in TARGETS:
        if t in n.produces:
            declared[t].append(n.key)
for t in TARGETS:
    print(f"  {t:18s} producers={declared[t] or '(无声明者)'}")

print()
print("=" * 78)
print("2) validate_topology：选出「生产者 + 消费者」看是否报缺失依赖")
print("=" * 78)
NODES = ["storyboarder", "character_designer", "scene_designer", "previs_artist",
         "technical_director", "photographer", "quality_assessor", "showrunner",
         "script_editor", "screenwriter", "inpainter"]
res = api.validate_topology(NODES, available_inputs=["user_input", "art_brief"])
print("  chosen:", NODES)
print("  ok =", res["ok"], " errors =", res["errors"])
produced = set()
for k in NODES:
    produced.update(api.node(k).produces)
print("  校验器认为可满足(由已选节点产出)：",
      sorted(a for a in TARGETS if a in produced))

print()
print("=" * 78)
print("3) 反向对照：只选消费者（不选生产者）应当报缺失依赖")
print("=" * 78)
for only, art in ((["photographer"], "control_inputs"),
                  (["quality_assessor"], "consistent_takes"),
                  (["previs_artist"], "scene_whitebox")):
    r2 = api.validate_topology(only, available_inputs=["user_input", "art_brief"])
    miss = r2["unmatched_inputs"].get(only[0], [])
    print(f"  只选 {only[0]:16s} → ok={r2['ok']}  缺={miss}  (关注 {art}: {art in miss})")

print()
print("=" * 78)
print("4) 静态扫描 app/**.py：这些 key 出现在哪些文件（字面量）")
print("=" * 78)
hits = {t: {} for t in TARGETS}
for py in sorted((ROOT / "app").rglob("*.py")):
    src = py.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in TARGETS:
            rel = py.relative_to(ROOT).as_posix()
            hits[node.value].setdefault(rel, 0)
            hits[node.value][rel] += 1
    # dict 字面量里的键（"fixed_images": []）也是 Constant，已覆盖
for t in TARGETS:
    print(f"  {t:18s} -> {hits[t]}")

print()
print("=" * 78)
print("5) 实跑实现：返回的 key 里有没有这些产物")
print("=" * 78)
from app.agents.planner import StoryPlanner          # noqa: E402
from app.agents.critics import ScriptCritic          # noqa: E402
from app.agents.prompt_refiner import PromptCompiler  # noqa: E402
from app.services.post_production import inpaint     # noqa: E402

out_planner = StoryPlanner().run({"user_input": "一只会说话的小猫想当侦探"})
print("  StoryPlanner.run() keys      :", sorted(out_planner.keys()),
      "| project_brief 写入 =", "project_brief" in out_planner)

out_critic = ScriptCritic().run({"script": {"scenes": [{"scene_id": "s1"}]}})
print("  ScriptCritic.run() keys      :", sorted(out_critic.keys()),
      "| adaptation_plan 写入 =", "adaptation_plan" in out_critic)

out_pc = PromptCompiler().run({"storyboard": [{"shot_id": "1", "description": "x"}]})
print("  PromptCompiler.run() keys    :", sorted(out_pc.keys()),
      "| control_inputs 写入 =", "control_inputs" in out_pc)

out_ip = inpaint({"images": [{"shot_id": "1"}]})
print("  inpaint(state) keys          :", sorted(out_ip.keys()),
      "| fixed_images 写入 =", "fixed_images" in out_ip,
      "值 =", out_ip.get("fixed_images"))

# 场景设计 / 质检：mock 图片服务下跑
try:
    from app.agents.stage2.scene_designer import SceneDesigner
    out_sd = SceneDesigner().run({"script": {"scenes": [{"scene_id": "s1", "location": "街道"}]}})
    print("  SceneDesigner.run() keys     :", sorted(out_sd.keys()),
          "| scene_whitebox 写入 =", "scene_whitebox" in out_sd)
except Exception as exc:  # noqa: BLE001
    print("  SceneDesigner.run() 异常（可能需外部服务）:", type(exc).__name__, exc)

try:
    from app.agents.quality_assessor import QualityAssessor
    out_qa = QualityAssessor().run({"images": [], "prompts": []})
    print("  QualityAssessor.run() keys   :", sorted(out_qa.keys()),
          "| consistent_takes 写入 =", "consistent_takes" in out_qa)
except Exception as exc:  # noqa: BLE001
    print("  QualityAssessor.run() 异常:", type(exc).__name__, exc)

print()
print("=" * 78)
print("6) 图规格（真实执行图）里有没有 control_inputs 这类节点的位置")
print("=" * 78)
from app.workflows.graph_spec import default_spec  # noqa: E402
spec = default_spec()
print("  graph_spec 节点:", sorted(n["key"] for n in spec["nodes"]))
print("  注册表节点 technical_director 在图里吗:",
      "technical_director" in {n["key"] for n in spec["nodes"]},
      "| prompt_compiler:", "prompt_compiler" in {n["key"] for n in spec["nodes"]})
print(json.dumps({"note": "graph_spec 用固定边，不读注册表端口"}, ensure_ascii=False))
