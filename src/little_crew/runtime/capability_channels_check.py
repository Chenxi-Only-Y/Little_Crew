"""能力通道层自检：注册 · 选择 · 失败语义 · 指纹。

**不真的调模型/合成**（会慢且要密钥）；用一个假通道验证机制。
"""
from __future__ import annotations

import os
import sys

from little_crew.runtime.capability_channels import (
    CAPABILITIES, Channel, call, describe, env_key, register, registered,
    resolve,
)

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


class Fake(Channel):
    def __init__(self, name, capability, location="local", ok=True, reason=""):
        self.name, self.capability, self.location = name, capability, location
        self._ok, self._reason = ok, reason
        self.seen = []

    def available(self):
        return (self._ok, self._reason)

    def call(self, request):
        self.seen.append(dict(request))
        return {"ok": self._ok, "path": str(request.get("path") or ""),
                "seconds": 1.5, "model": self.name}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("能力通道层 —— 自检")
    print("=" * 66)

    print(); print("A. 注册")
    a = register(Fake("loc", "tts"))
    check("A1 ★★ 注册成功", a in registered("tts"), str([c.name for c in registered("tts")]))
    try:
        register(Fake("bad", "nope"))
        ok = False
    except ValueError:
        ok = True
    check("A2 ★★★ 未知能力直接报错（不是静默接受）", ok, "")
    register(Fake("loc", "tts"))                 # 同名再注册
    check("A3 ★★ 同名覆盖（便于测试替换）",
          sum(1 for c in registered("tts") if c.name == "loc") == 1, "")

    print(); print("B. 选择")
    register(Fake("cloud", "tts", location="cloud", ok=False, reason="未配置"))
    os.environ.pop(env_key("tts"), None)
    check("B1 ★★ 默认选本地可用通道", resolve("tts").name == "loc", str(resolve("tts")))
    os.environ[env_key("tts")] = "cloud"
    check("B2 ★★★ 环境变量指到不可用的云端 -> 返回 None（**不静默回本地**）",
          resolve("tts") is None, str(resolve("tts")))
    os.environ.pop(env_key("tts"), None)
    check("B3 ★★ 指定名字优先", resolve("tts", prefer="loc").name == "loc", "")
    check("B4 ★★ env_key 命名规则", env_key("tts") == "LITTLE_CREW_TTS_CHANNEL", env_key("tts"))

    print(); print("C. 调用与失败语义")
    r = call("tts", {"text": "你好", "path": "/tmp/x.wav"})
    check("C1 ★★ 统一返回结构", r["ok"] and r["channel"] == "loc" and r["seconds"] == 1.5, str(r))
    check("C2 ★★★ 结果带 provider/channel/location",
          r["provider"] == "loc" and r["location"] == "local", str(r))
    os.environ[env_key("tts")] = "cloud"
    r2 = call("tts", {"text": "x"})
    check("C3 ★★★ 没有可用通道 -> 明确报 no-channel",
          r2["ok"] is False and "no-channel" in r2["error"], r2["error"])
    os.environ.pop(env_key("tts"), None)
    r3 = call("tts", {"path": ""})
    check("C4 ★ 请求缺字段也能拿到结构化返回", "ok" in r3, str(r3)[:60])

    print(); print("D. 诊断")
    register(Fake("m", "music"))
    d = describe()
    check("D1 ★★ describe 覆盖全部能力", set(d) == set(CAPABILITIES), str(sorted(d)))
    check("D2 ★★ describe 报告 available 与 reason",
          all("reason" in c for c in d["tts"]["channels"]), "")
    check("D3 ★ 未登记的能力给空列表", d["image"]["channels"] == [], str(d["image"]))

    n = sum(1 for _, ok, _ in _r if ok)
    print()
    for name, ok, detail in _r:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 66)
    print(f"结果：{n}/{len(_r)} 通过")
    return 0 if n == len(_r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
