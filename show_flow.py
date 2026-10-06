"""导出当前工作流的完整结构（用于回答"流程是什么样的"）。"""

import json
import sys
import io
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

with urllib.request.urlopen("http://127.0.0.1:8000/api/registry", timeout=25) as r:
    reg = json.loads(r.read().decode("utf-8", "replace"))["registry"]

nodes = {n["key"]: n for n in reg["nodes"]}
roles = {x["key"]: x for x in reg["roles"]}
depts = {d["key"]: d for d in reg["departments"]}
arts = {a["key"]: a for a in reg["artifacts"]}
cps = {c["key"]: c for c in reg["checkpoints"]}
order = reg["execution_order"]

print(f"  部门 {len(depts)} · 职业 {len(roles)} · 节点 {len(nodes)} · "
      f"产物 {len(arts)} · 检查点 {len(cps)}")
print()
print("=== 执行顺序（工作流的骨架）===")
for i, key in enumerate(order, 1):
    n = nodes.get(key)
    if not n:
        print(f"  {i:2}. {key}  ⚠ 找不到节点定义")
        continue
    dept = depts.get(n.get("dept", ""), {}).get("label", n.get("dept", ""))
    caps = "/".join(n.get("capability") or []) or "—"
    role_label = ""
    for rk, rv in roles.items():
        if rv.get("node") == key:
            role_label = rv["label"]
            break
    produces = ",".join(n.get("produces") or [])
    print(f"  {i:2}. [{dept}] {n['label']}（{role_label}）")
    print(f"       能力: {caps}")
    print(f"       产出: {produces or '—'}")

print()
print("=== 人工确认点 ===")
for c in reg["checkpoints"]:
    print(f"  · {c['label']}  （在 {c.get('node') or c.get('after')} 之后）")

print()
print("=== 部门 ===")
for d in reg["departments"]:
    members = [r["label"] for r in reg["roles"] if r.get("dept") == d["key"]]
    print(f"  {d.get('order','?'):>2}. {d['label']:<4} {d.get('desc','')[:34]:<36} {'、'.join(members)}")
