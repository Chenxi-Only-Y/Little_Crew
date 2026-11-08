"""视觉闸驱动的**定向重渲**：只重画崩掉的那几镜（2026-11-08）。

## 为什么这是最关键的一步

视觉闸已经能判「这一镜脸崩了」（EV4 实测 22 镜里 2 镜），
但**判出来之后没人动** —— finding 只落盘，图还是那张崩的。

这一步把「判」和「改」接上：`faces=broken` / `blank` → **重渲这一镜** → 再看一遍。

## 机制：不新造轮子，只改 `attempt`

编译器按 `provenance.seedDerivation` 派生种子：

```
projectSeed | episodeIdx | sceneIdx | shotIdx | attempt  ->  derive_seed()
```

而 `attempt` **进内容哈希**（`seedDerivation` 保留在 provenance 里）——
所以把 `attempt + 1` 就等于「同一个镜头、换一次尝试」：
**缓存必然 miss，图会被真正重画**；旧图仍在缓存里（可追溯）。

## 三条纪律

1 **只重画 error 级**（`faces=broken` / `blank`）—— 低一致度是 warn，不自动烧 GPU
2 **有上限**：默认最多 4 镜、最多 2 次尝试（与 `maxAutomaticAttempts` 一致）
3 **重画后必须复验**：不复验等于"重画了但不知道好没好"（这个项目踩过同类坑）
"""
from __future__ import annotations

import copy

#: 只重画这些 kind（与 `review/vision_review.py` 的 severity=error 对齐）
RERENDER_KINDS = ("vision.faces-broken", "vision.blank")


def plan(findings, *, max_shots: int = 4) -> list[dict]:
    """从视觉闸的 findings 里挑出**要重画**的镜（去重、保序、限量）。"""
    out: list[dict] = []
    seen: set[str] = set()
    for f in findings or ():
        if str(f.get("kind")) not in RERENDER_KINDS:
            continue
        sid = str(f.get("shotId") or "")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        out.append({"shotId": sid, "kind": f.get("kind"), "reason": f.get("reason", "")})
        if max_shots and len(out) >= max_shots:
            break
    return out


def bump_attempt(ir) : 
    """复制一份 IR 并把 `attempt` 加一（**不改原对象**）。

    返回 `(新IR, 新attempt)`；IR 缺 `seedDerivation` 时**补上但不猜种子** ——
    用 graphId 稳定哈希兜底的行为保持不变，只保证 attempt 变了。
    """
    new = copy.deepcopy(dict(ir or {}))
    prov = dict(new.get("provenance") or {})
    sd = dict(prov.get("seedDerivation") or {})
    old = int(sd.get("attempt") or 0)
    sd["attempt"] = old + 1
    prov["seedDerivation"] = sd
    new["provenance"] = prov
    return new, sd["attempt"]


def run(targets, irs_by_shot, render_fn, *, max_attempts: int = 2) -> dict:
    """逐个重画。

    `irs_by_shot`: `{shotId: ir}`；`render_fn(irs) -> {"batch": {...}}`（与 episode 同签名）
    返回 `{ok, rerendered: [{shotId, attempt, artifacts}], failed: [...], skipped: [...]}`
    """
    done, failed, skipped = [], [], []
    for t in targets or ():
        sid = str(t.get("shotId") or "")
        ir = (irs_by_shot or {}).get(sid)
        if not ir:
            skipped.append({"shotId": sid, "reason": "没有这镜的 IR（可能已被缓存跳过）"})
            continue
        attempts = 0
        ok_any = False
        while attempts < max(1, int(max_attempts)):
            attempts += 1
            ir2, att = bump_attempt(ir)
            try:
                res = render_fn([ir2]) or {}
            except Exception as e:                                # noqa: BLE001
                failed.append({"shotId": sid, "attempt": att,
                               "error": f"{type(e).__name__}: {str(e)[:80]}"})
                break
            arts = _artifacts_of(res)
            if arts:
                done.append({"shotId": sid, "attempt": att, "artifacts": arts,
                             "kind": t.get("kind", ""), "reason": t.get("reason", "")})
                ok_any = True
                break
            failed.append({"shotId": sid, "attempt": att, "error": "渲染没有产物"})
        if not ok_any and not any(x["shotId"] == sid for x in failed):
            failed.append({"shotId": sid, "error": "重画失败"})
    return {"kind": "vision.rework", "rerendered": done, "failed": failed,
            "skipped": skipped, "count": len(done)}


def _artifacts_of(res) -> list[str]:
    """从渲染结果里捞产物路径（兼容 batch 的各种形态）。"""
    batch = (res or {}).get("batch") if isinstance(res, dict) else None
    out: list[str] = []

    def _take(x):
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            p = x.get("path") or x.get("artifact")
            if p:
                out.append(str(p))
        elif isinstance(x, (list, tuple)):
            for y in x:
                _take(y)

    _take(batch)
    return out
