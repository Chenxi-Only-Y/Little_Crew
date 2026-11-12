"""阶段级断点：把"跑到哪了、产出了什么"存下来，下次**重放**而不是重跑（2026-11-08）。

## 为什么不是"跳过代码块"

`run()` 是一条 900 行的流水线，块与块之间共享十几个局部变量
（`cycle` / `script_draft` / `shots` / `picked` / `irs` …）。
想"从第 4 步开始"就必须把这些变量都搬出来 —— 等于把主流程拆开重写，
**一处漏改就静默出错**（这个项目已经踩过三次同类坑）。

## 重放式续跑

```
--from render
   ↓
读 checkpoint/stages.json
   ↓
把 screenwriter / storyboard_artist **包一层** Replay：
   命中 -> 直接返回上次的产物（不调模型，秒级）
   未命中 -> 退回真实现（并如实记录 "replayed: false"）
   ↓
主流程原样跑；昂贵的 LLM 阶段被"回放"，渲染阶段本来就命中缓存
```

**好处**：主流程一行不用改，续跑的语义靠"输入完全相同"保证 ——
只有输入完全相同，重放才是安全的（不同输入会被拒绝，见 `check()`）。

## 纪律

1 **不静默**：重放命中/未命中都写进 `delivery/resume.json`
2 **不假装**：checkpoint 缺字段就**明确拒绝续跑**，不去猜
3 **可验证**：`resume.json` 里记 `sameInputs`（本次参数 vs 上次参数）
"""
from __future__ import annotations

import json
from pathlib import Path

#: 阶段顺序（与 `episode.run()` 的注释编号一致）
#: `previs`（白模预演）加在 storyboard 与 render 之间（2026-11-12）：
#: 以前预演产物不进 checkpoint，续跑时**白模要重新跑一遍**（每镜 3~5 秒）。
STAGES = ("script", "storyboard", "previs", "render", "voice", "mix", "final")

#: **可选**阶段：缺失不算"前置不全"。
#: `previs`（白模预演）是 2026-11-12 才进 checkpoint 的 ——
#: 老工作目录里没有它，若当成必需前置，续跑会被**无故拒绝**。
#: 缺它只是"白模要重新跑"，不是"不能续"。
OPTIONAL_STAGES = ("previs",)

_FILE = "checkpoint/stages.json"


def _path(workdir) -> Path:
    return Path(str(workdir)) / _FILE


def load(workdir) -> dict:
    p = _path(workdir)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                        # noqa: BLE001
        return {}


def save(workdir, stage: str, data: dict, *, meta: dict | None = None) -> dict:
    """写一个阶段的结果（**合并**写，不覆盖别的阶段）。"""
    if stage not in STAGES:
        raise ValueError(f"未知阶段 {stage!r}（可选 {STAGES}）")
    root = Path(str(workdir))
    p = _path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    cur = load(root)
    stages = dict(cur.get("stages") or {})
    stages[stage] = {"done": True, "data": dict(data or {}),
                     "meta": dict(meta or {})}
    cur["kind"] = "stage.checkpoint"
    cur["stages"] = stages
    p.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    return cur


def done_stages(workdir) -> list[str]:
    st = (load(workdir).get("stages") or {})
    return [s for s in STAGES if (st.get(s) or {}).get("done")]


def check(workdir, from_stage: str, *, inputs: dict | None = None) -> dict:
    """能不能从 `from_stage` 续跑？返回 `{ok, reason, reuse, recompute}`。

    **同一个脚本、同一集号**才允许 —— 否则重放的就是别人的产物。
    """
    if from_stage not in STAGES:
        return {"ok": False, "reason": f"未知阶段 {from_stage!r}",
                "reuse": [], "recompute": list(STAGES)}
    ck = load(workdir)
    stages = ck.get("stages") or {}
    if not stages:
        return {"ok": False, "reason": "没有 checkpoint（这次跑完才会生成）",
                "reuse": [], "recompute": list(STAGES)}
    i = STAGES.index(from_stage)
    need = [s for s in STAGES[:i] if s not in OPTIONAL_STAGES]
    miss = [s for s in need if not (stages.get(s) or {}).get("done")]
    if miss:
        return {"ok": False,
                "reason": f"checkpoint 缺前置阶段：{', '.join(miss)}",
                "reuse": [s for s in need if s not in miss],
                "recompute": list(STAGES)}
    # 输入一致性：集号 + 脚本来源（不同输入重放=错）
    old = (stages.get(need[0]) or {}).get("meta") or {} if need else {}
    if inputs:
        for k, v in inputs.items():
            if k in old and str(old[k]) != str(v):
                return {"ok": False,
                        "reason": f"输入不一致（{k}: 上次 {old[k]!r} vs 本次 {v!r}）—— "
                                  f"重放会把别人的产物当成这次的",
                        "reuse": [], "recompute": list(STAGES)}
    # 复用清单 = 该阶段之前**已完成**的全部阶段（含可选阶段 previs）。
    # `need` 只用来判"缺不缺必需前置"；拿它当复用清单会把 previs 漏报。
    reuse = [s for s in STAGES[:i] if (stages.get(s) or {}).get("done")]
    return {"ok": True, "reason": "可以续跑", "reuse": reuse,
            "recompute": list(STAGES[i:]), "stages": stages}


def payload_of(workdir, stage: str, key: str, default=None):
    """取某阶段某个字段（重放用）。"""
    st = (load(workdir).get("stages") or {}).get(stage) or {}
    return ((st.get("data") or {}).get(key, default))
