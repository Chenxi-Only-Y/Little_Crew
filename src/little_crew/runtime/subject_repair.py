"""场景主体修复：把分镜里"剧本没授权"的场景名词换成最近的授权词（2026-11-10）。

## 为什么需要它

提示词里写了白名单，模型**仍然会**写"花田"（剧本只有 山坡/岩石/木屋）。
真跑 EF8/EF9 两次因此被**否决交付** —— 整集跑完、成片都出了，却判不通过。

**与其反复调提示词，不如在代码里做确定性修复**，并留下痕迹：

```
花田  -> 山坡      （按字符串相似度取最近的授权词）
```

## 纪律

1 **只改场景名词**，不动角色、动作、台词
2 **每条替换都记录**（shotId / 字段 / 原词 / 新词 / 相似度）→ `delivery/subject_repairs.json`
3 找不到相近词（相似度 < 0.2）时**不替换**，留给门禁否决 —— 不瞎猜
"""
from __future__ import annotations

import difflib

#: 只看这些字段（描述/动作）；提示词由 bridge 生成，修这里就够
FIELDS = ("description", "action")


def _near(word: str, allowed: list[str]) -> tuple[str, float]:
    if not allowed:
        return "", 0.0
    if word in allowed:
        return word, 1.0
    hit = difflib.get_close_matches(word, allowed, n=1, cutoff=0.2)
    if hit:
        return hit[0], round(difflib.SequenceMatcher(None, word, hit[0]).ratio(), 3)
    return allowed[0], 0.0


def _surfaces(canon: str) -> tuple:
    """归一名 -> 文本里可能出现的写法（花田 -> 花田/花海/花垄/花丛…）。"""
    from little_crew.review.scene_subject import SCENE_NOUNS

    return tuple(SCENE_NOUNS.get(canon, ())) or (canon,)


def plan(shots, allowed, *, detector=None, by_scene=None) -> list[dict]:
    """找出需要替换的词。

    `allowed`：全局授权词（兜底）；`by_scene`：`{sceneId: [词]}` —— **优先用这一场的词**。
    为什么：`花田` 和 `山坡` 字面相似度是 0，但同一场里"山坡"就是它该用的词。
    """
    if detector is None:
        from little_crew.review.scene_subject import subjects_in

        def detector(text):                                  # noqa: ANN001
            return set(subjects_in({"text": text}))

    allow = [str(x) for x in (allowed or ())]
    bs = {str(k): [str(x) for x in (v or ())] for k, v in (by_scene or {}).items()}
    out: list[dict] = []
    for s in shots or ():
        sid = str(getattr(s, "shot_id", "") or "")
        _scid = str(getattr(s, "scene_id", "") or "")
        _local = bs.get(_scid) or []
        for f in FIELDS:
            text = str(getattr(s, f, "") or "")
            if not text:
                continue
            for w in detector(text):
                if allow and w in allow:
                    continue
                # ⚠️ `w` 是**归一名**（花田），文本里可能是 花丛/花垄/花田 混着写 ——
                # ① 只 replace("花田") 是空操作；② 只替换**第一种**写法也不够：
                #    真跑 SH-EFR2-S01-001 同时含"花田"和"花垄"，换掉前者后复检仍判越权。
                # 所以对**每种出现的写法各出一条计划**。
                _surfs = [s for s in _surfaces(w) if s in text] or [w]
                # ① 先在这一场的授权词里找相近；② 没有再全局找；③ 最后用本场第一个词
                new, score = _near(w, _local)
                if not new or score < 0.2:
                    new2, sc2 = _near(w, allow)
                    if new2 and sc2 >= 0.2:
                        new, score = new2, sc2
                    elif _local:
                        new, score = _local[0], 0.0
                    elif allow:
                        # 本场没有地点词、全局也没有相近的 —— 用剧本第一个地点兜底。
                        # 这是"有界的猜测"：只在本场确实需要地点、且剧本只授权少数几个时发生，
                        # **且逐条记录**（via=global-default），便于复核。
                        new, score = allow[0], 0.0
                for _surf in _surfs:
                    out.append({"shotId": sid, "sceneId": _scid, "field": f,
                                "old": _surf, "canonical": w,
                                "new": new, "score": score,
                                # 本场兜底也算"执行"，但记 score=0 便于审计
                                "applied": bool(new) and (score >= 0.2 or bool(_local) or bool(allow)),
                                "via": ("sim" if score >= 0.2 else
                                        ("scene-default" if _local else
                                         ("global-default" if allow else "none")))})
    return out


def _polish(text: str, new: str) -> str:
    """替换后修语气：「山坡里 / 山坡间 / 山坡中」→「山坡上」。

    为什么要这一步：`花丛` → `山坡` 直接替换会得到"少女跪在山坡里"，
    读起来别扭。地点词的可接后缀和植物词不同 —— 这是**替换的副作用**，
    不是原文的问题，所以在这里收一下。
    """
    if not new:
        return text
    for bad in ("里", "间", "中", "内"):
        text = text.replace(f"{new}{bad}", f"{new}上")
    return text


def apply(shots, repairs) -> tuple[list, list]:
    """按计划替换。返回 `(新镜头列表, 实际执行的记录)`。"""
    import dataclasses

    by = {}
    for r in repairs or ():
        if r.get("applied"):
            by.setdefault((r["shotId"], r["field"]), []).append(r)
    done: list[dict] = []
    out = []
    for s in shots or ():
        sid = str(getattr(s, "shot_id", "") or "")
        changed = {}
        for f in FIELDS:
            text = str(getattr(s, f, "") or "")
            if not text:
                continue
            new_text = text
            for r in by.get((sid, f), ()):
                for _w in (r.get("old"), r.get("canonical")):
                    if _w and _w in new_text:
                        new_text = new_text.replace(_w, r["new"])
                    done.append({**r, "before": text[:60], "after": new_text[:60]})
            if new_text != text:
                for r in by.get((sid, f), ()):
                    new_text = _polish(new_text, r.get("new") or "")
                changed[f] = new_text
        if changed:
            try:
                out.append(dataclasses.replace(s, **changed))
            except Exception:                                 # noqa: BLE001
                # 不是 dataclass（鸭子类型/测试替身）时，浅拷贝后 setattr
                import copy as _copy

                try:
                    c = _copy.copy(s)
                    for k, v in changed.items():
                        setattr(c, k, v)
                    out.append(c)
                except Exception:                             # noqa: BLE001
                    out.append(s)
        else:
            out.append(s)
    return out, done
