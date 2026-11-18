"""边界重拍（2026-11-17，B2；2026-11-18 补自动循环）。

边界审判红后，**只重拍边界镜**是关键：
- 边界镜 = 本章前 1~3 镜（接戏镜），不是整章
- 本模块负责**定位/计划/执行/复审循环/证据**；真正的局部重渲由渲染阶段按 shotId 执行
- 重试上限：默认 2 次；仍红 -> 升级人工（不无限烧钱）

B2 自动循环（`run_retake_loop`）：

```
边界审红
  -> 契约红？ 是：补拍图片修不了契约 -> 不重渲，直接升级人工（写 retake-escalation.json）
  -> 否：hint_from_diffs 从 diff 自动生成针对性中文 hint
        -> run_retake：只重渲边界镜（shot manifest 版本+1）
        -> judge(ctx) 复审（VLM 真审；judge 由调用方注入）
        -> 绿/黄：结束；仍红：把新 diff 合并回 boundary，再生成更强的 hint 重试
        -> 达到 max_retries 仍红：升级人工
```

诚实边界：
- **没有复审判官**（judge=None：未开 VLM/无帧）时**不假装复审**，直接升级人工；
- 没有边界镜（无 shot.json）不硬跑，直接升级/说明；
- 每次尝试成本按 `run_retake` 结果累计，写进 `history` / `costCny`；
- 重试上限严格 = max_retries（默认 2），不会多跑一轮。

证据文件（向后兼容，`plan`/`write`/`run_retake` 的签名与证据文件保持不变）：
- `delivery/retake-plan.json`   —— 重拍计划（`write`）
- `delivery/retake-result.json` —— 单次重渲结果（`run_retake`）
- `delivery/retake-loop.json`   —— 自动循环总账（本模块新增）
- `delivery/retake-escalation.json` —— 升级人工证据（本模块新增）
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

__all__ = ["plan", "write", "run_retake", "run_retake_loop", "hint_from_diffs",
           "MAX_RETRIES", "MAX_HINT_CHARS"]

#: 边界自动补拍的重试上限（默认 2：最多两次「补拍-复审」）
MAX_RETRIES = 2
#: 自动生成 hint 的总长上限（中文按字符计）
MAX_HINT_CHARS = 400
#: VLM「一句话差异」进 hint 时的截断长度
JUDGE_CHARS = 80
#: 单条契约 diff 进 hint 时的截断长度
DIFF_CHARS = 60
#: 契约 diff 最多进 hint 几条（总量仍受 MAX_HINT_CHARS 约束）
MAX_CONTRACT_DIFFS = 6

#: 画面 diff -> 可执行补拍约束（与 boundary_audit.audit_visual 的标签对齐）
_VISUAL_HINTS: dict[str, str] = {
    "空间不一致": "沿用上一章同一空间/背景/陈设，不要换景",
    "人物造型不一致": "人物沿用同一服装/发型/妆造，不要改造型",
    "光线/时段不连续": "保持与上一章同一光线/时段/色温",
}

#: 关键词兜底（VLM/上游可能给出近义标签）
_VISUAL_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("空间", "场景", "背景"), "沿用上一章同一空间/背景/陈设，不要换景"),
    (("造型", "服装", "妆", "发型"), "人物沿用同一服装/发型/妆造，不要改造型"),
    (("光线", "时段", "色温"), "保持与上一章同一光线/时段/色温"),
)

#: 完全没有 diff 可依据时的通用 hint
GENERIC_HINT = ("边界衔接不连续：沿用上一章同一空间/背景、同一人物造型、"
                "同一光线/时段；只补拍接戏镜，不要换景/换造型/换光线")

#: 升级人工时给的 3 条建议（按升级原因选一组）
_ESCALATION_ACTIONS: dict[str, list[str]] = {
    "contract": [
        "改章卡/章契约：对齐上一章 exit 与本章 entry（地点/时间/人物/道具/悬念）",
        "改完重跑边界审（series_run --resume），确认契约转绿/黄",
        "若确认章卡正确、是上游剧本变化，人工评审后再改契约",
    ],
    "visual_no_judge": [
        "开启 --boundary-vlm（或确认边界帧存在）让 VLM 真复审",
        "人工并排对比上一章末帧与本章首帧：空间/造型/光线三项",
        "确认差异后手动只重拍边界镜，并在 delivery 记录豁免/修正原因",
    ],
    "retries_exhausted": [
        "人工对比每次尝试的 hint 与最后一版边界帧，定位持续不一致项",
        "修上游（章卡/角色设定/场景光线）后重跑边界补拍",
        "若差异可接受，人工放行并在 delivery 记录豁免原因",
    ],
    "no_shots": [
        "先跑一次普通渲染，生成 assets/shots/<shotId>/shot.json",
        "确认边界镜 shotId 以 SH- 开头且本章前 1~3 镜存在",
        "或显式传 retake_shots 指定要补拍的镜头",
    ],
    "render_failed": [
        "检查云端渲染失败原因（额度/网络/提示词超长）",
        "修好后手动重试边界镜并记录 retake-result",
        "把失败证据留在 delivery/retake-result.json 供人工判断",
    ],
}


# --------------------------------------------------------------------------- #
# 旧接口（2026-11-17，B2 计划层/执行层）——签名与证据文件保持不变
# --------------------------------------------------------------------------- #
def plan(boundary: Mapping[str, Any], workdir: str | Path,
         *, max_shots: int = 3) -> dict[str, Any]:
    """返回重拍计划：只包含边界镜（本章前 N 镜）。"""
    wd = Path(str(workdir))
    shots_dir = wd / "assets" / "shots"
    shots: list[str] = []
    if shots_dir.is_dir():
        for sub in sorted(shots_dir.iterdir()):
            if sub.is_dir() and sub.name.startswith("SH-"):
                shots.append(sub.name)
    boundary_shots = shots[:max(1, int(max_shots))]
    level = str(boundary.get("level") or "")
    out = {"kind": "boundary.retake", "level": level,
           "needRetake": level == "red",
           "scope": "boundary_shots_only",
           "retakeShots": boundary_shots,
           "maxRetries": MAX_RETRIES,
           "reason": ("边界审红：只重拍边界镜" if level == "red"
                      else "边界审非红，无需重拍"),
           "workdir": str(wd)}
    return out


def write(workdir: str | Path, retake: Mapping[str, Any]) -> Path:
    p = Path(str(workdir)) / "delivery" / "retake-plan.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(dict(retake), ensure_ascii=False, indent=2),
                 encoding="utf-8")
    return p


def run_retake(workdir: str | Path, shots: list[str], hint: str,
               *, renderer: Any = None) -> dict[str, Any]:
    """B2 执行层：给边界镜加 hint → 只重渲这些镜 → 更新 shot manifest。

    `renderer` 可注入（测试用）；默认走云端 `cloud_render.render_one`。
    """
    import json as _json
    from types import SimpleNamespace

    from little_crew.runtime import shot_manifest as _sm

    wd = Path(str(workdir))
    results: list[dict[str, Any]] = []
    for sid in shots:
        rec = _sm.load(wd, sid)
        if not rec:
            results.append({"shotId": sid, "ok": False,
                            "error": "没有 shot.json（先跑一次普通渲染）"})
            continue
        before = int(rec.get("version") or 0)
        new = _sm.bump_hint(wd, sid, hint)
        if renderer is None:
            from little_crew.runtime import cloud_render as _cr

            fake = SimpleNamespace(shot_id=sid,
                                   description=str(new.get("prompt") or ""),
                                   characters="")
            res = _cr.render_one(fake, str(new.get("anchor") or ""),
                                 wd / "assets" / "cloud_render",
                                 model=str(new.get("model") or _cr.DEFAULT_MODEL),
                                 extra="")
        else:
            res = renderer(sid, str(new.get("prompt") or ""))
        if not res.get("ok"):
            _sm.upsert(wd, shot_id=sid, prompt=str(new.get("prompt") or ""),
                       status="failed", meta={"error": str(res.get("error"))[:160]})
            results.append({"shotId": sid, "ok": False,
                            "error": str(res.get("error"))[:160]})
            continue
        upd = _sm.upsert(wd, shot_id=sid, prompt=str(new.get("prompt") or ""),
                         model=str(res.get("model") or new.get("model") or ""),
                         anchor=str(res.get("anchor") or new.get("anchor") or ""),
                         out=str(res.get("out") or ""), status="ok",
                         cost_cny=0.0 if res.get("reused") else 0.05,
                         meta={"retake": True, "hint": hint,
                               "reused": bool(res.get("reused"))})
        results.append({"shotId": sid, "ok": True,
                        "versionBefore": before, "versionAfter": upd.get("version"),
                        "out": upd.get("out"),
                        "reused": bool(res.get("reused")),
                        "costCny": 0.0 if res.get("reused") else 0.05})
    out = {"kind": "boundary.retake.result", "hint": hint,
           "shots": results, "ok": all(r.get("ok") for r in results) if results else False,
           "costCny": round(sum(float(r.get("costCny") or 0) for r in results), 4)}
    ev = wd / "delivery" / "retake-result.json"
    ev.parent.mkdir(parents=True, exist_ok=True)
    ev.write_text(_json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _norm(text: Any) -> str:
    """压空白；空值一律空串。"""
    return " ".join(str(text or "").split())


def _clip(text: Any, limit: int) -> str:
    """规范化 + 按字符截断（超出时补一个省略号，结果仍 <= limit）。"""
    s = _norm(text)
    if limit <= 0:
        return ""
    return s if len(s) <= limit else s[: max(1, limit - 1)] + "…"


def _visual_constraint(diff: Any) -> str:
    """把一条画面 diff 映射成可执行中文约束（未知标签也保留，不丢信息）。"""
    d = _norm(diff)
    if not d:
        return ""
    if d in _VISUAL_HINTS:
        return _VISUAL_HINTS[d]
    for label, hint in _VISUAL_HINTS.items():
        if label in d:
            return hint
    for keys, hint in _VISUAL_KEYWORDS:
        if any(k in d for k in keys):
            return hint
    return "针对画面问题补齐：" + _clip(d, DIFF_CHARS)


def hint_from_diffs(boundary: Mapping[str, Any]) -> str:
    """从边界审记录自动生成**针对性中文补拍 hint**。

    输入形如::

        {"level": "red",
         "contract": {"level": "red", "diffs": ["placeKey: ..."]},
         "visual": {"level": "red", "diffs": ["空间不一致"],
                    "judge": "一句话差异"}}

    规则（确定性、去重、总长 <= MAX_HINT_CHARS）：
    - 视觉 diffs -> 可执行约束（空间/造型/光线三类，未知标签保留）
    - `visual.judge` 一句话差异 -> 「上一版被判：…」（截断 <= JUDGE_CHARS）
    - 契约 diffs -> 逐条「契约要求：…」（截断，最多 MAX_CONTRACT_DIFFS 条）
    - 自动循环把新 diff 并回 `visual.stillRedDiffs` 后 -> 「上一版仍被判：…」
    - 完全没有可依据信息 -> 通用 hint
    """
    b: Mapping[str, Any] = boundary if isinstance(boundary, Mapping) else {}
    visual = b.get("visual") if isinstance(b.get("visual"), Mapping) else {}
    contract = b.get("contract") if isinstance(b.get("contract"), Mapping) else {}
    parts: list[str] = []

    def push(text: Any) -> None:
        s = _norm(text)
        if s and s not in parts:      # 去重：同一句只拼接一次
            parts.append(s)

    raw_visual = [_norm(d) for d in (visual.get("diffs") or ()) if _norm(d)]
    if not raw_visual:                # 兜底：边界记录把 diffs 放顶层
        raw_visual = [_norm(d) for d in (b.get("diffs") or ()) if _norm(d)]
    for d in raw_visual:
        push(_visual_constraint(d))

    still = [_norm(x) for x in (visual.get("stillRedDiffs") or ()) if _norm(x)]
    if still:
        push("上一版仍被判：" + _clip("、".join(still[:3]), JUDGE_CHARS)
             + "（这次必须严格按约束重拍）")

    sentence = _norm(visual.get("judge") or b.get("judge") or "")
    if sentence:
        if sentence.startswith("上一版"):
            push(_clip(sentence, JUDGE_CHARS))
        else:
            push("上一版被判：" + _clip(sentence, JUDGE_CHARS))

    for d in list(contract.get("diffs") or ())[:MAX_CONTRACT_DIFFS]:
        push("契约要求：" + _clip(d, DIFF_CHARS))

    text = "；".join(parts)
    if not text:
        return GENERIC_HINT
    return _clip(text, MAX_HINT_CHARS)


def _level_of(doc: Any) -> str:
    """取 doc.level（只认 green/yellow/red）；非合法值返回空串。"""
    if not isinstance(doc, Mapping):
        return ""
    lv = str(doc.get("level") or "").strip().lower()
    return lv if lv in ("green", "yellow", "red") else ""


def _diffs_of(doc: Any) -> list[str]:
    """收集 doc 里的 diff 文本（契约线 + 画面线 + 顶层兜底），去重保序。"""
    out: list[str] = []
    if not isinstance(doc, Mapping):
        return out
    for section in ("contract", "visual"):
        sec = doc.get(section)
        if isinstance(sec, Mapping):
            for d in sec.get("diffs") or ():
                s = _norm(d)
                if s and s not in out:
                    out.append(s)
    for d in doc.get("diffs") or ():
        s = _norm(d)
        if s and s not in out:
            out.append(s)
    return out


def _judge_sentence(doc: Any) -> str:
    """从复审记录里取 VLM 一句话差异。"""
    if not isinstance(doc, Mapping):
        return ""
    for section in ("visual", "contract"):
        sec = doc.get(section)
        if isinstance(sec, Mapping) and sec.get("judge"):
            return _clip(sec.get("judge"), JUDGE_CHARS)
    return _clip(doc.get("judge") or "", JUDGE_CHARS)


def _boundary_diffs(boundary: Any) -> list[str]:
    """边界记录里的全部 diff（用于证据 lastDiffs）。"""
    b = dict(boundary) if isinstance(boundary, Mapping) else {}
    return _diffs_of(b)[:12]


def _merge_red(boundary: Mapping[str, Any], verdict: Any,
               attempt: int) -> dict[str, Any]:
    """把复审判官返回的红项合并进 boundary，供下一次 hint 用（更强）。"""
    b: dict[str, Any] = dict(boundary) if isinstance(boundary, Mapping) else {}
    visual = dict(b.get("visual") or {}) if isinstance(b.get("visual"), Mapping) else {}
    old: list[str] = []
    for d in visual.get("diffs") or ():
        s = _norm(d)
        if s and s not in old:
            old.append(s)

    red_v = (verdict.get("visual")
             if isinstance(verdict, Mapping) and isinstance(verdict.get("visual"), Mapping)
             else {})
    incoming: list[Any] = list(red_v.get("diffs") or ())
    if isinstance(verdict, Mapping):
        incoming += list(verdict.get("diffs") or ())
    new: list[str] = []
    for d in incoming:
        s = _norm(d)
        if s and s not in new:
            new.append(s)
    for d in new:
        if d not in old:
            old.append(d)

    visual["diffs"] = old
    visual["level"] = "red"
    visual["retryCount"] = int(attempt)
    if new:
        visual["stillRedDiffs"] = new
    else:
        fallback = _norm(red_v.get("judge")) or _norm(
            verdict.get("judge") if isinstance(verdict, Mapping) else "")
        visual["stillRedDiffs"] = [fallback or f"第{attempt}次补拍后画面仍不连续"]
    sentence = _norm(red_v.get("judge")) or _norm(
        verdict.get("judge") if isinstance(verdict, Mapping) else "")
    if sentence:
        visual["judge"] = _clip(sentence, 200)
    b["visual"] = visual
    b["level"] = "red"

    # 契约红本应在循环外升级；若判官仍带回契约 diff，并回 boundary 让 hint 再提醒
    red_c = verdict.get("contract") if isinstance(verdict, Mapping) else None
    if isinstance(red_c, Mapping):
        contract = dict(b.get("contract") or {}) if isinstance(b.get("contract"), Mapping) else {}
        merged: list[str] = []
        for d in list(contract.get("diffs") or ()) + list(red_c.get("diffs") or ()):
            s = _norm(d)
            if s and s not in merged:
                merged.append(s)
        if merged:
            contract["diffs"] = merged
        if str(red_c.get("level") or "").strip().lower() == "red":
            contract["level"] = "red"
        b["contract"] = contract
    return b


def _suggestions(kind: str) -> list[str]:
    """升级建议：固定 3 条（按原因选组，未知原因退回「重试耗尽」组）。"""
    return list(_ESCALATION_ACTIONS.get(kind) or _ESCALATION_ACTIONS["retries_exhausted"])


def _write_json(path: Path, doc: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(doc), ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# B2 自动循环（本模块核心新增）
# --------------------------------------------------------------------------- #
def run_retake_loop(workdir: str | Path, boundary: Mapping[str, Any], *,
                    retake_shots: list[str] | None = None,
                    renderer: Any = None, judge: Any = None,
                    max_retries: int = MAX_RETRIES,
                    episode_id: str = "",
                    series_id: str = "") -> dict[str, Any]:
    """B2 自动循环：边界红 -> 生成 hint -> 只重拍边界镜 -> 复审（最多 max_retries 次）。

    参数
    ----
    boundary : 边界审合并记录（`boundary_audit.audit_chapter` 的产物，或至少含
               `level` / `contract.diffs` / `visual.diffs` / `visual.judge`）。
    retake_shots : 要补拍的边界镜 id 列表；缺省用 `plan(boundary, workdir)["retakeShots"]`。
    renderer : 注入 `run_retake(..., renderer=...)`（测试用；默认云端）。
    judge : 复审判官 `judge(ctx) -> 边界级记录或 {"level": ...}`；ctx 含
            attempt/hint/shots/result/workdir。**judge=None 不假装复审，直接升级人工。**
    max_retries : 严格重试上限（默认 2）。
    episode_id / series_id : 证据留痕用。

    返回
    ----
    dict（同时落盘 `delivery/retake-loop.json`；升级时另写
    `delivery/retake-escalation.json`）：kind/level/ok/needRetake/escalated/needsHuman/
    attempts/history/costCny/maxRetries/episodeId/seriesId/reason/retakeShots/shots/
    lastHint/lastDiffs/workdir。
    """
    wd = Path(str(workdir))
    b: dict[str, Any] = dict(boundary) if isinstance(boundary, Mapping) else {}
    level0 = _level_of(b)
    raw_level = str(b.get("level") or "").strip().lower()
    requested = level0 == "red" or raw_level == "red"
    try:
        mr = max(0, int(max_retries))
    except (TypeError, ValueError):
        mr = MAX_RETRIES
    contract = b.get("contract") if isinstance(b.get("contract"), Mapping) else {}
    contract_red = str(contract.get("level") or "").strip().lower() == "red"

    if retake_shots is None:
        shots = [str(x) for x in (plan(b, wd).get("retakeShots") or ())]
    else:
        shots = [str(x) for x in (retake_shots or ())]

    history: list[dict[str, Any]] = []
    attempts = 0
    cost = 0.0
    escalated = False
    reason = ""
    final_level = level0 or raw_level or "green"
    last_hint = ""
    last_result: dict[str, Any] = {}
    boundary_cur: dict[str, Any] = b
    action_kind = "retries_exhausted"

    if not requested:
        reason = "边界审非红，无需补拍"
    elif contract_red:
        # 契约红是章卡/章契约层面的硬矛盾：补拍图片修不了契约，不烧钱
        escalated = True
        action_kind = "contract"
        _cd = _boundary_diffs(b)
        reason = "契约红项需人工改章卡/章契约：补拍图片修不了契约"
        if _cd:
            reason += f"（diffs：{_clip('、'.join(_cd[:4]), 160)}）"
    elif mr <= 0:
        escalated = True
        reason = f"重试上限为 0（max_retries={mr}）：不自动补拍，人工确认边界连续性"
    elif judge is None:
        # 诚实边界：没有复审判官 = 无法验证补拍是否修好，不假装复审
        escalated = True
        action_kind = "visual_no_judge"
        reason = "无复审判官（未开 VLM/无帧）：补拍后无法自动复审，人工确认边界连续性"
    elif not shots:
        escalated = True
        action_kind = "no_shots"
        reason = ("没有边界镜可补拍（assets/shots 下无 shot.json）："
                  "先跑一次普通渲染或显式指定 retake_shots，人工确认")
    else:
        for attempt in range(1, mr + 1):
            hint = hint_from_diffs(boundary_cur)
            last_hint = hint
            res = run_retake(wd, list(shots), hint, renderer=renderer)
            last_result = res
            attempts = attempt
            cost = round(cost + float(res.get("costCny") or 0), 4)
            if not res.get("ok"):
                err = "；".join(str(x.get("error") or "")
                               for x in (res.get("shots") or ())
                               if not x.get("ok"))[:120]
                history.append({"attempt": attempt, "hint": hint,
                                "shots": list(shots), "retakeOk": False,
                                "costCny": float(res.get("costCny") or 0),
                                "level": "red", "diffs": [], "judge": ""})
                escalated = True
                action_kind = "render_failed"
                final_level = "red"
                reason = (f"边界镜补拍失败（未出新图）："
                          f"{err or 'renderer 返回 ok=false'}；人工确认")
                break
            try:
                verdict = judge({"attempt": attempt, "hint": hint,
                                 "shots": list(shots), "result": res,
                                 "workdir": str(wd)})
            except Exception as e:                            # noqa: BLE001
                history.append({"attempt": attempt, "hint": hint,
                                "shots": list(shots), "retakeOk": True,
                                "costCny": float(res.get("costCny") or 0),
                                "level": "red", "diffs": [], "judge": ""})
                escalated = True
                action_kind = "visual_no_judge"
                final_level = "red"
                reason = (f"复审判官异常：{type(e).__name__}: {str(e)[:100]}；"
                          "人工确认（不假装复审通过）")
                break
            nl = _level_of(verdict) or "yellow"   # 解析不了 -> 黄项转人工，不当绿
            vdiffs = _diffs_of(verdict)
            vjudge = _judge_sentence(verdict)
            history.append({"attempt": attempt, "hint": hint,
                            "shots": list(shots), "retakeOk": True,
                            "costCny": float(res.get("costCny") or 0),
                            "level": nl, "diffs": vdiffs, "judge": vjudge})
            final_level = nl
            if nl != "red":
                reason = f"第 {attempt} 次补拍后复审为 {nl}，停止重拍"
                break
            boundary_cur = _merge_red(boundary_cur, verdict, attempt)
            if attempt >= mr:
                escalated = True
                action_kind = "retries_exhausted"
                reason = (f"已自动补拍 {attempt} 次仍红：升级人工"
                          "（每轮 hint / 复审差异见 history / lastHint）")

    last_diffs = _boundary_diffs(boundary_cur)
    doc: dict[str, Any] = {
        "kind": "boundary.retake.loop",
        "level": final_level,
        "ok": final_level != "red",
        "needRetake": requested,
        "escalated": escalated,
        "needsHuman": escalated,
        "attempts": attempts,
        "history": history,
        "costCny": round(cost, 4),
        "maxRetries": mr,
        "episodeId": str(episode_id),
        "seriesId": str(series_id),
        "reason": reason,
        "retakeShots": list(shots),
        "shots": list(last_result.get("shots") or ()),
        "lastHint": last_hint,
        "lastDiffs": last_diffs,
        "workdir": str(wd),
    }
    _write_json(wd / "delivery" / "retake-loop.json", doc)
    if escalated:
        esc: dict[str, Any] = {
            "kind": "boundary.escalation",
            "needsHuman": True,
            "reason": reason,
            "level": final_level,
            "attempts": attempts,
            "costCny": round(cost, 4),
            "lastHint": last_hint,
            "lastDiffs": last_diffs,
            "history": history,
            "suggestedActions": _suggestions(action_kind),
            "maxRetries": mr,
            "episodeId": str(episode_id),
            "seriesId": str(series_id),
            "workdir": str(wd),
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
        _write_json(wd / "delivery" / "retake-escalation.json", esc)
    return doc
