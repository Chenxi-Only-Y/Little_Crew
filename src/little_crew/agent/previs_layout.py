"""白模布局的**校验器**（纯几何，不调模型）。

## 为什么它必须先于 LLM 存在

没有校验器，LLM 给出的布局**好坏无从判断** —— 只能靠人一辆辆看。
有了它，LLM 的输出才有"能不能过"这个客观答案，也才谈得上返工。

**所以它是 `PrevisAgent` 的验收标准，不是它的附属品。**

## 最重要的一条：**与 Blender 用同一套数学**

`setup_camera()` 里是这样取景的（`alpha.90` 修过 `sensor_fit`）：

```
cover  = SHOT_COVER[shot_size] * subject_h        # 画面垂直覆盖高度
fov_v  = 2*atan(sensor_height / (2*lens))         # sensor_fit='VERTICAL' 后成立
dist   = (cover/2) / tan(fov_v/2)
focus  = (0, 0, focus_ratio*subject_h)
cam    = focus + (sin(azim)*dist, -cos(azim)*dist, sin(ang)*dist)
```

**本模块逐字复算这套式子。** 若这里换一种算法，
"校验通过"与"Blender 渲出来的"就会是两件事 —— 那种校验比没有更坏。

## 三态（与项目其它门禁一致）

| 判定 | 含义 | 谁给的 |
|---|---|---|
| `KEEP` | 全部可接受 | 无 blocker、无 major |
| `RETRY` | **可改**，且给得出具体怎么改 | 有 major 但可局部修正 |
| `ESCALATE` | **不可自动修**（缺数据 / 自相矛盾） | 缺镜位参数、或镜头表本身矛盾 |

**"缺观测 ≠ 通过"**：拿不到必要字段时判 `ESCALATE`，不判 `KEEP`。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

__all__ = [
    "LOAD",
    "SENSOR_HEIGHT_MM",
    "SHOT_COVER",
    "Issue",
    "Verdict",
    "LayoutCheck",
    "camera_from_shot",
    "project_subject",
    "validate_layout",
]

#: Blender 相机默认 `sensor_height`（mm）。`sensor_fit='VERTICAL'` 后它掌管垂直视场。
SENSOR_HEIGHT_MM: float = 24.0

#: 景别 → 画面垂直覆盖高度（相对主体身高）。**必须与 `render_previs.py` 一致。**
SHOT_COVER: dict[str, float] = {
    "extreme_wide": 8.0, "wide": 4.0, "full": 1.15, "medium_full": 1.0,
    "medium": 0.70, "medium_close": 0.50, "close": 0.34, "extreme_close": 0.20,
}

#: 视线落点高度比例（相对主体身高）。与 Blender 侧一致。
FOCUS_RATIO: float = 0.55

#: 默认主体身高（m）。缺 `height_cm` 时用它 —— 但**用了就要在 issue 里说明**。
DEFAULT_HEIGHT_M: float = 1.68


class Verdict(Enum):
    KEEP = "KEEP"
    RETRY = "RETRY"
    ESCALATE = "ESCALATE"


SEVERITY = {"blocker": 3, "major": 2, "minor": 1}


@dataclass(frozen=True)
class Issue:
    """一条问题。**必须能指到具体对象**（哪个角色、哪个参数、差多少）。"""
    code: str
    severity: str          # blocker / major / minor
    who: str               # 涉及的 shotId 或 subject 名
    detail: str            # 人话：差多少、该往哪改
    fix: str = ""          # 可执行的改法（RETRY 时必须有）

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "severity": self.severity,
                "who": self.who, "detail": self.detail, "fix": self.fix}


@dataclass
class LayoutCheck:
    verdict: Verdict
    issues: tuple[Issue, ...] = ()
    #: 复算出的相机与投影（便于对账，也便于测试直接断言）
    camera: dict[str, Any] = field(default_factory=dict)
    projected: tuple[dict[str, Any], ...] = ()

    @property
    def ok(self) -> bool:
        return self.verdict is Verdict.KEEP

    def as_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict.value,
                "issues": [i.as_dict() for i in self.issues],
                "camera": dict(self.camera),
                "projected": [dict(p) for p in self.projected]}


# ---- 取景（与 Blender 同一套）-----------------------------------------

def camera_from_shot(shot: Mapping[str, Any], *,
                     subject_h: float = DEFAULT_HEIGHT_M) -> dict[str, Any]:
    """按镜头参数算出相机位置与视场。**与 `render_previs.py` 逐字一致。**"""
    size = str(shot.get("shot_size") or "medium")
    cover_mul = SHOT_COVER.get(size)
    if cover_mul is None:
        return {"error": f"未知景别 {size!r}", "known": sorted(SHOT_COVER)}
    cover = cover_mul * float(subject_h)
    # ⚠️ **区分"没给"与"给了非法值"**（2026-10-19 修）。
    #
    # 原来写 `float(shot.get("lens_mm") or 50.0)` —— 于是 `lens_mm: 0` 被
    # 当成"没给"，静默取了默认 50，**一个非法值被判成了合规**。
    # 测试 B5 抓到了这一点。
    #
    # 注意：`angle_deg` / `azimuth_deg` 的 0 是**合法值**（平视、正面），
    # 所以只有 `lens_mm` 需要这种区分。
    raw_lens = shot.get("lens_mm", None)
    lens = 50.0 if raw_lens is None else float(raw_lens)
    if lens <= 0:
        return {"error": f"lens_mm 必须为正（拿到 {lens}）"}
    fov_v = 2 * math.atan(SENSOR_HEIGHT_MM / (2 * lens))
    dist = (cover / 2) / math.tan(fov_v / 2)
    ang = math.radians(float(shot.get("angle_deg") or 0.0))
    azim = math.radians(float(shot.get("azimuth_deg") or 0.0))
    focus = (0.0, 0.0, FOCUS_RATIO * float(subject_h))
    pos = (focus[0] + math.sin(azim) * dist,
           focus[1] - math.cos(azim) * dist,
           focus[2] + math.sin(ang) * dist)
    return {"position": pos, "focus": focus, "fovV": fov_v, "fovH": 0.0,
            "distance": dist, "cover": cover, "lens": lens,
            "shotSize": size, "subjectHeight": float(subject_h)}


def project_subject(subject: Mapping[str, Any], cam: Mapping[str, Any], *,
                    aspect: float = 640 / 384) -> dict[str, Any]:
    """把一个角色投到画面坐标。返回**归一化**位置（0~1，超出即出画）。

    坐标系：角色给 `x`（左右，m）与 `z`（离相机的进深，m，正数）；
    相机在原点朝 -Y。**只做水平与进深，不做俯仰** ——
    白模只关心"在不在画面里"，不关心精确像素。
    """
    if "position" not in cam:
        return {"error": "相机无效，无法投影"}
    dist = cam["distance"]
    # ⚠️ **`z` 就是"角色离相机多远"**（2026-10-19 修）。
    #
    # 第一版写成 `depth = dist - z`，把 `z` 当成"相对焦平面的偏移" ——
    # 于是 `z == dist`（角色正好在主体距离上）被算成进深 0，报"贴脸"。
    # **基线布局直接判了 ESCALATE**，测试当场把它抓出来。
    #
    # 语义要选**最自然、最好核对**的那个：LLM 给"离相机 4 米"，人能一眼判断对不对；
    # 给"相对焦平面 +0.2 米"则要人先算一遍。
    z = float(subject.get("z") or dist)     # 未给就按"主体距离"算
    depth = z                               # 相机到角色的距离（正=在相机前方）
    if depth <= 0.05:
        return {"error": f"角色在相机后方或贴脸（进深 {depth:.2f} m）",
                "depth": depth}
    half_w = math.tan(cam["fovV"] / 2) * aspect * depth
    x = float(subject.get("x") or 0.0)
    u = 0.5 + (x / (2 * half_w)) if half_w else 0.5   # 0~1
    h = float(subject.get("height_cm") or DEFAULT_HEIGHT_M * 100) / 100.0
    cover_at_depth = 2 * math.tan(cam["fovV"] / 2) * depth
    v_frac = h / cover_at_depth if cover_at_depth else 0.0
    return {"u": round(u, 4), "depth": round(depth, 3),
            "heightFraction": round(v_frac, 4),
            "inFrameX": 0.0 <= u <= 1.0,
            "inFrameY": v_frac <= 1.0}


# ---- 校验 --------------------------------------------------------------

def fair_join(*parts: Any) -> str:
    """把"谁 / 哪个道具"拼成一句 —— 便于定位。"""
    return " / ".join(str(p) for p in parts if p)


def validate_layout(layout: Mapping[str, Any], *,
                    aspect: float = 640 / 384) -> LayoutCheck:
    """校验一份白模布局。

    `layout` 形状：

    ```json
    {"shotId": "SH-001",
     "shot":   {"shot_size": "full", "lens_mm": 50, "azimuth_deg": 18,
                "angle_deg": 2, "duration_sec": 6.0, "camera_movement": "dolly_in"},
     "subjects": [{"who": "少女", "x": 0.3, "z": 2.6, "height_cm": 168}],
     "reason": "机位在室内桌侧偏左，朝窗 …"}
    ```
    """
    issues: list[Issue] = []
    sid = str(layout.get("shotId") or "")
    shot = layout.get("shot") or {}
    subjects: Sequence[Mapping[str, Any]] = tuple(layout.get("subjects") or ())

    # ---- 缺观测：直接 ESCALATE（"缺观测 ≠ 通过"）----
    if not shot:
        issues.append(Issue("missing-shot", "blocker", sid,
                            "没有镜头参数（shot）—— 无法推算取景",
                            "补 shot_size / lens_mm / angle_deg / azimuth_deg"))
        return LayoutCheck(Verdict.ESCALATE, tuple(issues))
    if not subjects:
        issues.append(Issue("no-subject", "blocker", sid,
                            "没有任何角色 —— 白模无法表达位置",
                            "至少给一个 subjects 条目"))
        return LayoutCheck(Verdict.ESCALATE, tuple(issues))

    unknown = [s.get("who") or "?" for s in subjects if not s.get("who")]
    if unknown:
        issues.append(Issue("subject-no-name", "major", sid,
                            f"{len(unknown)} 个角色没有名字 —— 下游无法对应",
                            "每个 subjects 都要有 who"))

    # ---- 取景 ----
    heights = [float(s.get("height_cm") or 0) / 100.0 for s in subjects]
    known_h = [h for h in heights if h > 0]
    if not known_h:
        issues.append(Issue("no-height", "major", sid,
                            "所有角色都没给身高 —— 取景按默认算，可能偏",
                            "补 height_cm"))
        subject_h = DEFAULT_HEIGHT_M
    else:
        subject_h = max(known_h)

    cam = camera_from_shot(shot, subject_h=subject_h)
    if "error" in cam:
        issues.append(Issue("bad-shot-param", "blocker", sid,
                            str(cam["error"]),
                            "改用合法景别 / 正的镜头焦距"))
        return LayoutCheck(Verdict.ESCALATE, tuple(issues))

    # ---- 景别与进深是否矛盾 ----
    dist = cam["distance"]
    for s in subjects:
        z = float(s.get("z") or dist)
        who = str(s.get("who") or "?")
        if z <= 0:
            issues.append(Issue("behind-camera", "blocker", who,
                                f"进深 z={z:.2f}m 在相机后方/贴脸",
                                "把 z 改到 0.5m 以上"))
            continue
        if z > dist * 2.5:
            issues.append(Issue("too-far-for-size", "major", who,
                                f"角色距相机 {z:.1f}m，而该景别的主体距离是 "
                                f"{dist:.1f}m —— 会小得像远景",
                                f"把 z 收到 {dist*0.7:.1f}~{dist*1.3:.1f}m，"
                                f"或改用更大的景别"))
        p = project_subject(dict(s, z=z), cam, aspect=aspect)
        if "error" in p:
            issues.append(Issue("project-failed", "blocker", who,
                                str(p["error"]), "检查 z 与相机参数"))
            continue
        if not p["inFrameX"]:
            # 出画：**标出但不一定拦** —— 刻意的画外音/边缘构图是存在的
            issues.append(Issue("out-of-frame-x", "minor", who,
                                f"水平位置 u={p['u']:.2f}（0~1 之外，出画）",
                                "把 x 收到画内，或确认这是刻意的画外构图"))
        # ⚠️ **"不许切头切脚"只对全身类景别成立**（2026-10-19 真机发现）。
        #
        # 第一版对所有景别都查这条，于是 `close` 变成了无解：
        #   · `too-far-for-size` 要求 z ≈ 1.9m（close 的主体距离）
        #   · `too-tall-in-frame` 又要求 158cm 的人在该距离下占高 ≤100%
        # 而 close 的 cover = 0.34×身高 = 0.57m —— **人物必然比画面高，那是特写**。
        #
        # 真机上模型被判 RETRY 两次，每次都在把 z 往远推（6.2m）来躲这条错规则。
        #
        # **正确判据**：只有"该景别本该装下整个人"时才查（cover ≥ 1 倍身高）。
        cover_mul = SHOT_COVER.get(cam["shotSize"], 1.0)
        if not p["inFrameY"] and cover_mul >= 1.0:
            issues.append(Issue("too-tall-in-frame", "major", who,
                                f"角色在画内占高 {p['heightFraction']:.0%} > 100% —— "
                                f"{cam['shotSize']} 景别本该装下整个人",
                                "改用更紧的景别（如 full→medium），或把 z 拉远"))

    # ---- 角色互相重叠 ----
    for i, a in enumerate(subjects):
        for b in subjects[i + 1:]:
            za, zb = float(a.get("z") or dist), float(b.get("z") or dist)
            xa, xb = float(a.get("x") or 0.0), float(b.get("x") or 0.0)
            if abs(za - zb) < 0.15 and abs(xa - xb) < 0.15:
                issues.append(Issue("subjects-overlap", "major",
                                    f"{a.get('who')}/{b.get('who')}",
                                    f"两人位置几乎重合（Δz={abs(za-zb):.2f}m "
                                    f"Δx={abs(xa-xb):.2f}m）—— 白模里会叠成一个",
                                    "把两人的 x 或 z 拉开"))

    # ---- 穿模：人站在道具里面（**真机发现**）----
    #
    # 2026-10-19 实测：agent 把「少女坐在桌边」摆成了
    #   少女 x=0.45 z=2.9   桌 x=0.45 z=2.85 w=1.2 d=0.7
    # 桌子在 z 占 2.5~3.2、x 占 -0.15~1.05 —— **她落在桌子里面**。
    # 渲出来的画面是"桌沿横切过人体"，一眼就假。
    #
    # 判据：人（一个半径 `半径比×身高` 的竖向圆柱）与盒子在**水平面上相交**，
    # 且盒子**足够高**（茶几/地垫这类矮物件可以踩上去，不算穿模）。
    for prop in (layout.get("set") or ()):
        if not isinstance(prop, Mapping):
            continue
        try:
            pw = float(prop.get("w") or 0)
            pd = float(prop.get("d") or 0)
            ph = float(prop.get("h") or 0)
            px = float(prop.get("x") or 0)
            pz = float(prop.get("z") or 0)
        except (TypeError, ValueError):
            continue
        if pw <= 0 or pd <= 0 or ph <= 0:
            continue
        for s in subjects:
            who = str(s.get("who") or "?")
            h = float(s.get("height_cm") or DEFAULT_HEIGHT_M * 100) / 100.0
            body_r = h * 0.085
            # 矮于 30% 身高的物件视为"可以踩上去"（地垫、矮台），不报
            if ph < h * 0.30:
                continue
            # **薄物件当平面看**（门/窗/墙）：人站在门框里是正常的。
            #
            # 第一版没这条，把「旅人站在门口」判成了穿模 ——
            # 而门只有 0.1m 厚，它是**一块板**，不是可以"进去"的体积。
            # 判据：最薄的水平边 < 0.25m → 平面，不参与体积相交。
            if min(pw, pd) < 0.25:
                continue
            sv = float(s.get("z") or dist)
            sx = float(s.get("x") or 0.0)
            if (abs(sx - px) < pw / 2 + body_r * 0.6
                    and abs(sv - pz) < pd / 2 + body_r * 0.6):
                issues.append(Issue(
                    "subject-inside-prop", "major", fair_join(who, prop),
                    f"{who} 落在「{prop.get('label') or '道具'}」里面"
                    f"（人 x={sx:.2f} z={sv:.2f}，"
                    f"道具占 x={px-pw/2:.2f}~{px+pw/2:.2f} "
                    f"z={pz-pd/2:.2f}~{pz+pd/2:.2f}）—— 穿模",
                    f"把人移到道具外侧，或把「{prop.get('label') or '道具'}」挪开"
                    f">{max(pw, pd)/2 + body_r:.2f}m"))

    # ---- 缺 reason ----
    if not str(layout.get("reason") or "").strip():
        issues.append(Issue("no-reason", "major", sid,
                            "没有给空间理由 —— 评审无法判断这个机位是否合理",
                            "补一句可被人反驳的 reason"))

    # ---- 裁定（与项目门禁同一顺序）----
    has = {s: any(i.severity == s for i in issues) for s in SEVERITY}
    if has["blocker"]:
        verdict = Verdict.ESCALATE
    elif has["major"]:
        verdict = Verdict.RETRY
    else:
        verdict = Verdict.KEEP
    # RETRY 必须给得出改法 —— 给不出就是 ESCALATE
    if verdict is Verdict.RETRY and any(
            i.severity == "major" and not i.fix for i in issues):
        verdict = Verdict.ESCALATE
    return LayoutCheck(verdict, tuple(issues), camera=dict(cam),
                       projected=tuple(
                           project_subject(dict(s, z=float(s.get("z") or dist)),
                                           cam, aspect=aspect)
                           for s in subjects))
