"""无限画布的**后端**：把一次跑批转成"流水线依赖图"（2026-11-16）。

## 为什么是后端

前端（无限画布）只负责画。它需要的全部数据是一次运行后的：
**节点（职位/通道）、边（产物契约）、状态（完成/跳过/缺失）、证据（文件）、费用**。
这些必须在后端由**名册契约 + 交付证据**推导，不能由前端猜。

## 图的结构

```jsonc
{
  "kind": "pipeline.graph",
  "episodeId": "E001",
  "crew": {"preset": "standard", "hash": "…", "active": [...], "skipped": [...]},
  "nodes": [
    {"id": "scene_designer", "type": "position", "title": "场景设计师",
     "department": "art", "stage": "asset", "execution": "text_llm",
     "active": true, "status": "done", "produces": ["scene.library"],
     "evidence": ["delivery/scene.library.json"]},
    {"id": "code:shot.image", "type": "channel", "title": "渲染通道", ...}
  ],
  "edges": [{"from": "scene_designer", "to": "storyboard_artist",
             "artifact": "scene.library"}],
  "sources": [{"id": "source:source.novel", "artifact": "source.novel"}],
  "costs": {"imageCny": 0.2, "lipsyncCny": 0.75}
}
```

## 诚实边界

- `status` 只根据**证据文件是否存在**推断：`done` / `pending`（启用但无证据）/
  `skipped`（编制未启用）/ `blocked` 由门禁另行记录 —— 不把"没证据"当"完成"。
- 费用只汇总能对账的（`image_calls.json` / `lipsync.json`），不估算。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

#: 产物 -> 交付证据文件名（相对 workdir/delivery/）
EVIDENCE_OF: dict[str, tuple[str, ...]] = {
    "script.final": ("script.rounds.json",),
    "script.draft": ("script.rounds.json",),
    "art.bible": ("art.bible.json",),
    "scene.library": ("scene.library.json",),
    "character.sheet": ("anchors_used.json",),
    "character.anchor_set": ("anchors_used.json",),

    "shot.list": ("shot_selection.json",),
    "shot.voice": ("voice_used.json",),
    "shot.music": ("mix.json",),
    "shot.video": ("video_plan.json",),
    "shot.image": ("image_calls.json",),
    "qc.report": ("vision_review.json",),
    "continuity.report": ("continuity_audit.json",),
    "review.verdict": ("shotlist_review.json",),
    "post.plan": ("post.plan.json",),
    "compliance.report": ("compliance.json",),
    "episode.master": ("episode.master.mp4",),
    "delivery.evidence": ("delivery.evidence.json",),
}

#: 没有职位生产者、但确实是流水线产物的"通道节点"
CHANNEL_NODES: dict[str, dict[str, str]] = {
    "shot.image": {"title": "渲染通道（出图）", "kind": "image"},
    "episode.master": {"title": "合成通道（成片）", "kind": "assembly"},
    "delivery.evidence": {"title": "交付证据链", "kind": "evidence"},
}

#: 外部输入（剧本来源等）
SOURCE_NODES: dict[str, str] = {"source.novel": "源素材/小说"}


def _evidence_exists(workdir: str | Path | None, art: str) -> list[str]:
    if not workdir:
        return []
    d = Path(str(workdir)) / "delivery"
    return [f"delivery/{n}" for n in EVIDENCE_OF.get(str(art), ())
            if (d / n).is_file()]


def _read_costs(workdir: str | Path | None) -> dict[str, float]:
    out: dict[str, float] = {}
    if not workdir:
        return out
    d = Path(str(workdir)) / "delivery"
    try:
        p = d / "image_calls.json"
        if p.is_file():
            j = json.loads(p.read_text(encoding="utf-8"))
            cloud = (j.get("cloud") or {}) if isinstance(j, dict) else {}
            out["imageCny"] = round(float(cloud.get("estCostCny") or 0.0), 2)
    except Exception:                                         # noqa: BLE001
        pass
    try:
        p = d / "lipsync.json"
        if p.is_file():
            j = json.loads(p.read_text(encoding="utf-8"))
            out["lipsyncCny"] = round(float((j or {}).get("costCny") or 0.0), 2)
    except Exception:                                         # noqa: BLE001
        pass
    return out


def build_graph(*, roster: Sequence[Any], crew: Mapping[str, Any] | None = None,
                workdir: str | Path | None = None,
                episode_id: str = "",
                stage_evidence: Mapping[str, Sequence[str]] | None = None,
                ) -> dict[str, Any]:
    """按名册契约 + 编制 + 交付证据，构建画布后端图。

    `stage_evidence`：**运行期才知道的证据**（如"导演这次真的产出了 episode.plan"），
    由 `run()` 传入 `{agent_id: ["delivery/run.report.json"]}` —— 只看文件系统
    会漏掉"不单独落文件的阶段"，也会把"报告文件存在"误当成"该阶段跑过"。
    """
    stage_evidence = dict(stage_evidence or {})
    crew = dict(crew or {})
    active = set(crew.get("active") or [])
    active_known = bool(active)
    by_id = {str(a.agent_id): a for a in roster}
    producers: dict[str, list[str]] = {}
    for a in roster:
        for art in (a.produces or ()):
            producers.setdefault(str(art), []).append(str(a.agent_id))

    nodes: list[dict[str, Any]] = []
    for a in roster:
        aid = str(a.agent_id)
        ev: list[str] = []
        status = "skipped"
        if not active_known or aid in active:
            for art in (a.produces or ()):
                ev.extend(_evidence_exists(workdir, str(art)))
            ev.extend(str(x) for x in stage_evidence.get(aid, ()) or ())
            ev = sorted(set(ev))
            status = "done" if ev else "pending"
        nodes.append({
            "id": aid, "type": "position",
            "title": str(a.crew_title), "agent": aid,
            "department": str(a.department.value),
            "stage": str(a.stage.value),
            "execution": str(a.execution.value),
            "active": (not active_known) or (aid in active),
            "status": status,
            "capabilities": list(a.capabilities or ()),
            "produces": list(a.produces or ()),
            "requires": list(a.requires or ()),
            "evidence": ev,
        })

    # 通道节点（产物无职位生产者）
    channel_ids: set[str] = set()
    for art, meta in CHANNEL_NODES.items():
        if producers.get(art):
            continue
        ev = _evidence_exists(workdir, art)
        nodes.append({
            "id": f"code:{art}", "type": "channel",
            "title": meta["title"], "agent": "",
            "department": "channel", "stage": "render",
            "execution": meta["kind"], "active": True,
            "status": "done" if ev else "pending",
            "capabilities": [], "produces": [art], "requires": [],
            "evidence": ev,
        })
        channel_ids.add(art)

    edges: list[dict[str, str]] = []
    seen_edges: set[tuple[str, str, str]] = set()
    for a in roster:
        aid = str(a.agent_id)
        if active_known and aid not in active:
            continue
        for art in (a.requires or ()):
            art = str(art)
            srcs = producers.get(art) or []
            if srcs:
                for src in srcs:
                    if active_known and src not in active:
                        continue
                    key = (src, aid, art)
                    if key not in seen_edges:
                        seen_edges.add(key)
                        edges.append({"from": src, "to": aid, "artifact": art})
            elif art in channel_ids:
                key = (f"code:{art}", aid, art)
                if key not in seen_edges:
                    seen_edges.add(key)
                    edges.append({"from": f"code:{art}", "to": aid,
                                  "artifact": art})

    sources = [{"id": f"source:{art}", "title": title, "artifact": art}
               for art, title in SOURCE_NODES.items()
               if not producers.get(art)]

    return {
        "kind": "pipeline.graph",
        "episodeId": str(episode_id or ""),
        "crew": {
            "preset": crew.get("preset"),
            "hash": crew.get("hash"),
            "commercial": bool(crew.get("commercial")),
            "active": sorted(active),
            "skipped": sorted(crew.get("skipped") or ()),
            "autoAdded": list(crew.get("autoAdded") or ()),
        },
        "nodes": nodes,
        "edges": edges,
        "sources": sources,
        "costs": _read_costs(workdir),
        "summary": {
            "positions": sum(1 for n in nodes if n["type"] == "position"),
            "active": sum(1 for n in nodes
                          if n["type"] == "position" and n["active"]),
            "done": sum(1 for n in nodes if n["status"] == "done"),
            "pending": sum(1 for n in nodes if n["status"] == "pending"),
            "skipped": sum(1 for n in nodes if n["status"] == "skipped"),
            "edges": len(edges),
        },
    }


def write_graph(workdir: str | Path, graph: Mapping[str, Any]) -> Path:
    """把图写到 `delivery/pipeline.graph.json`（画布前端直接读）。"""
    d = Path(str(workdir)) / "delivery"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "pipeline.graph.json"
    p.write_text(json.dumps(dict(graph), ensure_ascii=False, indent=2,
                            default=str), encoding="utf-8")
    return p
