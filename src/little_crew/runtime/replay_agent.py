"""重放代理：把上次的产物直接交回去，**不调模型**（2026-11-08）。

用于 `--from` 续跑：主流程原样跑，昂贵的 LLM 阶段（编剧/分镜）被"回放"。

## 为什么包一层而不是跳过

`run()` 里各阶段共享局部变量，跳过一段等于改主流程；
而"包一层"只替换**实现**，契约（`__call__(envelope, runtime)` 与 `CallResult`）不变。

## 纪律

- **命中就命中，未命中就退真实现** —— 两种都记录，绝不假装命中
- 重放的是**上一次的真实产物**（含 evidence），不是"重新编一个像样的"
"""
from __future__ import annotations


class ReplayAgent:
    """`inner`：真实现（未命中时用）；`records`：上次的产物（按调用顺序）。"""

    def __init__(self, inner, records, *, agent_id: str = "",
                 payload_key: str = "") -> None:
        self.inner = inner
        self.records = list(records or ())
        self.agent_id = agent_id or getattr(inner, "agent_id", "")
        self.payload_key = payload_key
        self.replayed = 0
        self.missed = 0
        self.log: list[dict] = []

    def __call__(self, *, envelope, runtime=None):
        if self.replayed < len(self.records):
            rec = self.records[self.replayed] or {}
            obj = _restore(rec.get("output"), self.payload_key)
            if obj is not None:
                self.replayed += 1
                self.log.append({"n": self.replayed, "replayed": True,
                                 "kind": rec.get("kind", "")})
                from little_crew.agent.protocol import CallResult

                return CallResult.success(obj, evidence=list(rec.get("evidence") or ()))
        self.missed += 1
        self.log.append({"n": self.replayed + self.missed, "replayed": False,
                         "why": "重放记录用尽" if self.records else "没有重放记录"})
        return self.inner(envelope=envelope, runtime=runtime)

    def as_dict(self) -> dict:
        return {"agentId": self.agent_id, "replayed": self.replayed,
                "missed": self.missed, "records": len(self.records),
                "log": self.log[:20]}


def _restore(raw, payload_key: str):
    """把 JSON 还原成**真实对象**（ScriptDraft / ShotList / 普通 dict）。

    还原不出来就返回 None —— 让调用方退回真实现，**不要塞一个半成品**。
    """
    if not isinstance(raw, dict) or not raw:
        return None
    try:
        if raw.get("kind") == "script.draft" or "scenes" in raw:
            from little_crew.agent.crew_llm import _as_draft

            d = _as_draft(raw)
            if d is not None:
                return {"script.draft": d} if payload_key == "script.draft" \
                    else {"script.final": d}
        if raw.get("kind") == "shot.list" or "shots" in raw:
            from little_crew.agent.messages import Shot, ShotList

            # ⚠️ `as_dict()` 存的是**驼峰键**（shotId / durationSec / …），
            # 而 `Shot(**kw)` 要**蛇形键** —— 直接喂会一个都对不上，
            # 结果"还原"出 0 镜、静默退回真实现（2026-11-08 真跑踩到：
            # 续跑没有回报，分镜又跑了一遍）。
            _ALIAS = {"shotId": "shot_id", "sceneId": "scene_id",
                      "shotSize": "shot_size",
                      "cameraMovement": "camera_movement",
                      "durationSec": "duration_sec",
                      "createdAt": "created_at", "msgId": "msg_id",
                      "replyTo": "reply_to", "traceId": "trace_id"}
            fields = {f for f in Shot.__dataclass_fields__}
            shots = []
            for s in (raw.get("shots") or ()):
                s = {_ALIAS.get(k, k): v for k, v in (s or {}).items() if k != "kind"}
                kw = {k: v for k, v in (s or {}).items() if k in fields}
                if isinstance(kw.get("characters"), list):
                    kw["characters"] = tuple(kw["characters"])
                # ⚠️ `Shot.__post_init__` 要求 frm / to 非空（消息契约），
                # 而 checkpoint 里存的是产物，不一定带这两个字段 ——
                # 真跑踩到：还原直接抛 "消息缺 from"，整条续跑链作废。
                kw.setdefault("frm", "storyboard_artist")
                kw.setdefault("to", "*")
                try:
                    shots.append(Shot(**kw))
                except Exception:                            # noqa: BLE001
                    continue
            if shots:
                sl = ShotList(
                    frm=str(raw.get("from") or raw.get("frm") or "storyboard_artist"),
                    to=str(raw.get("to") or "*"),
                    stage="storyboard",
                    episode_id=str(raw.get("episodeId") or ""),
                    shots=tuple(shots),
                    total_seconds=float(raw.get("totalSeconds") or 0.0))
                return {"shot.list": sl}
    except Exception:                                        # noqa: BLE001
        return None
    return None
