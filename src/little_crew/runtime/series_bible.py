"""系列资产台账（2026-11-17）：跨集场景/角色/档位的唯一事实来源。

## 为什么需要

单集能跑，但长篇短剧是"多集 + 复用 + 更新 + 升档"：

- 同一地点在第 1 集叫"站台"、第 8 集叫"空站台" → 必须有**稳定 sceneKey**
- 同一场景第 1 集 Tier1、第 5 集成题眼 → 需要**档位历史与升档**
- 角色跨集出现、换装 → 需要**角色 bible 与造型版本**
- 已经生成过的资产 → **复用（0 成本）**，而不是每集重生成

## 存储

```
series/
  series.json     # 本文件：剧集清单 / scenes / cast / usage / budget
  overrides.json  # 人工干预（可选）
```

## 纪律

- **稳定 key 优先用剧本地点**（标题第一段去时段），别名表可配
- 场景卡内容哈希变化 → 新版本；版本只增不减
- 档位 history 记录每次决策来源（llm/manual/heuristic/series）
- 复用判定：同 key + 同卡哈希 + 现有档位 ≥ 目标档位 → 可复用
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import random
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    "SCHEMA", "load", "save", "stable_scene_key", "card_hash",
    "episodes_of", "upsert_episode", "upsert_scene", "upsert_character",
    "scene_entry", "character_entry", "decide_reuse", "record_usage",
    "summary", "find_scene",
    # #8 并行写台账锁（2026-11-18）
    "LedgerLockTimeout", "LOCK_FILE", "LOCK_TIMEOUT_S", "LOCK_STALE_S",
    "locked_merge", "merge_episode",
]

SCHEMA = "little_crew.series/v1"
# ---- 跨进程写锁（#8）：纯 stdlib，O_CREAT|O_EXCL 锁文件，Windows 可用 ----
LOCK_FILE = "series.json.lock"
LOCK_TIMEOUT_S = 30.0     # 默认等锁上限，超时抛 LedgerLockTimeout
LOCK_STALE_S = 300.0      # 锁文件 mtime 超过 300s 视为陈旧锁，可破除


class LedgerLockTimeout(RuntimeError):
    """等系列台账写锁超时（或锁文件无法创建）。"""


_LOCK_POLL_MIN = 0.01     # 重试抖动下限（秒）
_LOCK_POLL_MAX = 0.05     # 重试抖动上限（秒）
_TIME_WORDS = {"夜", "日", "晨", "黄昏", "清晨", "深夜", "午后", "傍晚",
               "凌晨", "雨", "白天", "晚上"}


def _loc_norm(text: Any) -> str:
    """地点归一：取标题第一段（去时段），去掉前后空白/标点。"""
    s = str(text or "").strip()
    if not s:
        return ""
    parts = [p.strip() for p in re.split(r"[·•|/]", s) if p.strip()]
    for p in parts:
        if p not in _TIME_WORDS:
            return re.sub(r"[（）()【】\[\]\s]", "", p)
    return parts[0] if parts else ""


def stable_scene_key(heading: Any, *, aliases: Mapping[str, str] | None = None) -> str:
    """剧本标题/场景卡 → 稳定 sceneKey。别名可把"空站台/站台"归到同一个。"""
    loc = _loc_norm(heading)
    if aliases:
        for k, v in aliases.items():
            _k = _loc_norm(k)
            if _k and (loc == _k or loc.startswith(_k) or loc.endswith(_k)
                       or _k in loc):
                return str(v)
    return loc.lower()


def card_hash(card: Mapping[str, Any] | None) -> str:
    try:
        from little_crew.ir.scene_card import canonical_scene_card

        canon = canonical_scene_card(dict(card or {}))
    except Exception:                                        # noqa: BLE001
        canon = dict(card or {})
    blob = json.dumps(canon, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def load(base: str | Path) -> dict[str, Any]:
    """读 series.json；不存在返回空台账。"""
    p = Path(str(base)) / "series.json"
    if not p.is_file():
        return {"schema": SCHEMA, "seriesId": Path(str(base)).name,
                "episodes": [], "scenes": {}, "cast": {},
                "usage": [], "createdAt": ""}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:                                        # noqa: BLE001
        return {"schema": SCHEMA, "episodes": [], "scenes": {}, "cast": {},
                "usage": []}


def _atomic_write(path: str | Path, text: str) -> Path:
    """原子写：同目录写临时文件 → os.replace 覆盖目标 → 清理临时文件。

    并发读者永远看到"旧完整版"或"新完整版"，不会读到写了一半的 JSON。
    """
    p = Path(str(path))
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp",
                                    dir=str(p.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(str(tmp), str(p))
    finally:
        # 成功时 tmp 已被 replace 掉；失败时（异常/被 replace 前）清理残留
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    return p


def save(base: str | Path, data: Mapping[str, Any]) -> Path:
    p = Path(str(base)) / "series.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    # #8：整份覆盖也从"直接 write_text"改为原子写（行为/路径/返回不变）
    _atomic_write(p, json.dumps(dict(data), ensure_ascii=False, indent=2))
    return p


def _lock_path(base: str | Path) -> Path:
    return Path(str(base)) / LOCK_FILE


def _unlink_quiet(path: Path, attempts: int = 3) -> bool:
    """删文件（少量重试）：Windows 上杀毒/索引器偶发占用会瞬时 EACCES。

    返回 True = 文件已不存在（删成功或本来就没有）；False = 仍删不掉。
    """
    for i in range(max(1, attempts)):
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if i + 1 < attempts:
                time.sleep(0.005)
    return False


@contextlib.contextmanager
def _lock(base: str | Path, timeout_s: float = LOCK_TIMEOUT_S):
    """跨进程互斥锁：O_CREAT|O_EXCL 建锁文件，失败 → 0.01~0.05s 抖动重试。

    - 超时抛 ``LedgerLockTimeout``（带锁路径/占用时长/释放提示）
    - 锁文件 mtime 超过 ``LOCK_STALE_S`` → 破除并继续抢锁
    - ``finally`` 必删锁文件：mutate 抛异常也释放
    yield 一个信息字典：``{"waitMs": 等锁毫秒, "staleBroke": 是否破过陈旧锁}``
    """
    base_p = Path(str(base))
    base_p.mkdir(parents=True, exist_ok=True)
    lp = base_p / LOCK_FILE
    t0 = time.monotonic()
    try:
        budget = max(0.0, float(timeout_s))
    except (TypeError, ValueError):
        budget = LOCK_TIMEOUT_S
    deadline = t0 + budget
    info: dict[str, Any] = {"waitMs": 0, "staleBroke": False}
    token = f"pid={os.getpid()} t={time.time():.6f}\n".encode("ascii", "replace")
    fd: int | None = None
    while True:
        try:
            fd = os.open(str(lp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            pass
        except PermissionError:
            # Windows 坑：与"删锁文件"竞争时 CreateFile(CREATE_NEW) 可能返回
            # EACCES（ERROR_ACCESS_DENIED）而不是 EEXIST —— 不是权限配置错误，
            # 按"锁被占/正在释放"处理，继续重试（超时仍会抛 LedgerLockTimeout）。
            pass
        except OSError as e:                              # noqa: BLE001
            raise LedgerLockTimeout(
                f"系列台账锁创建失败：{lp}（{type(e).__name__}: {e}）") from e
        else:
            try:
                os.write(fd, token)
            except OSError:
                pass
            break
        # ---- 锁已被占：先判陈旧，再看超时 ----
        try:
            age = time.time() - lp.stat().st_mtime
        except FileNotFoundError:
            continue       # 对方刚好释放，立刻重试
        except OSError:
            age = -1.0
        if age > LOCK_STALE_S:
            if _unlink_quiet(lp):
                info["staleBroke"] = True
                continue
            # 删不掉（疑似仍被活进程占用）：按"被占"处理（走超时逻辑）
        if time.monotonic() >= deadline:
            info["waitMs"] = int((time.monotonic() - t0) * 1000)
            raise LedgerLockTimeout(
                f"系列台账忙：{lp} 已被占用 {age:.1f}s，等锁 {budget:.1f}s 超时"
                f"（正常释放会自动删锁；进程已死可手删锁文件，"
                f"或等 mtime 超过 {LOCK_STALE_S:.0f}s 自动破除）")
        time.sleep(random.uniform(_LOCK_POLL_MIN, _LOCK_POLL_MAX))
    info["waitMs"] = int((time.monotonic() - t0) * 1000)
    try:
        yield info
    finally:
        try:
            if fd is not None:
                os.close(fd)
        except OSError:
            pass
        _unlink_quiet(lp)


def locked_merge(base: str | Path, mutate: Callable[[dict[str, Any]], Any],
                 *, timeout_s: float = LOCK_TIMEOUT_S) -> dict[str, Any]:
    """锁内"读最新 → mutate → 原子写回"，返回合并结果。

    ``mutate(data)`` 可原地改 dict，也可返回一个新 dict（返回 Mapping 时以它为准）。
    mutate/写盘抛异常时：锁一定释放、文件保持旧完整内容（原子写保证）。
    """
    with _lock(base, timeout_s=timeout_s) as info:
        data = load(base)
        out = mutate(data)
        if isinstance(out, Mapping):
            data = dict(out)
        save(base, data)
    return {"data": data, "locked": True, "waitMs": info["waitMs"],
            "staleBroke": info["staleBroke"], "summary": summary(data)}


def episodes_of(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(x) for x in (data.get("episodes") or ()) if isinstance(x, Mapping)]


def upsert_episode(data: dict[str, Any], *, episode_id: str, title: str = "",
                   scenes: Sequence[str] = (), shots: int = 0,
                   status: str = "done") -> dict[str, Any]:
    for ep in data.setdefault("episodes", []):
        if isinstance(ep, dict) and ep.get("episodeId") == episode_id:
            ep.update({"title": title or ep.get("title", ""),
                       "scenes": sorted(set(scenes) or ep.get("scenes") or []),
                       "shots": shots or ep.get("shots", 0),
                       "status": status})
            return ep
    ep = {"episodeId": episode_id, "title": title,
          "scenes": sorted(set(scenes)), "shots": int(shots), "status": status}
    data["episodes"].append(ep)
    return ep


def scene_entry(data: Mapping[str, Any], scene_key: str) -> dict[str, Any] | None:
    scenes = data.get("scenes") or {}
    got = scenes.get(scene_key) if isinstance(scenes, Mapping) else None
    return dict(got) if isinstance(got, Mapping) else None


def find_scene(data: Mapping[str, Any], scene_key: str) -> dict[str, Any] | None:
    return scene_entry(data, scene_key)


def upsert_scene(data: dict[str, Any], *, scene_key: str, heading: str = "",
                 card: Mapping[str, Any] | None = None, tier: int | None = None,
                 episode_id: str = "", assets: Mapping[str, Any] | None = None,
                 decision_source: str = "", reason: str = "") -> dict[str, Any]:
    """登记/更新场景；卡哈希变化 → 版本 +1；档位只升不降（除非 force）。"""
    scenes = data.setdefault("scenes", {})
    prev = scenes.get(scene_key) if isinstance(scenes, dict) else None
    ch = card_hash(card) if card else ""
    try:
        from little_crew.ir.scene_card import canonical_scene_card as _csc

        _card_canon = _csc(dict(card or {})) if card else {}
    except Exception:                                        # noqa: BLE001
        _card_canon = dict(card or {})
    now = {"sceneKey": scene_key, "heading": heading,
           "cardHash": ch, "card": _card_canon, "updatedIn": episode_id,
           "lastDecisionSource": decision_source,
           "lastReason": str(reason)[:200]}
    if not isinstance(prev, dict):
        now.update({"version": 1, "tier": int(tier or 1), "episodes": [episode_id] if episode_id else [],
                    "usage": 0, "assets": dict(assets or {}), "tierHistory": []})
        if tier:
            now["tierHistory"].append({"episodeId": episode_id, "tier": int(tier),
                                       "source": decision_source})
        scenes[scene_key] = now
        return now
    # 已存在：版本/档位/使用
    prev_update = dict(prev)
    if ch and ch != str(prev.get("cardHash") or ""):
        prev_update["version"] = int(prev.get("version") or 1) + 1
        prev_update["cardHash"] = ch
    prev_update["heading"] = heading or prev.get("heading", "")
    if _card_canon:
        prev_update["card"] = _card_canon
    prev_update["updatedIn"] = episode_id or prev.get("updatedIn", "")
    prev_update["lastDecisionSource"] = decision_source or prev.get("lastDecisionSource", "")
    prev_update["lastReason"] = str(reason)[:200] if reason else prev.get("lastReason", "")
    if episode_id and episode_id not in (prev_update.get("episodes") or []):
        prev_update["episodes"] = list(prev_update.get("episodes") or []) + [episode_id]
    if tier is not None:
        old_tier = int(prev_update.get("tier") or 1)
        new_tier = max(old_tier, int(tier))     # 档位只升不降（人工可另外改）
        if new_tier != old_tier:
            prev_update["tier"] = new_tier
            prev_update.setdefault("tierHistory", []).append(
                {"episodeId": episode_id, "tier": new_tier,
                 "source": decision_source or "series"})
        else:
            prev_update["tier"] = old_tier
    if assets:
        # 按视图合并：新产物覆盖同名视图，但**不丢旧视图**（9 视图复用缺 2 张的根因）
        prev_update["assets"] = {**dict(prev_update.get("assets") or {}),
                                 **dict(assets)}
    scenes[scene_key] = prev_update
    return prev_update


def character_entry(data: Mapping[str, Any], character_id: str) -> dict[str, Any] | None:
    got = (data.get("cast") or {}).get(character_id) if isinstance(data.get("cast"), Mapping) else None
    return dict(got) if isinstance(got, Mapping) else None


def upsert_character(data: dict[str, Any], *, character_id: str, name: str = "",
                     appearance: str = "", outfit: str = "",
                     episode_id: str = "", anchor: str = "") -> dict[str, Any]:
    """登记/更新角色；外观变化 → 版本 +1；造型单独记版本集合。"""
    cast = data.setdefault("cast", {})
    prev = cast.get(character_id) if isinstance(cast, dict) else None
    app_hash = hashlib.sha256(str(appearance or "").encode("utf-8")).hexdigest()[:10]
    if not isinstance(prev, dict):
        cast[character_id] = {"characterId": character_id, "name": name or character_id,
                              "appearance": appearance, "appearanceHash": app_hash,
                              "version": 1, "outfits": [outfit] if outfit else [],
                              "episodes": [episode_id] if episode_id else [],
                              "anchor": anchor}
        return cast[character_id]
    upd = dict(prev)
    if app_hash != str(prev.get("appearanceHash") or ""):
        upd["version"] = int(prev.get("version") or 1) + 1
        upd["appearanceHash"] = app_hash
        upd["appearance"] = appearance
    upd["name"] = name or prev.get("name", character_id)
    if outfit and outfit not in (upd.get("outfits") or []):
        upd["outfits"] = list(upd.get("outfits") or []) + [outfit]
    if episode_id and episode_id not in (upd.get("episodes") or []):
        upd["episodes"] = list(upd.get("episodes") or []) + [episode_id]
    if anchor:
        upd["anchor"] = anchor
    cast[character_id] = upd
    return upd


def decide_reuse(data: Mapping[str, Any], *, scene_key: str,
                 card: Mapping[str, Any] | None, desired_tier: int) -> dict[str, Any]:
    """复用判定：同 key + 同卡哈希 + 现有档位 ≥ 目标 → 复用；否则重生成/升档。"""
    got = scene_entry(data, scene_key)
    if not got:
        return {"reuse": False, "why": "台账无此场景（首次）", "version": 0}
    ch = card_hash(card) if card else ""
    if ch and ch != str(got.get("cardHash") or ""):
        return {"reuse": False, "why": f"场景卡内容变化（v{got.get('version', 1)} → 新版本）",
                "version": int(got.get("version") or 1)}
    have = int(got.get("tier") or 1)
    want = int(desired_tier or 1)
    if have >= want:
        return {"reuse": True, "why": f"已有 Tier{have} ≥ 目标 Tier{want}",
                "version": int(got.get("version") or 1),
                "assets": dict(got.get("assets") or {})}
    return {"reuse": False, "upgrade": True,
            "why": f"档位升级 Tier{have} → Tier{want}",
            "version": int(got.get("version") or 1)}


def record_usage(data: dict[str, Any], *, episode_id: str, scene_key: str,
                 tier: int, action: str, source: str = "",
                 assets: Mapping[str, Any] | None = None) -> dict[str, Any]:
    entry = {"episodeId": episode_id, "sceneKey": scene_key, "tier": int(tier),
             "action": action, "source": source, "assets": dict(assets or {})}
    data.setdefault("usage", []).append(entry)
    sc = (data.get("scenes") or {}).get(scene_key)
    if isinstance(sc, dict):
        sc["usage"] = int(sc.get("usage") or 0) + 1
    return entry


def summary(data: Mapping[str, Any]) -> dict[str, Any]:
    scenes = data.get("scenes") or {}
    cast = data.get("cast") or {}
    tiers = {}
    for v in scenes.values():
        if isinstance(v, Mapping):
            tiers[int(v.get("tier") or 1)] = tiers.get(int(v.get("tier") or 1), 0) + 1
    return {"seriesId": data.get("seriesId"), "episodes": len(data.get("episodes") or ()),
            "scenes": len(scenes), "cast": len(cast), "tiers": tiers,
            "usage": len(data.get("usage") or ())}


# ---------------------------------------------------------------------------
# #8（2026-11-18）：按集写 + 合并锁
#
# 背景：并行跑多集时，每个子进程各自 load 整份 series.json → 内存 upsert →
# save 整份覆盖 → 后写者把先写者的 scenes/cast/usage/episodes 覆盖掉。
# 这里把"整份覆盖"换成"锁内重读最新 + 只合并本集增量 + 原子写回"：
#   - scenes 走 upsert_scene（tier 只升不降 / cardHash 变才升 version / assets 合并）
#   - usage 按 (episodeId, sceneKey, action) 去重（同集重复合并不重复记）
#   - cast 走 upsert_character（episodes/outfits 去重）
#   - episodes 走 upsert_episode（同集 update，不重复）
# 幂等：同一集用同一份增量重复 merge 不会虚增版本/usage。
# ---------------------------------------------------------------------------
def _scene_usage_rows(scenes: Sequence[Mapping[str, Any]],
                      usage: Sequence[Mapping[str, Any]]):
    """合并 usage 增量：scenes 里带 action 的先派生，显式 usage 再补；同动作去重。"""
    rows: list[dict[str, Any]] = []
    for item in scenes or ():
        if not isinstance(item, Mapping):
            continue
        act = str(item.get("action") or "")
        if not act:
            continue
        rows.append({"sceneKey": str(item.get("sceneKey") or ""),
                     "tier": int(item.get("tier") or 1), "action": act,
                     "source": str(item.get("decisionSource") or ""),
                     "assets": item.get("assets") or {}})
    for item in usage or ():
        if isinstance(item, Mapping):
            rows.append(dict(item))
    return rows


def merge_episode(base: str | Path, *, episode_id: str, title: str = "",
                  scenes: Sequence[Mapping[str, Any]] = (),
                  usage: Sequence[Mapping[str, Any]] = (),
                  cast: Sequence[Mapping[str, Any]] = (),
                  shots: int = 0, timeout_s: float = LOCK_TIMEOUT_S,
                  episode_scene_ids: Sequence[str] | None = None) -> dict[str, Any]:
    """锁内合并**本集增量**，幂等、不丢他集数据。返回合并摘要。

    episodes[].scenes 默认按传入 scenes 的 sceneKey 生成；剧集里若要沿用
    场景 id（如 ``SC-E01-001``），调用方用 ``episode_scene_ids`` 显式给。
    """
    added = {"scenes": 0, "usage": 0, "cast": 0}

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        before_scenes = data.get("scenes") if isinstance(data.get("scenes"), dict) else {}
        keys_before = set(before_scenes)
        for item in scenes or ():
            if not isinstance(item, Mapping):
                continue
            key = str(item.get("sceneKey") or "")
            if not key:
                continue
            upsert_scene(
                data, scene_key=key,
                heading=str(item.get("heading") or ""),
                card=item.get("card"),
                tier=(None if item.get("tier") is None else int(item.get("tier") or 1)),
                episode_id=episode_id,
                assets=item.get("assets") or None,
                decision_source=str(item.get("decisionSource") or ""),
                reason=str(item.get("reason") or ""))
        added["scenes"] = len(set(data.get("scenes") or {}) - keys_before)
        # ---- usage：按 (episodeId, sceneKey, action) 去重后追加 ----
        seen = set()
        for u in data.get("usage") or ():
            if isinstance(u, Mapping):
                seen.add((str(u.get("episodeId") or ""),
                          str(u.get("sceneKey") or ""),
                          str(u.get("action") or "")))
        for u in _scene_usage_rows(scenes, usage):
            sk = str(u.get("sceneKey") or "")
            act = str(u.get("action") or "")
            if not sk or not act:
                continue
            sig = (episode_id, sk, act)
            if sig in seen:
                continue
            seen.add(sig)
            record_usage(data, episode_id=episode_id, scene_key=sk,
                         tier=int(u.get("tier") or 1), action=act,
                         source=str(u.get("source") or ""),
                         assets=u.get("assets") or None)
            added["usage"] += 1
        # ---- cast：upsert_character（幂等） ----
        for item in cast or ():
            if not isinstance(item, Mapping):
                continue
            cid = str(item.get("characterId") or "")
            if not cid:
                continue
            upsert_character(
                data, character_id=cid,
                name=str(item.get("name") or ""),
                appearance=str(item.get("appearance") or ""),
                outfit=str(item.get("outfit") or ""),
                episode_id=episode_id,
                anchor=str(item.get("anchor") or ""))
        # ---- episode：upsert_episode（同集 update，不重复） ----
        ep_scenes: list[str]
        if episode_scene_ids is not None:
            ep_scenes = [str(x) for x in episode_scene_ids]
        else:
            ep_scenes = [str(it.get("sceneKey") or "") for it in (scenes or ())
                         if isinstance(it, Mapping) and it.get("sceneKey")]
        upsert_episode(data, episode_id=episode_id, title=title,
                       scenes=ep_scenes, shots=shots)
        return data

    with _lock(base, timeout_s=timeout_s) as info:
        data = load(base)
        cast_before = len(data.get("cast") or {})
        out = _mutate(data)
        if isinstance(out, Mapping):
            data = dict(out)
        save(base, data)
        added["cast"] = max(0, len(data.get("cast") or {}) - cast_before)
    return {"ok": True, "episodeId": episode_id, "added": added,
            "summary": summary(data), "waitMs": info["waitMs"],
            "staleBroke": info["staleBroke"]}
