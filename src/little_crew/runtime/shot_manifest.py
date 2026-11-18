"""每镜资产记录（shot manifest，2026-11-18，0.9.0）。

把"整集一个 batch"改成"每镜一条记录"：
```
assets/shots/<shotId>/shot.json   # prompt/promptHash/model/anchor/out/status/version/history
assets/shots/<shotId>/*.png       # 产物
```
- 渲染 = 写/更新一条记录
- 重渲 = 更新一条记录（版本+1，旧版进 history）
- 整集 = 读取全部记录组成的视图（complete/missing）
- B2 补拍 = 改边界镜记录里的 prompt（加 hint）→ 只重渲这一条
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

__all__ = ["RECORD", "record_path", "load", "save", "upsert", "bump_hint",
           "view", "complete", "to_batch"]


RECORD = "shot.json"


def record_path(workdir: str | Path, shot_id: str) -> Path:
    return Path(str(workdir)) / "assets" / "shots" / str(shot_id) / RECORD


def load(workdir: str | Path, shot_id: str) -> dict[str, Any] | None:
    p = record_path(workdir, shot_id)
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except Exception:                                        # noqa: BLE001
        return None


def _hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:12]


def save(workdir: str | Path, rec: Mapping[str, Any]) -> Path:
    p = record_path(workdir, str(rec.get("shotId") or ""))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(dict(rec), ensure_ascii=False, indent=2),
                 encoding="utf-8")
    return p


def upsert(workdir: str | Path, *, shot_id: str, prompt: str = "",
           model: str = "", anchor: str = "", out: str = "",
           status: str = "ok", cost_cny: float = 0.0,
           meta: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """写入/更新一条镜头记录；prompt 变化 -> version+1 + 旧版进 history。"""
    prev = load(workdir, shot_id) or {}
    ph = _hash(prompt) if prompt else str(prev.get("promptHash") or "")
    changed = bool(prompt) and ph != str(prev.get("promptHash") or "")
    version = int(prev.get("version") or 0) + (1 if (not prev or changed) else 0)
    rec = dict(prev)
    rec.update({"shotId": str(shot_id), "version": max(1, version),
                "prompt": prompt or str(prev.get("prompt") or ""),
                "promptHash": ph, "model": model or str(prev.get("model") or ""),
                "anchor": anchor or str(prev.get("anchor") or ""),
                "out": out or str(prev.get("out") or ""),
                "status": status,
                "costCny": round(float(prev.get("costCny") or 0) + float(cost_cny), 4)})
    if meta:
        rec["meta"] = {**dict(prev.get("meta") or {}), **dict(meta)}
    if changed and prev:
        rec.setdefault("history", []).append({
            "version": prev.get("version"), "promptHash": prev.get("promptHash"),
            "out": prev.get("out"), "status": prev.get("status")})
        rec["history"] = rec["history"][-10:]
    save(workdir, rec)
    return rec


def bump_hint(workdir: str | Path, shot_id: str, hint: str) -> dict[str, Any]:
    """B2：给边界镜提示词追加 continuity hint；只改这一条记录。"""
    rec = load(workdir, shot_id)
    if not rec:
        raise KeyError(f"没有镜头记录：{shot_id}")
    old = str(rec.get("prompt") or "")
    new_prompt = (old + "；" + str(hint or "").strip()).strip("；")
    return upsert(workdir, shot_id=shot_id, prompt=new_prompt,
                  model=str(rec.get("model") or ""),
                  anchor=str(rec.get("anchor") or ""),
                  out=str(rec.get("out") or ""),
                  status="pending",
                  meta={"hint": str(hint or "")})


def view(workdir: str | Path) -> dict[str, Any]:
    """整集视图：读取全部镜头记录（下游/合成用，不再依赖内存 batch）。"""
    base = Path(str(workdir)) / "assets" / "shots"
    shots: dict[str, Any] = {}
    if base.is_dir():
        for sub in sorted(base.iterdir()):
            if not sub.is_dir():
                continue
            rec = load(workdir, sub.name)
            if rec:
                shots[str(sub.name)] = rec
    missing = [sid for sid, r in shots.items()
               if str(r.get("status")) != "ok" or not r.get("out")]
    return {"kind": "shot.manifest", "shots": shots,
            "complete": not missing, "missing": missing}


def complete(workdir: str | Path) -> bool:
    return bool(view(workdir)["complete"])



def to_batch(workdir: str | Path, *, episode_id: str = "") -> Any:
    """manifest -> CachedBatch 适配器：下游（门禁/合成/审片）无需改代码。"""
    from little_crew.runtime.cached_render import CachedBatch, CacheOutcome

    v = view(workdir)
    outcomes = []
    prefix = f"G-{episode_id}-" if episode_id else ""
    for sid, rec in sorted(v["shots"].items()):
        out = Path(str(rec.get("out") or ""))
        if str(rec.get("status")) == "ok" and out.is_file():
            outcomes.append(CacheOutcome(
                graph_id=f"{prefix}{sid}", ok=True, from_cache=True,
                artifacts={f"{sid}.png": out.read_bytes()}, seconds=0.0))
        else:
            outcomes.append(CacheOutcome(
                graph_id=f"{prefix}{sid}", ok=False,
                failure="artifact-missing",
                reason=f"manifest 记录不可用：status={rec.get('status')}"))
    return CachedBatch(outcomes=tuple(outcomes))
