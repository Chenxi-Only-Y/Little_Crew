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
                priority: int = 3) -> dict:
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

    n_max = max(1, min(MAX_SHOTS_PER_SCENE, int(math.floor(ts / smin))))
    # 至少几镜才填得满：用"有效上限"估，再留 1.5 倍余量，避免强迫长镜头
    n_min = max(1, min(MAX_SHOTS_PER_SCENE, int(math.ceil(ts / (eff * 1.5)))))
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
