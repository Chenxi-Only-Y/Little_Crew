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


def _scene_id(x: Any) -> str:
    if isinstance(x, Mapping):
        return str(x.get("sceneId") or x.get("scene_id") or "")
    return str(getattr(x, "scene_id", "") or "")


def choose_tier(*, shots_in_scene: int, director_scene: Mapping[str, Any] | None = None,
                recurring: bool = False) -> dict[str, Any]:
    """上游决策：返回 `{tier, score, factors}`。**纯函数**，可复核。"""
    score = 0
    factors: list[str] = []
    if shots_in_scene >= 2:
        score += 1
        factors.append(f"该场 {shots_in_scene} 镜（多机位）")
    d = dict(director_scene or {})
    role = str(d.get("role") or "")
    if role in _HERO_ROLES:
        score += 2
        factors.append(f"导演标为「{role}」")
    try:
        prio = int(d.get("priority") or 0)
    except (TypeError, ValueError):
        prio = 0
    if prio >= 4:
        score += 1
        factors.append(f"优先级 {prio}≥4")
    if bool(d.get("allowLong")):
        score += 1
        factors.append("允许长镜头（空间需连续）")
    if recurring:
        score += 2
        factors.append("跨场/跨集复用")
    tier = 2 if score >= 2 else 1
    return {"tier": tier, "score": score, "factors": factors or ["普通场/一次性"]}


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
             size: str = DEFAULT_SIZE,
             t2i: Callable[[str, Path], dict] | None = None,
             edit: Callable[[Path, str, Path], dict] | None = None,
             verify: bool = False,
             checker: Callable[[Path], str] | None = None,
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
    for sid in sorted(per_scene):
        card = dict(scene_cards.get(sid) or {})
        dec = choose_tier(shots_in_scene=per_scene[sid],
                          director_scene=plan_scenes.get(sid),
                          recurring=sid in recurring)
        tier = int(dec["tier"])
        prompts = build_prompts(sid, card, tier, language)
        outdir = wd / "assets" / "scene_views" / sid
        rows: list[dict] = []
        base: Path | None = None
        n_expected = len(prompts)
        for view, prompt in prompts.items():
            dst = outdir / f"{view}.png"
            ref_used = None
            if view == "base":
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
            if _check is not None and res.get("ok") and res.get("out"):
                _people = str(_check(Path(str(res["out"]))) or "")
                if _people == "yes":
                    # 重试一次：提示词追加硬约束
                    _dst2 = outdir / f"{view}__retry.png"
                    _p2 = prompt + _EMPTY_HARD
                    _res2 = (_t2i if view == "base" else
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
                   "peopleDetected": _people, "tries": _tries}
            if _people == "yes":
                rep["peopleWarnings"] += 1
            rows.append(row)
            if row["ok"] and view == "base":
                base = Path(str(row["out"]))
        ok_n = sum(1 for r in rows if r["ok"])
        _attempts = sum(int(r.get("tries") or 1) for r in rows if r["ok"])
        rep["costCny"] = round(rep["costCny"] + _attempts * COST_PER_IMAGE_CNY, 2)
        rep["generated"] += ok_n
        rep["failed"] += (n_expected - ok_n)
        rep["scenes"][sid] = {
            "sceneId": sid, "tier": tier, "decision": dec,
            "shots": per_scene[sid], "anchors": anchor_facts(card),
            "sceneHash": _hash(json.dumps(dict(card), sort_keys=True, ensure_ascii=False)),
            "images": rows,
            "ok": ok_n == n_expected,
            "note": ("正俯图只作空间草图/ControlNet 输入，不回灌 img2img"
                     if tier >= 2 else "轻量档：仅空景锚点"),
        }
    return rep


def write_report(workdir: str | Path, report: Mapping[str, Any]) -> Path:
    """写 `delivery/scene_views.json`。"""
    out = Path(str(workdir)) / "delivery" / "scene_views.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dict(report), ensure_ascii=False, indent=2),
                   encoding="utf-8")
    return out
