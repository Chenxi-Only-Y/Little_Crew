"""`runtime/backend_select.py` 自检 —— 不联网（通道 `available()` 不在这里测）。

## 守什么

| 段 | 守什么 |
|---|---|
| A 映射表 | `localgen`→HTTP、`comfyui`→COMFYUI，且**默认是 localgen**（行为不变） |
| B 挑选 | 正常挑到 / 未知名字**明确报错** / 声明里没有这一类**明确报错** |
| C 造通道 | 按 kind 造对应 `Channel`；未支持的 kind 报错 |
| D 描述 | 一行说明里带 kind 与 endpoint（**让人看得见换了后端**） |
| E 源码守卫 | ★ `episode_demo` 不再写死 `BackendKind.HTTP` |

## 为什么 E 段是重点

改造前 `episode_demo.py` 写死：

```python
be = next(b for b in spec.backends if b.kind is BackendKind.HTTP)
```

**后果不是报错，而是「整集永远走 local-gen」** —— 于是 ComfyUI 专用的
`composition.zimage_controlnet` 永远编译不进去，白模控制图生成了也**没人用**。

这类「看着正常、实际少做一件事」的错误，**只能靠源码断言钉住**。
"""
from __future__ import annotations

import sys
from pathlib import Path

from little_crew.models.spec import BackendKind
from little_crew.runtime.backend_select import (
    BACKEND_CHOICES,
    DEFAULT_BACKEND,
    BackendChoiceError,
    describe,
    make_channel,
    pick_backend,
)

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


class _BE:
    """测试替身。**缺什么属性都返回空串** —— 通道实现在构造时会摸好几个字段
    （`path` / `api_key_env` / `required_kinds` …），逐个补太脆。"""

    def __init__(self, kind, endpoint=""):
        self.kind = kind
        self.endpoint = endpoint

    def __getattr__(self, name: str):
        return ""


class _Spec:
    def __init__(self, backends, model_id="m"):
        self.backends = tuple(backends)
        self.model_id = model_id


def _map() -> None:
    print()
    print("A. 映射表")
    check("A1 localgen -> HTTP", BACKEND_CHOICES.get("localgen") is BackendKind.HTTP,
          str(BACKEND_CHOICES.get("localgen")))
    check("A2 comfyui -> COMFYUI",
          BACKEND_CHOICES.get("comfyui") is BackendKind.COMFYUI,
          str(BACKEND_CHOICES.get("comfyui")))
    check("A3 ★★ 默认仍是 localgen（A 步要求行为不变）",
          DEFAULT_BACKEND == "localgen", DEFAULT_BACKEND)
    check("A4 只有两个可选（不多不少）", len(BACKEND_CHOICES) == 2,
          str(sorted(BACKEND_CHOICES)))


def _pick() -> None:
    print()
    print("B. 挑选")
    spec = _Spec([_BE(BackendKind.COMFYUI, "c"), _BE(BackendKind.HTTP, "h")])
    check("B1 挑到 HTTP", pick_backend(spec, "localgen").endpoint == "h", "")
    check("B2 挑到 COMFYUI", pick_backend(spec, "comfyui").endpoint == "c", "")
    check("B3 ★ 名字大小写/空白容错",
          pick_backend(spec, "  ComfyUI ").endpoint == "c", "")
    bad = None
    try:
        pick_backend(spec, "no-such")
    except BackendChoiceError as e:
        bad = str(e)
    check("B4 ★★ 未知名字 -> 明确报错（不静默退回默认）", bool(bad), str(bad)[:60])
    check("B5 报错里列出可选值", bool(bad) and "localgen" in bad and "comfyui" in bad,
          str(bad)[:80])
    only_http = _Spec([_BE(BackendKind.HTTP, "h")], model_id="z-image-turbo")
    miss = None
    try:
        pick_backend(only_http, "comfyui")
    except BackendChoiceError as e:
        miss = str(e)
    check("B6 ★★ 声明里没有这一类 -> 明确报错（说清已声明哪些）",
          bool(miss) and "COMFYUI" in miss, str(miss)[:80])


def _channel() -> None:
    print()
    print("C. 造通道")
    from little_crew.runtime.channels.comfy import ComfyChannel
    from little_crew.runtime.channels.http import HttpChannel

    ch_http = make_channel(_BE(BackendKind.HTTP, "http://127.0.0.1:1"), timeout_s=5)
    ch_comfy = make_channel(_BE(BackendKind.COMFYUI, "http://127.0.0.1:2"), timeout_s=5)
    check("C1 HTTP -> HttpChannel", isinstance(ch_http, HttpChannel),
          type(ch_http).__name__)
    check("C2 COMFYUI -> ComfyChannel", isinstance(ch_comfy, ComfyChannel),
          type(ch_comfy).__name__)
    check("C3 超时参数传进去了", getattr(ch_http, "timeout_s", None) == 5,
          str(getattr(ch_http, "timeout_s", None)))
    bad = None
    try:
        make_channel(_BE(BackendKind.PROCESS, "x"))
    except BackendChoiceError as e:
        bad = str(e)
    check("C4 ★ 未支持的 kind -> 明确报错", bool(bad), str(bad)[:60])


def _describe() -> None:
    print()
    print("D. 一行说明")
    s = describe(_BE(BackendKind.HTTP, "http://127.0.0.1:8100"))
    check("D1 带 kind", "HTTP" in s, s)
    check("D2 带 endpoint", "8100" in s, s)
    s2 = describe(_BE(BackendKind.COMFYUI, ""))
    check("D3 endpoint 为空时不说空话", "未设" in s2, s2)


def _source_guard() -> None:
    print()
    print("E. ★ 源码守卫：入口不再写死后端")
    src = (Path(__file__).parent / "episode_demo.py").read_text(encoding="utf-8")
    check("E1 ★★★ 不再写死 `b.kind is BackendKind.HTTP`",
          "b.kind is BackendKind.HTTP" not in src, "还在写死")
    check("E2 ★★ 入口用 pick_backend 挑后端", "_pick_backend(" in src, "没用")
    check("E3 ★★ 入口用 make_channel 造通道", "_make_channel(" in src, "没用")
    check("E4 ★ 有 --backend 开关", '"--backend"' in src, "没有")
    check("E5 ★★ 不可用时明确打印（不静默退回）",
          "不可用" in src and "return 1" in src, "")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")            # type: ignore[attr-defined]
    except Exception:                                        # noqa: BLE001
        pass
    print("=" * 74)
    print("后端选择 —— 自检")
    print("=" * 74)
    _map()
    _pick()
    _channel()
    _describe()
    _source_guard()
    print()
    npass = sum(1 for _, ok, _ in _results if ok)
    for name, ok, detail in _results:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 74)
    print(f"结果：{npass}/{len(_results)} 通过")
    return 0 if npass == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
