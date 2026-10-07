"""对抗验证：CHECKPOINTS 里的 previs/voice/edit 是否真的无法暂停。

不改项目文件、不写生产 DB：临时输出目录 + 临时 sqlite。
"""
from __future__ import annotations

import logging
import pathlib
import sys
import tempfile

sys.path.insert(0, r"D:\AI\ds.6")
logging.basicConfig(level=logging.ERROR, stream=sys.stdout)
logging.getLogger("langgraph").setLevel(logging.ERROR)

from app.core.config import settings  # noqa: E402

settings.hitl = True
for _k in ("mock_llm", "mock_image", "mock_quality", "mock_critics", "mock_video"):
    if hasattr(settings, _k):
        setattr(settings, _k, True)

_tmp = pathlib.Path(tempfile.mkdtemp(prefix="adv_ckpt_"))
settings.output_dir = str(_tmp / "outputs")
settings.db_path = str(_tmp / "sessions.db")
settings.db_url = f"sqlite:///{(_tmp / 'sessions.db').as_posix()}"

from app.registry import api as reg  # noqa: E402
from app.workflows import graph_spec as gs  # noqa: E402
from app.workflows import main_workflow as mw  # noqa: E402

print("=== 1. 注册表 CHECKPOINTS ===")
for c in reg.all_checkpoints():
    print(f"   key={c.key:12s} node={c.node:20s} label={c.label}")

print("\n=== 2. 图里实现了的 review 函数 ===")
print("   graph_spec.REVIEW_NODE_FNS =", list(gs.REVIEW_NODE_FNS))
print("   main_workflow.REVIEW_NODE_FNS_BY_KEY =", sorted(mw.REVIEW_NODE_FNS_BY_KEY))

spec = gs.default_spec()
graph = mw.build_graph().compile()
node_keys = set(graph.get_graph().nodes)
print("\n=== 3. 图节点 ===")
print("  ", sorted(node_keys))

print("\n=== 4. 每个确认点能否在图里真正暂停 ===")
for c in reg.all_checkpoints():
    review_in_spec = f"review_{c.key}" in {n["key"] for n in spec["nodes"]}
    review_fn = f"review_{c.key}" in mw.REVIEW_NODE_FNS_BY_KEY
    # 该 checkpoint 的 key 是否被任何 _checkpoint_on 读取
    print(f"   {c.key:12s} node={c.node:20s} review节点在spec={review_in_spec} "
          f"实现函数={review_fn} 节点在图里={c.node in node_keys}")

print("\n=== 5. 实测：只启用 previs/voice/edit 三个确认点，看是否会 __interrupt__ ===")
sid = "adv-ckpt-1"
cfg = {"configurable": {"thread_id": sid}}
seq: list[str] = []
interrupts: list = []
for chunk in graph.stream(
        {"user_input": "一只猫在屋顶看星星", "session_id": sid,
         "enabled_checkpoints": ["previs", "voice", "edit"]},
        config=cfg, stream_mode="updates"):
    for node, data in chunk.items():
        seq.append(node)
        if node == "__interrupt__":
            interrupts.append(data)

print("   实际执行节点序列:")
print("  ", seq)
print("   __interrupt__ 次数 =", len(interrupts))
for i in interrupts:
    print("     ", i)

print("\n=== 6. 对照：启用 script 一个确认点，应当会暂停 ===")
sid2 = "adv-ckpt-2"
cfg2 = {"configurable": {"thread_id": sid2}}
seq2: list[str] = []
int2: list = []
for chunk in graph.stream(
        {"user_input": "一只猫在屋顶看星星", "session_id": sid2,
         "enabled_checkpoints": ["script"]},
        config=cfg2, stream_mode="updates"):
    for node, data in chunk.items():
        seq2.append(node)
        if node == "__interrupt__":
            int2.append(data)

print("   执行节点序列:", seq2)
print("   __interrupt__ 次数 =", len(int2), [ (i[0].value.get("node") if isinstance(i, tuple) else i) for i in int2 ])
