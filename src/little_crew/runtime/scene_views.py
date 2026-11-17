"""场景空间视图（2026-11-17）：**上游自己决策**的两档场景资产。

## 两档（"2 合 1"）

| 档 | 产物 | 何时选 |
|---|---|---|
**Tier 1** | 1 张空景锚点图（无人建立镜头） | 普通场/一次性过场 |
**Tier 2** | 4 张：基准空景 + 反向平视 + 45° 斜俯全景 + 90° 正交俯视平面图 | 主场景：反复出现、多机位、导演标为高潮/抒情/高优先级/允许长镜头 |

## "按上游自己决策"

档位不是人选的，是**上游信号**决定的：导演计划的 `role/priority/allowLong`、
该场镜头数、是否跨场复用。决策依据写进证据（`decision.factors`），可复核。

## 纪律

- 每步都**显式约束**（出入口/陈设相对位置/无人物/无文字），否则多图会互相打架
- **正俯图只当空间草图/ControlNet 输入**，不回灌 img2img 参考（模型对正交图理解差）
- 失败**不阻断**整集：记 error 后继续
- 云端才走（¥0.05/张）；本地路径暂标 skipped，不静默假装生成
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    "COST_PER_IMAGE_CNY",
    "choose_tier",
    "build_prompts",
    "generate",
    "anchor_facts",
    "write_report",
]

#: 云端单张价（与 Sudorelay 一致）
COST_PER_IMAGE_CNY = 0.05
DEFAULT_SIZE = "1536x1024"

#: 导演计划里"值得多花 3 张图"的角色
_HERO_ROLES = ("高潮", "抒情")

#: 模型经常无视"无人物" → 这是给 QC 重试用的一句话
_EMPTY_HARD = "（这是空景资产：画面中绝对不允许出现任何人形、人物、剪影或手；只有环境。）"

#: Tier3 九宫格机位矩阵：key -> (行, 列, 标签, 机位说明)
#: 行=3，列=3；标签用于程序拼板（**不靠模型写字**）。
TIER3_VIEWS: tuple[tuple[str, int, int, str, str], ...] = (
    ("front_wide", 1, 1, "正面全景", "人眼高度主视角，完整呈现空间整体形态"),
    ("front_close", 1, 2, "正面近景", "正面推近主体，聚焦入口、中轴线与细节"),
    ("side_wide", 1, 3, "侧面全景", "侧面展示体量关系、轮廓与空间纵深"),
    ("side_close", 2, 1, "侧面近景", "侧面近景，补充立面设计与结构层级"),
    ("back_wide", 2, 2, "背面全景", "反向机位，完整展示背面结构与布局"),
    ("back_close", 2, 3, "背面近景", "背面近景，展示隐藏空间与装饰系统"),
    ("top_close", 3, 1, "俯视近景", "俯视聚焦主体区域，展示顶部结构与空间细节"),
    ("topdown", 3, 2, "正交平面图",
     "90° 垂直正俯正交平面图：无透视，标注出入口与主要陈设的相对位置（空间草图/ControlNet 用）"),
    ("hero45", 3, 3, "45°英雄视角", "45°高位鸟瞰，同时展示立面与总体布局"),
)

#: Tier3 统一摄影参数（写进每个派生视图，保证一致）
_TIER3_CAMERA = "统一 50mm 焦段，统一曝光/白平衡/景深/色彩风格；16:9 画幅；只允许摄影机机位移动。"


def _scene_id(x: Any) -> str:
    if isinstance(x, Mapping):
        return str(x.get("sceneId") or x.get("scene_id") or "")
    return str(getattr(x, "scene_id", "") or "")


def _location_key(heading: Any) -> str:
    """场景"地点键"：取标题第一段（去掉时段），用于统计剧本里同一地点的出现次数。"""
    s = str(heading or "").strip()
    if not s:
        return ""
    parts = [p.strip() for p in s.split("·") if p.strip()]
    if not parts:
        return ""
    # 排除纯时段
    _TIME = {"夜", "日", "晨", "黄昏", "清晨", "深夜", "午后", "傍晚", "凌晨", "雨"}
    for p in parts:
        if p not in _TIME:
            return p
    return parts[0]


def scene_frequency(script: Any, shots: Sequence[Any]) -> dict[str, dict[str, Any]]:
    """按**剧本场景出现频率**给每个场景打分。

    `score = 该场镜头数 + 该地点在剧本中出现的次数 (+2 跨集复用)`

    - Tier1：score ≤ 2（一次性、单镜/两镜过场）
    - Tier2：3 ≤ score ≤ 4（普通主场景）
    - Tier3：score ≥ 5（高频/多机位/复用；仍受每集上限约束）
    """
    scenes: list[Any] = []
    if isinstance(script, Mapping):
        scenes = list(script.get("scenes") or ())
    elif script is not None:
        scenes = list(getattr(script, "scenes", ()) or ())
    occ: dict[str, int] = {}
    heading_of: dict[str, str] = {}
    for s in scenes:
        if isinstance(s, Mapping):
            sid = str(s.get("id") or s.get("sceneId") or s.get("scene_id") or "")
            head = s.get("heading") or s.get("location") or ""
        else:
            sid = str(getattr(s, "scene_id", "") or getattr(s, "id", "") or "")
            head = getattr(s, "heading", "") or getattr(s, "location", "")
        if not sid:
            continue
        heading_of[sid] = _location_key(head)
        occ[heading_of[sid]] = occ.get(heading_of[sid], 0) + 1
    per_scene: dict[str, int] = {}
    for s in shots or ():
        sid = _scene_id(s)
        if sid:
            per_scene[sid] = per_scene.get(sid, 0) + 1
    out: dict[str, dict[str, Any]] = {}
    for sid in per_scene:
        location = heading_of.get(sid) or sid
        occurrences = occ.get(location, 1)
        score = per_scene[sid] + occurrences
        if score <= 2:
            tier = 1
        elif score <= 4:
            tier = 2
        else:
            tier = 3
        out[sid] = {"sceneId": sid, "location": location,
                    "shots": per_scene[sid], "occurrences": occurrences,
                    "score": score, "tier": tier,
                    "factors": [f"剧本地点出现 {occurrences} 次",
                                f"该场 {per_scene[sid]} 镜",
                                f"频率分 {score}"]}
    return out


def choose_tier(freq: Mapping[str, Any]) -> dict[str, Any]:
    """按频率结果返回档位（纯函数，供测试/报告直接调用）。"""
    return {"tier": int(freq.get("tier") or 1), "score": int(freq.get("score") or 0),
            "factors": list(freq.get("factors") or [])}


def _join(v: Any) -> str:
    if isinstance(v, (list, tuple)):
        return "、".join(str(x) for x in v if str(x).strip())
    return str(v or "").strip()


def _base_prompt(card: Mapping[str, Any], language: str = "zh") -> str:
    """空景锚点提示词：结构优先、无人物、要紧信息放前面。"""
    space = str(card.get("space") or "（空间未定）")
    mats = _join(card.get("materials"))
    props = _join(card.get("props"))
    palette = _join(card.get("palette"))
    era = str(card.get("era") or "")
    if str(language).lower().startswith("zh"):
        return (
            f"固定机位广角建立镜头，{space}，{era or '当代'}质感，夜间/当前时段。"
            f"前景：框景元素；中景：主体空间；后景：纵深延伸。"
            f"出入口与主要陈设在画面中位置明确、固定。"
            f"材质：{mats or '待定'}；道具：{props or '待定'}；主色：{palette or '待定'}。"
            "光源：主光方向与色温明确，明暗对比清晰。空间结构连续、自洽。"
            "画面中没有任何人物、没有手、没有文字水印、没有分屏；电影质感，高精度 3D 半写实国漫 CG。"
        )
    return (
        f"wide establishing shot, locked-off camera, empty scene, {space}, "
        f"{era or 'contemporary'} era. clear foreground/midground/background layers; "
        f"entrances and key set pieces fixed in frame. materials: {mats or 'tbd'}; "
        f"props: {props or 'tbd'}; palette: {palette or 'tbd'}. "
        "clear key light direction and color temperature, coherent continuous space. "
        "no people, no hands, no text, no watermark, no split screen; "
        "cinematic still, high-detail 3D semi-realistic CG."
    )


def build_prompts(scene_id: str, card: Mapping[str, Any], tier: int,
                  language: str = "zh") -> dict[str, str]:
    """返回该场景要生成的视图 → 提示词（Tier1 只有 base）。"""
    base = _base_prompt(card, language)
    out = {"base": base}
    if int(tier) >= 3:
        lock = ("严格执行场景一致性锁定：空间形态/结构/比例/布局/材质/色彩体系/光照与阴影方向/"
                "体积光/天气与时段全部锁死；禁止重新设计、禁止增减体量、禁止改布局；"
                "**唯一许可：只移动摄影机机位**。")
        out = {}          # Tier3：front_wide 即基准，共 9 张（不额外多一张）
        # ⚠️ 必须把**空景锚点内容**（场景卡：空间/材质/道具/色板/年代）带进每个视图，
        # 否则九宫格只剩"锁死/机位"这些空话，模型会自由发挥（实测：地铁站厅被画成古风房间）。
        _content = base
        for key, _row, _col, _label, _desc in TIER3_VIEWS:
            out[key] = (
                f"{_content}\n{lock}{_label}：{_desc}。以基准图为参考，保持同一空间不漂移。"
                f"{_TIER3_CAMERA}"
                "画面中没有任何人物、没有手、没有文字水印；高精度 3D 半写实国漫 CG。"
                if str(language).lower().startswith("zh") else
                f"{lock} {_label}: {_desc}. reference the base image, same space. "
                "50mm, uniform exposure/WB/DOF/color, 16:9, camera move only. "
                "no people, no hands, no text; high-detail 3D semi-realistic CG."
            )
        return out
    if int(tier) >= 2:
        out["reverse"] = (
            f"同一空间（{scene_id}）的**反向平视回望**：机位在基准图对面、平视，"
            "补全基准图没拍到的身后那面墙与陈设。布局/材质/主色/光位与基准图一致。"
            "画面中没有任何人物、没有手、没有文字；电影质感，高精度 3D 半写实国漫 CG。"
            if str(language).lower().startswith("zh") else
            f"reverse angle eye-level shot of the SAME space ({scene_id}): camera opposite "
            "the base view, completing the unseen wall and set pieces. keep layout, materials, "
            "palette and key light identical. no people, no hands, no text; cinematic CG."
        )
        out["oblique45"] = (
            "仍以基准图为参考，45° 俯视斜角全景：补全平视看不出的人物/物体的远近关系，"
            "**固化当前区域的布置**。保持材质、色板、出入口位置一致。无人物、无文字。"
            if str(language).lower().startswith("zh") else
            "high-angle 45-degree oblique wide establishing shot of the SAME space, "
            "revealing depth relationships and locking the layout. keep materials, palette "
            "and entrances consistent. no people, no text."
        )
        out["topdown"] = (
            "以基准/反向/斜俯三张图为参考，生成 90° 垂直正俯的**正交平面图**："
            "无透视、无立面、标注出入口与主要陈设的相对位置。线稿/平面图风格，无人物、无文字。"
            if str(language).lower().startswith("zh") else
            "orthographic top-down floor plan of the SAME space, 90-degree bird's-eye view, "
            "no perspective, mark entrances and key set-piece positions. line-art plan style, "
            "no people, no text."
        )
    return out


def anchor_facts(card: Mapping[str, Any], limit: int = 5) -> list[str]:
    """从场景卡抽 3~5 条**可核对**锚点事实（给 QC/人工核对）。"""
    out: list[str] = []
    for k in ("space", "materials", "props", "palette"):
        v = card.get(k)
        if isinstance(v, (list, tuple)):
            out.extend(str(x).strip() for x in v if str(x).strip())
        elif str(v or "").strip():
            out.append(str(v).strip())
    # 去重保序，最多 limit
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq[:limit]


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


def _cloud_key() -> str:
    try:
        from little_crew.config import env_value

        return str(env_value("SUDORELAY_API_KEY", "") or "")
    except Exception:                                        # noqa: BLE001
        return ""


def _post_json(url: str, body: dict, key: str, timeout: int = 600) -> dict:
    from little_crew import net

    req = net.urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "User-Agent": "OpenAI/Python 1.0"})
    with net.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def _save_b64(obj: dict, dst: Path) -> bool:
    item = (obj.get("data") or [{}])[0]
    img = None
    if item.get("b64_json"):
        img = base64.b64decode(item["b64_json"])
    elif item.get("url"):
        from little_crew import net

        with net.urlopen(net.urllib.request.Request(
                item["url"], headers={"User-Agent": "OpenAI/Python 1.0"}),
                timeout=600) as r:
            img = r.read()
    if not img:
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(img)
    return True


def _default_people_check(path: Path) -> str:
    """VLM 判"图里有没有人"：返回 yes/no/unknown。失败不抛。"""
    try:
        import base64 as _b64

        from little_crew.agent.llm import LlmClient

        b64 = _b64.b64encode(Path(path).read_bytes()).decode("ascii")
        c = LlmClient("vlm-judge-cloud", temperature=0.2, timeout_s=90)
        txt = (c.chat_image("图中是否出现任何人物/人形/剪影/手？只回 yes 或 no。",
                            b64, mime="image/png", what="scene.people") or "").lower()
        if "yes" in txt:
            return "yes"
        if "no" in txt:
            return "no"
        return "unknown"
    except Exception:                                        # noqa: BLE001
        return "unknown"


def _default_t2i(prompt: str, dst: Path) -> dict:
    key = _cloud_key()
    if not key:
        return {"ok": False, "error": "缺少 SUDORELAY_API_KEY"}
    t0 = time.time()
    try:
        obj = _post_json("https://api.sudorelay.com/v1/images/generations",
                         {"model": "gpt-image-2.6-sunburst", "prompt": prompt,
                          "size": DEFAULT_SIZE, "n": 1}, key)
        if not _save_b64(obj, dst):
            return {"ok": False, "error": "响应里没有图片"}
        return {"ok": True, "out": str(dst), "seconds": round(time.time() - t0, 1)}
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}


def _default_edit(ref: Path, prompt: str, dst: Path) -> dict:
    """以参考图做 img2img（Sudorelay /images/edits）。"""
    key = _cloud_key()
    if not key:
        return {"ok": False, "error": "缺少 SUDORELAY_API_KEY"}
    if not Path(ref).is_file():
        return {"ok": False, "error": f"参考图不存在：{ref}"}
    t0 = time.time()
    try:
        from little_crew import net

        data = Path(ref).read_bytes()
        raw = net.post_multipart(
            "https://api.sudorelay.com/v1/images/edits",
            {"model": "gpt-image-2.6-sunburst", "prompt": prompt,
             "size": DEFAULT_SIZE, "n": "1"},
            {"image": ("ref.png", data)},
            headers={"Authorization": f"Bearer {key}",
                     "User-Agent": "OpenAI/Python 1.0"}, timeout=600)
        obj = json.loads(raw.decode("utf-8", errors="replace"))
        if not _save_b64(obj, dst):
            return {"ok": False, "error": "响应里没有图片"}
        return {"ok": True, "out": str(dst), "seconds": round(time.time() - t0, 1)}
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}


def generate(*, scene_cards: Mapping[str, Mapping[str, Any]],
             shots: Sequence[Any], director_plan: Mapping[str, Any] | None,
             workdir: str | Path, enabled: bool = False, language: str = "zh",
             script: Any = None,
             tiers: Mapping[str, Any] | None = None,
             reuse_map: Mapping[str, Any] | None = None,
             size: str = DEFAULT_SIZE,
             t2i: Callable[[str, Path], dict] | None = None,
             edit: Callable[[Path, str, Path], dict] | None = None,
             verify: bool = False,
             checker: Callable[[Path], str] | None = None,
             tier3_enabled: bool = True,
             tier3_max: int = 2,
             ) -> dict:
    """按上游决策生成场景视图；返回证据 dict（不抛）。"""
    wd = Path(str(workdir))
    rep: dict[str, Any] = {"kind": "scene.views", "enabled": bool(enabled),
                           "size": size, "scenes": {}, "costCny": 0.0,
                           "generated": 0, "failed": 0, "peopleWarnings": 0}
    if not enabled:
        return rep
    # 统计每场镜数 + 导演计划映射
    per_scene: dict[str, int] = {}
    for s in shots or ():
        sid = _scene_id(s)
        if sid:
            per_scene[sid] = per_scene.get(sid, 0) + 1
    plan = dict(director_plan or {})
    plan_scenes = {str(x.get("sceneId") or ""): x
                   for x in (plan.get("scenes") or ()) if isinstance(x, Mapping)}
    recurring = {sid for sid, n in per_scene.items() if n >= 3}
    _t2i = t2i or _default_t2i
    _edit = edit or _default_edit
    _check = checker or (_default_people_check if verify else None)
    # ---- 档位来源：外部决策（LLM/人工）优先，否则频率启发式 ----
    if tiers is not None:
        _decisions = {}
        _src = "external"
        for sid in per_scene:
            row = tiers.get(sid)
            if isinstance(row, Mapping):
                try:
                    tier = int(row.get("tier") or 1)
                except (TypeError, ValueError):
                    tier = 1
                if tier not in (1, 2, 3):
                    tier = 1
                _src = str(row.get("source") or _src)
                _decisions[sid] = {
                    "tier": tier, "score": int(row.get("score") or 0),
                    "factors": (list(row.get("factors") or ())
                                + ([str(row.get("reason"))] if row.get("reason") else [])),
                    "source": _src,
                }
            else:
                try:
                    tier = int(row or 1)
                except (TypeError, ValueError):
                    tier = 1
                _decisions[sid] = {"tier": tier if tier in (1, 2, 3) else 1,
                                   "score": 0, "factors": ["外部指定档位"],
                                   "source": "external"}
        rep["decisionSource"] = _src
    else:
        _freq = scene_frequency(script, shots) if script is not None else {
            sid: {"sceneId": sid, "shots": per_scene[sid], "occurrences": 1,
                  "score": per_scene[sid] + 1,
                  "tier": (1 if per_scene[sid] + 1 <= 2 else
                           (2 if per_scene[sid] + 1 <= 4 else 3)),
                  "location": sid,
                  "factors": [f"该场 {per_scene[sid]} 镜（未提供剧本，按镜数）"]}
            for sid in per_scene}
        _decisions = {sid: choose_tier(_freq.get(sid) or {}) for sid in per_scene}
        rep["decisionSource"] = "heuristic"
        if tier3_enabled and tier3_max > 0:
            _cands = sorted((sid for sid, d in _decisions.items()
                             if int(d["tier"]) >= 2),
                            key=lambda s: (-int(_decisions[s]["score"]), s))[:tier3_max]
            for sid in _cands:
                if int(_decisions[sid]["tier"]) == 3:
                    continue
                _decisions[sid]["baseTier"] = int(_decisions[sid]["tier"])
                _decisions[sid]["tier"] = 3
                _decisions[sid]["factors"] = list(_decisions[sid]["factors"]) + [
                    f"Tier3：九宫格+平面图（每集上限 {tier3_max} 场）"]
    for sid in sorted(per_scene):
        card = dict(scene_cards.get(sid) or {})
        dec = _decisions[sid]
        tier = int(dec["tier"])
        prompts = build_prompts(sid, card, tier, language)
        outdir = wd / "assets" / "scene_views" / sid
        rows: list[dict] = []
        base: Path | None = None
        _reuse = (reuse_map or {}).get(sid) or {}
        _reuse_views = (dict(_reuse.get("assets") or {})
                        if _reuse.get("reuse") else {})

        def _copy_reused(view: str, dst: Path) -> dict | None:
            src = _reuse_views.get(view)
            if not src or not Path(str(src)).is_file():
                return None
            import shutil as _sh

            dst.parent.mkdir(parents=True, exist_ok=True)
            _sh.copy2(str(src), str(dst))
            return {"ok": True, "out": str(dst), "seconds": 0.0, "reused": True}
        n_expected = len(prompts)
        _first_view = ("front_wide" if tier >= 3 else "base")
        for view, prompt in prompts.items():
            dst = outdir / f"{view}.png"
            ref_used = None
            _pre = _copy_reused(view, dst)
            if _pre is not None:
                res = _pre
            elif view == _first_view:
                res = _t2i(prompt, dst)
            else:
                # 反向/45°/正俯都以基准图为参考；正俯只用于空间草图（不再回灌）
                ref_used = base
                if base is None:
                    res = {"ok": False, "error": "基准图未生成，跳过派生视图"}
                else:
                    res = _edit(base, prompt, dst)
            # ---- 空景 QC（模型经常无视"无人物"）----
            _people = ""
            _tries = 1
            if (_check is not None and res.get("ok") and res.get("out")
                    and not res.get("reused")):
                _people = str(_check(Path(str(res["out"]))) or "")
                if _people == "yes":
                    # 重试一次：提示词追加硬约束
                    _dst2 = outdir / f"{view}__retry.png"
                    _p2 = prompt + _EMPTY_HARD
                    _res2 = (_t2i if view == _first_view else
                             (lambda p_, d_: _edit(base, p_, d_)))(_p2, _dst2)
                    _tries = 2
                    if _res2.get("ok"):
                        res, _dst_used = _res2, _dst2
                        _people = str(_check(Path(str(_res2["out"]))) or "")
                    else:
                        row2 = {"view": view, "promptHash": _hash(_p2),
                                "ok": False, "error": _res2.get("error", ""),
                                "retry": True}
                        res = _res2
            row = {"view": view, "promptHash": _hash(prompt),
                   "reference": (str(ref_used) if ref_used else ""),
                   "ok": bool(res.get("ok")), "out": res.get("out"),
                   "seconds": res.get("seconds"), "error": res.get("error", ""),
                   "peopleDetected": _people, "tries": _tries,
                   "reused": bool(res.get("reused"))}
            if _people == "yes":
                rep["peopleWarnings"] += 1
            rows.append(row)
            if row["ok"] and view == _first_view:
                base = Path(str(row["out"]))
        ok_n = sum(1 for r in rows if r["ok"])
        _attempts = sum(int(r.get("tries") or 1) for r in rows
                        if r["ok"] and not r.get("reused"))
        _reused_n = sum(1 for r in rows if r.get("reused"))
        rep["reused"] = int(rep.get("reused") or 0) + _reused_n
        rep["costCny"] = round(rep["costCny"] + _attempts * COST_PER_IMAGE_CNY, 2)
        rep["generated"] += ok_n
        rep["failed"] += (n_expected - ok_n)
        _board = None
        if tier >= 3:
            _bd = outdir / "board.jpg"
            if _compose_board(rows, _bd, sid):
                _board = str(_bd)
        rep["scenes"][sid] = {
            "sceneId": sid, "tier": tier, "decision": dec,
            "board": _board,
            "matrix": ([{"view": k, "row": r, "col": c, "label": lb}
                        for k, r, c, lb, _d in TIER3_VIEWS] if tier >= 3 else []),
            "shots": per_scene[sid], "anchors": anchor_facts(card),
            "reused": bool(_reuse.get("reuse")),
            "reusedFrom": str(_reuse.get("from") or ""),
            "reusedViews": sum(1 for r in rows if r.get("reused")),
            "generatedViews": sum(1 for r in rows
                                  if r.get("ok") and not r.get("reused")),
            "sceneHash": _hash(json.dumps(dict(card), sort_keys=True, ensure_ascii=False)),
            "images": rows,
            "ok": ok_n == n_expected,
            "note": ("Tier3 九宫格矩阵：9 张分开出图 + 程序拼 3×3 展示板；"
                     "正俯只作空间草图/ControlNet 输入"
                     if tier >= 3 else
                     ("正俯图只作空间草图/ControlNet 输入，不回灌 img2img"
                      if tier >= 2 else "轻量档：仅空景锚点")),
        }
    return rep


def _compose_board(views: list[dict], dst: Path, scene_id: str) -> bool:
    """把 Tier3 的 9 张图拼成 3×3 展示板（编号+名称由程序写，不靠模型）。"""
    try:
        from PIL import Image, ImageDraw

        # 中文字体（Windows），找不到就退回 ASCII 机位键，避免"口口"乱码
        _font = None
        try:
            from PIL import ImageFont

            for _fp in (r"C:\Windows\Fonts\msyh.ttc",
                        r"C:\Windows\Fonts\msyhbd.ttc",
                        r"C:\Windows\Fonts\simhei.ttf"):
                if Path(_fp).is_file():
                    _font = ImageFont.truetype(_fp, 18)
                    break
        except Exception:                                    # noqa: BLE001
            _font = None
        cells: dict[tuple[int, int], tuple[Path, str]] = {}
        pos = {k: (r, c, label) for k, r, c, label, _d in TIER3_VIEWS}
        for v in views:
            if not v.get("ok") or not v.get("out"):
                continue
            p = Path(str(v["out"]))
            if p.is_file() and p.parent.name == scene_id or p.is_file():
                key = str(v.get("view"))
                if key in pos:
                    r, c, label = pos[key]
                    cells[(r, c)] = (p, label)
        if len(cells) < 9:
            return False
        W = 640
        _im = Image.open(next(iter(cells.values()))[0]).convert("RGB")
        H = int(W * _im.height / max(_im.width, 1))
        pad, cap = 8, 26
        board = Image.new("RGB", (3 * W + 4 * pad, 3 * (H + cap) + 4 * pad), (18, 18, 22))
        d = ImageDraw.Draw(board)
        for (r, c), (p, label) in sorted(cells.items()):
            im = Image.open(p).convert("RGB").resize((W, H))
            x = pad + (c - 1) * (W + pad)
            y = pad + (r - 1) * (H + cap + pad)
            board.paste(im, (x, y))
            n = (r - 1) * 3 + c
            if _font is not None:
                d.text((x + 4, y + H + 4), f"{n:02d} {label}",
                       fill=(230, 230, 235), font=_font)
            else:
                d.text((x + 4, y + H + 4), f"{n:02d} view{n}", fill=(230, 230, 235))
        dst.parent.mkdir(parents=True, exist_ok=True)
        board.save(dst, quality=90)
        return True
    except Exception:                                        # noqa: BLE001
        return False


def write_report(workdir: str | Path, report: Mapping[str, Any]) -> Path:
    """写 `delivery/scene_views.json`。"""
    out = Path(str(workdir)) / "delivery" / "scene_views.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dict(report), ensure_ascii=False, indent=2),
                   encoding="utf-8")
    return out
