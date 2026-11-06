"""场景预算协调器：把「这场给几镜」升级为**场级联合配额**（2026-11-06）。

## 为什么要有它

之前的预算只有"总镜头数"一个维度，于是出现两种坏结果：

- 短剧本被切成 20 镜（每场均匀 4 镜，不管内容）
- 长剧本某场被塞 10 镜 82 秒（单场严重失衡）

**根因：镜头数与时长是两个独立的量，只约束一个必然失衡。**

本模块把两者**联合**起来：

```
每场总时长 T_s  ──┐
单镜时长范围      ├─→ 反推 N_min/N_max ──→ 联合可行的 (镜数, 单镜时长)
导演节奏权重      ──┘
```

## 为什么是纯函数

配额是**算术**，不是创作判断。做成纯函数意味着：
**可穷举边界、可复现、不花钱、不需要模型** ——
而"逻辑一错整批数据作废"是最贵的错（按字数压预算那次就是这样）。

## 不做什么

- **不调模型**（导演的创作判断从参数进来）
- **不做镜头级 merge/split**（那是渲染后的事，见 `review/scene_pacing`）
- **不做多轮重分配**（震荡风险，留到 P4，且只允许一轮）
"""
from __future__ import annotations

import math

#: 单镜时长默认范围（秒）
DEFAULT_SINGLE_MIN = 2.0
DEFAULT_SINGLE_MAX = 10.0
#: 单镜绝对上限（平台硬约束）
DEFAULT_LONGEST = 10.0
#: 每场总时长默认上限（秒）
DEFAULT_SCENE_SECONDS = 30.0
#: 每场镜数的**常识上限**。
#:
#: 光看 `T_s / sMin` 会推出荒谬结果：30 秒能切 15 镜、80 秒能切 40 镜 ——
#: 那是"两秒一镜"的碎切，正是实测里被判节奏差的那种。
#: 一场景很少需要超过 8 个镜头。
MAX_SHOTS_PER_SCENE = 8


def _eff_max(single_max: float, longest_cap: float) -> float:
    return max(1.0, min(float(single_max), float(longest_cap)))


def preferred_avg(single_min: float, single_max: float, *,
                  avg_target: str = "", allow_long: bool = False) -> float:
    """导演的"目标平均镜头时长"落到具体秒数。

    - `偏短`：贴着下限（镜头更碎、节奏更赶）
    - `偏长`：靠上限，但**不允许长镜头时留出余量**
    - 其它：取中值
    """
    lo, hi = float(single_min), float(single_max)
    t = str(avg_target or "").strip()
    if t in ("偏短", "短", "short"):
        return round(lo * 1.4, 2)
    if t in ("偏长", "长", "long"):
        return round(hi * (0.9 if allow_long else 0.75), 2)
    return round((lo + hi) / 2.0, 2)


def scene_quota(*, scene_seconds: float = DEFAULT_SCENE_SECONDS,
                single_min: float = DEFAULT_SINGLE_MIN,
                single_max: float = DEFAULT_SINGLE_MAX,
                longest_cap: float = DEFAULT_LONGEST,
                avg_target: str = "", allow_long: bool = False,
                priority: int = 3, role: str = "") -> dict:
    """**单场**联合配额：由总时长反推可行的镜数区间与单镜范围。

    关键规则（与旧实现的差别）：

    1 `N_max` 由 `T_s / sMin` 决定 —— **时长决定最多几镜**
    2 `N_min` 由 `T_s / 单镜有效上限` 决定 —— **别让一场只挂一个空镜**
    3 两者冲突（极小 T_s）时，以**至少 1 镜**为准，并把该场标 `tight`
    4 导演 `avg_target` 只影响**目标均值**，不直接改区间
    """
    ts = max(1.0, float(scene_seconds))
    smin = max(0.5, float(single_min))
    eff = _eff_max(single_max, longest_cap)

    # ⚠️ **一场至少 3 镜**（2026-11-06，第三次被数据说服）
    #
    # 盲判反复指向同一件事：**"一场只有 2 镜"是最强的低分信号**
    #   48 条样本：低分 67% 的场是 2 镜，高分只有 37%
    #   58 条样本：A 的 2 镜场占比 2%、节奏 4.62；B 是 14%、节奏 3.79
    # 而之前 `nMin = ceil(T_s / (上限×1.5))` 在短场正好算出 2 ——
    # 等于把每场都允许压到 2 镜，B 就压到了 2。
    #
    # 例外：场太短（<15s）或 role=过场/空镜，允许 2 镜。
    _floor = 2 if (ts < 15.0 or str(role) == "过场") else 3
    n_max = max(1, min(MAX_SHOTS_PER_SCENE, int(math.floor(ts / smin))))
    # 至少几镜才填得满：用"有效上限"估，再留 1.5 倍余量，避免强迫长镜头
    n_min = max(_floor, min(MAX_SHOTS_PER_SCENE,
                            int(math.ceil(ts / (eff * 1.5)))))
    tight = False
    if n_min > n_max:
        n_min = n_max
        tight = True
    if n_min * smin > ts + 1e-6:                 # 不可行 -> 放宽到 1 镜
        n_min = 1
        tight = True

    avg = preferred_avg(smin, eff, avg_target=avg_target, allow_long=allow_long)
    ideal = max(1, int(round(ts / avg))) if avg > 0 else n_min
    n_ideal = min(max(ideal, n_min), n_max)

    return {
        "sceneSeconds": round(ts, 2),
        "shotMin": n_min,
        "shotMax": n_max,
        "shotIdeal": n_ideal,
        "singleMin": round(smin, 2),
        "singleMax": round(eff, 2),
        "longestCap": round(float(longest_cap), 2),
        "avgShotTarget": round(avg, 2),
        "priority": int(priority),
        "tight": tight,
        "feasibleSeconds": round(n_min * smin, 2),
    }


def allocate(scenes, *, global_shot_cap: int | None = None) -> dict:
    """**多场**联合分配：按优先级在总镜头数封顶时决定谁被削。

    `scenes`: `[{"sceneId","sceneSeconds","singleMin","singleMax","longestCap",
                 "avgTarget","allowLong","priority"}, ...]`

    规则：
    1 每场先按 `scene_quota` 拿自己的 `[min,max]`（时长不可让 —— 那是物理约束）
    2 若 Σ`shotIdeal` > `global_shot_cap`：**从优先级最低**的场开始，
       把它的 `shotIdeal` 往 `shotMin` 压，压到底再动次低的。
       **同优先级时按「可削量」从大到小**（一次削够，少动几场），
       并用 `sceneId` 兜底保证确定性
    3 永远不把任何一场压到 `shotMin` 以下
    4 若**全部压到 `shotMin` 仍超帽** -> **接受软超帽**，
       标 `softCapExceeded=True` 与 `overBy`，**不再继续削**
       （宁可超帽，也不产生空场 —— 空场是更严重的错）
    """
    quotas = []
    for s in scenes or ():
        q = scene_quota(
            scene_seconds=s.get("sceneSeconds", DEFAULT_SCENE_SECONDS),
            single_min=s.get("singleMin", DEFAULT_SINGLE_MIN),
            single_max=s.get("singleMax", DEFAULT_SINGLE_MAX),
            longest_cap=s.get("longestCap", DEFAULT_LONGEST),
            avg_target=s.get("avgTarget", ""),
            allow_long=bool(s.get("allowLong", False)),
            priority=int(s.get("priority", 3)),
            role=str(s.get("role") or ""),
        )
        q["sceneId"] = str(s.get("sceneId") or "")
        q["role"] = str(s.get("role") or "")
        quotas.append(q)

    total_ideal = sum(q["shotIdeal"] for q in quotas)
    trimmed = False
    trim_order: list[str] = []
    soft_cap_exceeded = False
    over_by = 0
    if global_shot_cap is not None and total_ideal > int(global_shot_cap):
        # 同优先级：可削量大的先削（少动几场），sceneId 兜底保确定性
        order = sorted(
            quotas,
            key=lambda q: (q["priority"],
                           -(q["shotIdeal"] - q["shotMin"]),
                           q["sceneId"]),
        )
        need = total_ideal - int(global_shot_cap)
        for q in order:
            if need <= 0:
                break
            take = min(need, q["shotIdeal"] - q["shotMin"])
            if take > 0:
                q["shotIdeal"] -= take
                need -= take
                trimmed = True
                trim_order.append(q["sceneId"])
        if need > 0:
            # 全压到 N_min 仍超帽 -> 接受软超帽，不再削
            soft_cap_exceeded = True
            over_by = need

    return {
        "kind": "scene.quota",
        "scenes": quotas,
        "totalIdeal": sum(q["shotIdeal"] for q in quotas),
        "totalMin": sum(q["shotMin"] for q in quotas),
        "globalShotCap": global_shot_cap,
        "trimmed": trimmed,
        "trimOrder": trim_order,
        "softCapExceeded": soft_cap_exceeded,
        "overBy": over_by,
    }


def prompt_lines(quota: dict) -> list[str]:
    """把单场配额写成**分镜师看得懂的两行**。"""
    return [
        f"  {quota.get('sceneId')}：总时长 ≤{quota['sceneSeconds']:.0f}s · "
        f"镜数 {quota['shotMin']}~{quota['shotMax']}（目标 {quota['shotIdeal']}）· "
        f"单镜 {quota['singleMin']:.0f}~{quota['singleMax']:.0f}s · "
        f"平均约 {quota['avgShotTarget']:.0f}s",
    ]


#: 压力开关：把每场 T_s 乘一个系数，**故意制造越界**，用来验证处置链路。
#:
#: 为什么需要它：P2 之后配额真的被遵守了，正常跑根本不会越界，
#: 于是"处置"这条链路**在真跑里永远看不到**。
#: 用它把上限压到 0.6 倍，就能看到"审计报越界 → 生成处置"。
#: 用法：`set LITTLE_CREW_QUOTA_SCALE=0.6`
QUOTA_SCALE_ENV = "LITTLE_CREW_QUOTA_SCALE"


def _quota_scale() -> float:
    import os

    try:
        v = float(os.environ.get(QUOTA_SCALE_ENV) or 1.0)
    except (TypeError, ValueError):
        return 1.0
    return v if 0.1 <= v <= 2.0 else 1.0


def quotas_for(plan, scenes) -> dict:
    """由导演方案 + 剧本场次算出**每场配额**（分镜提示词与事后审计共用）。

    `plan`: `{"scenes": [{"sceneId","shotBudget","sceneSeconds",...}]}`
    `scenes`: 剧本的场次列表（要有 `scene_id`/`id`）
    """
    budgets = {}
    for s in (plan or {}).get("scenes") or ():
        sid = str(s.get("sceneId") or "")
        if sid:
            budgets[sid] = s
    rows = []
    for sc in scenes or ():
        sid = str(sc.get("scene_id") or sc.get("id") or "")
        if not sid:
            continue
        b = budgets.get(sid) or {}
        bud = int(b.get("shotBudget") or 0)
        ts = float(b.get("sceneSeconds") or 0)
        if ts <= 0:
            ts = max(12.0, min(40.0, (bud or 3) * 6.0))
        ts = round(ts * _quota_scale(), 1)
        rows.append({"sceneId": sid, "sceneSeconds": ts,
                     "singleMin": float(b.get("singleMin") or 2.0),
                     "singleMax": float(b.get("singleMax") or 8.0),
                     "longestCap": float(b.get("longestCap") or 8.0),
                     "avgTarget": str(b.get("avgTarget") or ""),
                     "allowLong": bool(b.get("allowLong") or False),
                     "priority": int(b.get("priority") or 3),
                     "role": str(b.get("role") or "")})
    cap = sum(int((budgets.get(r["sceneId"]) or {}).get("shotBudget") or 0)
              for r in rows) or None
    return allocate(rows, global_shot_cap=cap)


def audit(quotas, shots) -> dict:
    """把实际镜头表对照配额审计 —— **只看可数的**（2026-11-06，P2 验收用）。

    每场检查三项：镜数是否在 [min,max]、总时长是否超 T_s、单镜是否超上限。
    """
    per: dict[str, dict] = {}
    for q in (quotas or {}).get("scenes") or ():
        per[q["sceneId"]] = {"sceneId": q["sceneId"], "shots": 0, "seconds": 0.0,
                             "maxShot": 0.0, "quota": q}
    for s in shots or ():
        sid = str(getattr(s, "scene_id", "") or "")
        d = float(getattr(s, "duration_sec", 0) or 0)
        row = per.get(sid)
        if row is None:
            continue
        row["shots"] += 1
        row["seconds"] = round(row["seconds"] + d, 2)
        row["maxShot"] = round(max(row["maxShot"], d), 2)

    findings = []
    for sid, row in per.items():
        q = row["quota"]
        if row["shots"] < q["shotMin"]:
            findings.append({"sceneId": sid, "kind": "under-min-shots",
                             "severity": "warn", "shots": row["shots"],
                             "detail": {"shots": row["shots"],
                                        "floor": q["shotMin"]},
                             "reason": f"{row['shots']} 镜 < 下限 {q['shotMin']}"})
        if row["shots"] > q["shotMax"]:
            findings.append({"sceneId": sid, "kind": "over-max-shots",
                             "severity": "warn", "shots": row["shots"],
                             "detail": {"shots": row["shots"],
                                        "cap": q["shotMax"]},
                             "reason": f"{row['shots']} 镜 > 上限 {q['shotMax']}"})
        # ⚠️ findings 必须带**数字**（2026-11-06）：
        # 处置要写"超了多少、上限多少"，只给 reason 字符串的话
        # 下游只能从文本里抠，抠不到就变成 "None"（真跑里出现过）。
        if row["seconds"] > q["sceneSeconds"] + 1e-6:
            findings.append({"sceneId": sid, "kind": "over-scene-seconds",
                             "severity": "warn",
                             "seconds": round(row["seconds"], 2),
                             "shots": row["shots"],
                             "detail": {"seconds": round(row["seconds"], 2),
                                        "cap": q["sceneSeconds"]},
                             "reason": f"该场 {row['seconds']:.1f}s > 上限 "
                                       f"{q['sceneSeconds']:.0f}s"})
        if row["maxShot"] > q["singleMax"] + 1e-6:
            findings.append({"sceneId": sid, "kind": "over-single-max",
                             "severity": "warn",
                             "maxShot": round(row["maxShot"], 2),
                             "detail": {"seconds": round(row["maxShot"], 2),
                                        "cap": q["singleMax"]},
                             "reason": f"单镜 {row['maxShot']:.1f}s > 上限 "
                                       f"{q['singleMax']:.0f}s"})
    total = len(per)
    ok = sum(1 for r in per.values()
             if r["quota"]["shotMin"] <= r["shots"] <= r["quota"]["shotMax"]
             and r["seconds"] <= r["quota"]["sceneSeconds"] + 1e-6)
    return {"kind": "scene.audit", "scenes": list(per.values()),
            "findings": findings, "sceneCount": total,
            "compliantScenes": ok,
            "complianceRate": round(ok / total, 3) if total else 0.0}


#: 允许"超额场"从别处借到的最大比例（借完仍不超过 cap × (1+MAX_BORROW)）
MAX_BORROW = 0.25


def redistribute(quotas, audit, *, round_no: int = 1, max_rounds: int = 1) -> dict:
    """**一轮**时长再分配：把有余量的场次借给超时的场次（P4，2026-11-06）。

    ## 为什么要限"一轮"

    一旦允许反复借，就会出现 A 借给 B → B 超标 → 再借回 A 的**震荡**。
    所以：`round >= max_rounds` 直接返回 `applied=False`，
    并记录 `redistributedFrom`（谁给了谁），**第二轮不再让**。

    ## 规则

    1 出借方：`used < cap` 的场，余量 = `cap - used`
    2 借入方：`used > cap` 的场，缺口 = `used - cap`
    3 借入上限 = `缺口`，且借完 `cap` 不超过 `原 cap × (1 + MAX_BORROW)`
    4 借出方的 `cap` 相应减少（它自己的余量本来就富余）
    5 任何一方都不会被压到 `cap < used × 0.5`（防止把好场次掏空）
    """
    # ⚠️ 用 `round > max_rounds`，不是 `>=`：
    # 第 1 轮就是"唯一允许的那一轮"，写 `>=` 会把正常的一轮也挡掉
    # （第一次跑就踩了：applied=False 且 KeyError）。
    if round_no > max_rounds:
        return {"kind": "quota.redistribution", "applied": False,
                "reason": "已达轮次上限（只允许一轮，防震荡）",
                "moves": [], "round": round_no, "totalMoved": 0.0}

    rows = []
    for q in (quotas or {}).get("scenes") or ():
        row = next((r for r in (audit or {}).get("scenes") or ()
                    if r.get("sceneId") == q.get("sceneId")), None)
        if row is None:
            continue
        rows.append({"q": q, "used": float(row["seconds"]), "cap": float(q["sceneSeconds"])})

    donors = [r for r in rows if r["cap"] - r["used"] > 0.5]
    takers = [r for r in rows if r["used"] - r["cap"] > 0.5]
    donors.sort(key=lambda r: -(r["cap"] - r["used"]))
    takers.sort(key=lambda r: -(r["used"] - r["cap"]))

    moves = []
    for tk in takers:
        need = tk["used"] - tk["cap"]
        room = tk["cap"] * MAX_BORROW
        need = min(need, max(0.0, room))
        for dn in donors:
            if need <= 1e-6:
                break
            spare = dn["cap"] - dn["used"]
            # 不许把出借方掏到"低于已用量的 1.5 倍"
            keep = max(dn["used"] * 1.5, dn["used"] + 1.0)
            spare = min(spare, max(0.0, dn["cap"] - keep))
            give = min(spare, need)
            if give <= 1e-6:
                continue
            tk["cap"] = round(tk["cap"] + give, 1)
            dn["cap"] = round(dn["cap"] - give, 1)
            need -= give
            moves.append({"from": dn["q"]["sceneId"], "to": tk["q"]["sceneId"],
                          "seconds": round(give, 1)})

    applied = []
    for m in moves:
        for r in rows:
            if r["q"]["sceneId"] == m["to"]:
                r["q"]["sceneSeconds"] = r["cap"]
            elif r["q"]["sceneId"] == m["from"]:
                r["q"]["sceneSeconds"] = r["cap"]
    for m in moves:
        applied.append(m)
    return {"kind": "quota.redistribution", "applied": bool(moves),
            "round": round_no, "moves": applied,
            "totalMoved": round(sum(m["seconds"] for m in applied), 1),
            "reason": (f"把 {len(applied)} 次余量借给超时场" if applied
                       else "没有可借的余量（或没有超时场）")}
