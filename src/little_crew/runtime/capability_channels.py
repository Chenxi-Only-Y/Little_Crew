"""**能力调用层**：同一个能力，本地与云端只是"通道"不同（2026-11-08）。

## ⚠️ 它与 `runtime/channels/` 是两个层，不要混

```
作业层  runtime/channels/         Channel.submit/poll/collect/reconcile
         ComfyChannel · HttpChannel   适合"提交一个渲染任务、等它出图"
能力层  runtime/capability_channels.py（本文件）
         BreezeTts · LocalVideo · LocalMusic · CloudChannel
                                      适合"同步调一次，拿一个结果"
```

**为什么不能只留一层**：TTS 合成一次 1~4 秒、直接返回文件；
渲染一镜要几分钟、必须能轮询与对账。**硬凑成一个协议，两边都会别扭。**

## 本层遵守的约定（与作业层对齐词汇）

- `channel_id` / `capability` / `location`
- `available() -> (bool, reason)`（**不抛异常**，与作业层一致）
- 调用统一 `dict -> dict`，返回必带 `ok / error / provider / channel`

## 问题在哪

原来每个能力一套写法：

```
tts     -> tts_breeze.synth(text, path, voice=…, instruction=…, seed=…)
video   -> video_operator.render(image, path, duration=…, backend=…)
music   -> music_bed.resolve(seconds, out_dir, mood=…)
text    -> LlmClient.chat(prompt, system=…)
```

**签名不同、错误语义不同、指纹记录不一致** —— 想换云端就得改调用方，
于是每换一次通道就多一处分叉。

## 这一层怎么统一

```
请求统一是 dict（能力相关字段放里面）
返回统一是 dict：{ok, path, seconds, provider, model, servedModel,
                  fingerprint, error}
选择统一走环境变量：LITTLE_CREW_<CAP>_CHANNEL=local|cloud[:实现名]
```

调用方只写 `call("tts", {...})`，**不关心是本地还是云端**。

## 三条纪律

1 **不可用要说清**：云端没配 key/endpoint 时 `available()=False`，
   `call()` 返回"未配置"而不是静默降级到本地
2 **指纹统一记**：cloud 通道成功后写 `provider.model.servedModel`（复用
   `governance/provider_fingerprint`），本地写实现名
3 **能力不是无限的**：只登记真实存在的能力（text/image/video/tts/music），
   未知能力直接报错
"""
from __future__ import annotations

import os

#: 已登记的能力（**新增能力必须先在这里登记**）
CAPABILITIES = ("text", "image", "video", "tts", "music")

#: 每个能力的环境变量：选哪个通道（local / cloud[:名字]）
def env_key(capability: str) -> str:
    return f"LITTLE_CREW_{str(capability).upper()}_CHANNEL"


class Channel:
    """一个通道 = 一个能力的一种实现。子类只需实现 `available()` 与 `call()`。"""

    name: str = ""
    capability: str = ""
    location: str = "local"      # "local" | "cloud"

    #: 通道实例标识（与 `runtime/channels/Channel.channel_id` 同义）
    @property
    def channel_id(self) -> str:
        return self.name

    def available(self) -> tuple[bool, str]:           # pragma: no cover - 抽象
        raise NotImplementedError

    def call(self, request: dict) -> dict:             # pragma: no cover - 抽象
        raise NotImplementedError

    # ---- 统一包装：补齐公共字段 + 记录指纹 ----
    def invoke(self, request: dict) -> dict:
        req = dict(request or {})
        can, why = self.available()
        if not can:
            return self._fail("channel-unavailable",
                              f"{self.capability} 通道 {self.name} 不可用"
                              f"（{self.location}）{('：' + why) if why else ''}")
        try:
            out = dict(self.call(req) or {})
        except Exception as e:                        # noqa: BLE001
            return self._fail("channel-error", f"{type(e).__name__}: {e}")
        out.setdefault("ok", False)
        out.setdefault("error", "")
        out["provider"] = str(out.get("provider") or self.name)
        out["channel"] = self.name
        out["location"] = self.location
        if out.get("ok"):
            self._note(req, out)
        return out

    def _note(self, request: dict, out: dict) -> None:
        """把这次调用记进指纹（云端尤其需要）。"""
        try:
            from little_crew.governance.provider_fingerprint import note_reply

            note_reply(kind=f"channel:{self.capability}",
                       provider=str(out.get("provider") or self.name),
                       model=str(out.get("model") or ""),
                       served_model=str(out.get("servedModel") or ""),
                       params={"location": self.location},
                       request=str(request.get("prompt")
                                   or request.get("text")
                                   or request.get("image") or ""))
        except Exception:                             # noqa: BLE001
            pass

    def _fail(self, code: str, why: str) -> dict:
        return {"ok": False, "error": f"{code}: {why}", "path": "",
                "seconds": 0.0, "provider": self.name, "channel": self.name,
                "location": self.location, "model": "", "servedModel": ""}


#: 已注册的通道：capability -> [Channel, ...]
_REGISTRY: dict[str, list[Channel]] = {}


def register(channel: Channel) -> Channel:
    """登记一个通道（重复登记同名会覆盖 —— 便于测试替换）。"""
    cap = str(getattr(channel, "capability", "") or "")
    if cap not in CAPABILITIES:
        raise ValueError(f"未知能力 {cap!r}（可选：{CAPABILITIES}）")
    lst = _REGISTRY.setdefault(cap, [])
    for i, c in enumerate(lst):
        if c.name == channel.name:
            lst[i] = channel
            return channel
    lst.append(channel)
    return channel


def registered(capability: str) -> list[Channel]:
    return list(_REGISTRY.get(str(capability), ()))


def resolve(capability: str, *, prefer: str = "") -> Channel | None:
    """选一个通道。

    优先级：`prefer` > 环境变量 `LITTLE_CREW_<CAP>_CHANNEL` > 第一个可用的本地通道。

    环境变量写法：
        `local`            选第一个可用的本地通道
        `cloud`            选第一个可用的云端通道
        `local:breeze`     选指定名字的通道
    """
    cap = str(capability)
    if cap not in CAPABILITIES:
        raise ValueError(f"未知能力 {cap!r}（可选：{CAPABILITIES}）")
    want = str(prefer or os.environ.get(env_key(cap)) or "").strip()
    items = registered(cap)
    if not items:
        return None
    if want:
        if ":" not in want:
            # 不带冒号时：**先按"通道名"精确匹配**，再当成 location。
            # （否则 `prefer="breeze"` 会被误当成 location="breeze" 而永远选不到）
            for c in items:
                if c.name == want and c.available()[0]:
                    return c
        loc, _, name = want.partition(":")
        loc, name = loc.strip(), name.strip()
        for c in items:
            if name and c.name != name:
                continue
            if loc and c.location != loc:
                continue
            if c.available()[0]:
                return c
        return None
    for c in items:                                   # 默认：本地优先
        if c.location == "local" and c.available()[0]:
            return c
    for c in items:
        if c.available()[0]:
            return c
    return None


def call(capability: str, request: dict, *, prefer: str = "") -> dict:
    """统一调用入口。**找不到可用通道时明确失败**，不静默降级。"""
    ch = resolve(capability, prefer=prefer)
    if ch is None:
        return {"ok": False, "path": "", "seconds": 0.0,
                "error": f"no-channel: {capability} 没有可用通道"
                         f"（已登记 {[c.name for c in registered(capability)]}）",
                "provider": "", "channel": "", "location": "",
                "model": "", "servedModel": ""}
    return ch.invoke(request)


def describe() -> dict:
    """当前每个能力有哪些通道、选中的是哪个（给诊断/证据用）。"""
    out = {}
    for cap in CAPABILITIES:
        items = registered(cap)
        ch = resolve(cap)
        out[cap] = {
            "channels": [{"name": c.name, "location": c.location,
                          "available": bool(c.available()[0]),
                          "reason": c.available()[1]} for c in items],
            "selected": ch.name if ch else "",
            "env": env_key(cap),
            "envValue": os.environ.get(env_key(cap), ""),
        }
    return out
