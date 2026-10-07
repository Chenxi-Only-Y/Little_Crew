"""对抗验证：swap() 换图后，断在确认点的会话是否被静默作废并标成 done。

走真实路径：task_runner.start_run → 停在 review → apply_ops 删节点 →
validate_spec → compile_graph(spec) → MANAGER.swap → resume_run → 看结果。
"""
import sys, time, json

sys.path.insert(0, r"D:\AI\ds.6")

from app.core.config import settings
settings.mock_llm = True
settings.mock_image = True
settings.mock_quality = True
settings.mock_critics = True
settings.hitl = True

from app.workflows import graph_spec as gs
from app.workflows import proposal as P
from app.workflows.graph_manager import MANAGER
from app.workflows.main_workflow import compile_graph
from app.services import task_runner as tr

CFG = lambda sid: {"configurable": {"thread_id": sid}}


def dump(tag, sid):
    snap = MANAGER.current_graph().get_state(CFG(sid))
    print(f"--- {tag}")
    print("   next        =", snap.next)
    print("   created_at  =", snap.created_at)
    print("   checkpoint_id=", (snap.config or {}).get("configurable", {}).get("checkpoint_id"))
    print("   tasks       =", [(getattr(t, "name", None),
                                bool(getattr(t, "interrupts", None))) for t in (snap.tasks or [])])
    print("   has_checkpoint ->", tr.has_checkpoint(sid))
    print("   task_runner._run_stream would see next:", snap.next)
    return snap


sid = f"adv_swap_{int(time.time()*1000)}"
tr.start_run(sid, "对抗验证：换图作废断点", "tester")

deadline = time.time() + 90
row = {}
while time.time() < deadline:
    row = tr.get_session_store().get(sid) or {}
    if row.get("status") in ("review", "done", "error"):
        break
    time.sleep(0.1)

print("== 停在确认点 ==")
print("store status =", row.get("status"), "phase =", row.get("phase"))
print("store review =", json.dumps(row.get("result") or {}, ensure_ascii=False)[:400])
snap0 = dump("swap 之前", sid)

# ---- 用真实提案路径生成"删掉 review_critics"的规格 ----
ops = [
    {"op": "remove_edge", "from": "critics", "to": "review_critics"},
    {"op": "remove_node", "key": "review_critics"},
    {"op": "set_edge", "from": "critics", "to": "review_script",
     "why": "把文学策划审核摘掉，直接进剧本确认"},
]
print("== validate_ops ==", [p.as_dict() for p in P.validate_ops(ops)])
spec, log = P.apply_ops(gs.default_spec(), ops)
print("apply_ops log:", log)
problems = gs.validate_spec(spec)
print("validate_spec errs:", [p.as_dict() for p in gs.errors_of(problems)])
print("new node keys:", [n["key"] for n in spec["nodes"] if n["key"].startswith("review")])

new_graph = compile_graph(spec=spec)
MANAGER.swap(new_graph, reason="对抗验证：删掉 review_critics", source="adv-probe")
snap1 = dump("swap 之后", sid)

print()
print("== next 变化 ==", snap0.next, "->", snap1.next)

# ---- 续跑 ----
started, reason = tr.resume_run(sid, "tester", "approve", {})
print("resume_run ->", started, reason)

deadline = time.time() + 60
while time.time() < deadline:
    row = tr.get_session_store().get(sid) or {}
    if row.get("status") not in ("running", "review") or (row.get("status") == "review" and False):
        pass
    if row.get("status") in ("done", "error", "cancelled", "failed"):
        break
    time.sleep(0.2)

row = tr.get_session_store().get(sid) or {}
print()
print("== 续跑结束（原始 session 行）==")
print(json.dumps({k: v for k, v in row.items() if k != "thinking"},
                 ensure_ascii=False)[:1200])
res = row.get("payload")
print("payload =", res, "| empty?", not res)
snap2 = dump("续跑之后", sid)
vals = snap2.values or {}
print("final state 关键产物：",
      {k: (len(v) if isinstance(v, (list, dict)) else v)
       for k, v in vals.items()
       if k in ("review_decision", "review_point", "images", "storyboard",
                "prompts", "video", "publish", "distribution", "scripts")})
print()
print("结论标记：status=%s phase=%s payload_empty=%s" % (
    row.get("status"), row.get("phase"), not res))

# ---- 追加对抗检查：回滚能不能救回来？（伤害是否永久）----
print()
print("== 回滚到旧图 ==", "can_rollback =", MANAGER.can_rollback())
MANAGER.rollback(reason="对抗验证：回滚看断点是否复活")
dump("回滚之后", sid)

# ---- 追加对抗检查：在旧图上再续跑一次，会不会真的跑完？ ----
started2, reason2 = tr.resume_run(sid, "tester", "approve", {})
print("二次 resume_run ->", started2, reason2)
if started2:
    deadline = time.time() + 90
    while time.time() < deadline:
        row = tr.get_session_store().get(sid) or {}
        if row.get("status") in ("done", "error", "cancelled", "failed", "review"):
            break
        time.sleep(0.2)
    row = tr.get_session_store().get(sid) or {}
    print("二次续跑后 status =", row.get("status"), "phase =", row.get("phase"),
          "payload_keys =", sorted((row.get("payload") or {}).keys())[:12])
