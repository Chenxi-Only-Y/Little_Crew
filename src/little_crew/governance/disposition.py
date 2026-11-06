"""处置闭环：把"哪里坏了"变成"**具体怎么修**"（2026-11-06，P3）。

## 为什么需要它

`interface_defects` 能说清"某场超时 6 秒"，但定向返工目前只会**整场重切** ——
模型不知道该怎么改，于是可能把 6 秒挪到别的场，或者干脆少给镜头。

**处置 = 把缺陷翻译成可执行的改法**：

| 缺陷 | 处置 |
|---|---|
| 场总时长超上限 | 合并相邻同机位/同动作镜头 · 压缩空镜 · 总时长必须 ≤T_s |
| 单镜超上限 | 把该镜切成两镜（**不许丢内容**） |
| 镜头数超上限 | 合并相邻镜头 |
| 镜头数不足下限 | 补足到下限（拆一个长镜 / 补一个过渡镜） |
| 串镜（多镜同一张） | 不靠重切解决 —— 属于渲染/缓存问题，只报不改 |

## 设计纪律

- **纯函数**：不调模型、可穷举、可单测
- **每条处置都带数字**（超了多少、上限多少）—— 否则模型还是在猜
- **不许提"删镜头"**：删会丢内容；合并/拆分才是改法
"""
from __future__ import annotations

#: 处置动作（固定词表，便于下游识别与统计）
MERGE = "merge-adjacent"
SPLIT = "split-shot"
TRIM = "trim-total"
FILL = "fill-shots"
NOOP = "noop"

#: 缺陷 kind -> 处置动作
_KIND_ACTIONS: dict[str, tuple[str, ...]] = {
    "over-scene-seconds": (MERGE, TRIM),
    "over-single-max": (SPLIT,),
    "over-max-shots": (MERGE,),
    "under-min-shots": (FILL,),
    "duration.over": (SPLIT,),
    "duration.long": (SPLIT,),
    "duplicate-artifact": (NOOP,),          # 渲染/缓存问题，重切无用
}


def actions_for(kind: str) -> tuple[str, ...]:
    """缺陷类型 -> 处置动作。未知类型返回空（保守：不乱给指令）。"""
    return _KIND_ACTIONS.get(str(kind or ""), ())


def instruction_for(finding: dict, quota: dict | None = None) -> str:
    """一条缺陷 -> 一句**给模型看的中文改法**。

    数字必须来自 finding/quota，不许编 —— 模型需要知道"超了多少"。
    """
    kind = str(finding.get("kind") or "")
    q = quota or {}
    detail = finding.get("detail") or {}
    sid = str(finding.get("sceneId") or (finding.get("unit") or {}).get("sceneId") or "")

    if kind == "over-scene-seconds":
        got = finding.get("seconds") or detail.get("seconds")
        cap = q.get("sceneSeconds")
        return (f"{sid} 该场总时长 {got}s 超过上限 {cap}s："
                f"合并相邻同机位/同动作的镜头，或压缩空镜；"
                f"合并后总时长必须 ≤{cap}s，且**镜头数不得少于 {q.get('shotMin', 3)}**")
    if kind in ("over-single-max", "duration.over", "duration.long"):
        got = finding.get("maxShot") or detail.get("seconds")
        cap = q.get("singleMax")
        return (f"{sid} 有镜头 {got}s 超过单镜上限 {cap}s：把它**切成两镜**"
                f"（不许丢内容），切完每镜都要 ≤{cap}s")
    if kind == "over-max-shots":
        got = finding.get("shots")
        cap = q.get("shotMax")
        return (f"{sid} 给了 {got} 个镜头，超过上限 {cap}："
                f"把相邻同机位/同动作的镜头合并，总数压到 ≤{cap}")
    if kind == "under-min-shots":
        got = finding.get("shots")
        floor = q.get("shotMin", 3)
        return (f"{sid} 只有 {got} 个镜头，少于下限 {floor}："
                f"拆开一个信息量最大的镜头，或补一个过渡镜，到 ≥{floor}")
    return ""


def plan(findings, quotas=None) -> dict:
    """把审计/缺陷 -> `{sceneId: {"actions": [...], "instructions": [...]}}`。

    `quotas`: `plan_alloc.quotas_for()` 的结果（提供上限数字）
    """
    by_scene: dict[str, dict] = {}
    for f in findings or ():
        if not isinstance(f, dict):
            continue
        sid = str(f.get("sceneId") or (f.get("unit") or {}).get("sceneId") or "")
        if not sid:
            continue
        acts = actions_for(str(f.get("kind") or ""))
        if not acts or acts == (NOOP,):
            continue
        # ⚠️ 用 .get：配额里**可能没有 sceneId**（例如单场配额直接传进来）。
        # 之前用 x["sceneId"] 会 KeyError，而调用方是 try/except ——
        # 结果就是"处置静默缺席"，最难发现的那种失败。
        qmap = {x["sceneId"]: x
                for x in (quotas or {}).get("scenes") or ()
                if isinstance(x, dict) and x.get("sceneId")}
        text = instruction_for(f, qmap.get(sid))
        row = by_scene.setdefault(sid, {"sceneId": sid, "actions": [],
                                        "instructions": []})
        for a in acts:
            if a not in row["actions"]:
                row["actions"].append(a)
        if text:
            row["instructions"].append(text)
    return {
        "kind": "scene.disposition",
        "scenes": [by_scene[k] for k in sorted(by_scene)],
        "sceneCount": len(by_scene),
    }


def prompt_lines(disposition, scene_id: str) -> list[str]:
    """给重切提示词用的几行（没有处置就返回空）。"""
    for row in (disposition or {}).get("scenes") or ():
        if row.get("sceneId") != scene_id:
            continue
        return ["【本场必须这样改】"] + [f"  · {x}" for x in row["instructions"]]
    return []
