"""**后端选择**：把"用哪个后端"从入口脚本里搬出来，变成一处可测的判定。

## 它解决什么问题

原来 `episode_demo.py` 里写死了这一行：

```python
be = next(b for b in spec.backends if b.kind is BackendKind.HTTP)
ch = HttpChannel(be, timeout_s=1800)
```

后果：

1. **整集永远走 local-gen** —— 哪怕 `z-image-turbo` 同时声明了 COMFYUI 后端
2. 于是 `composition.zimage_controlnet`（ComfyUI 专用）**永远编译不进去**，
   白模控制图生成出来了也**不会被消费**
3. 换后端要改入口脚本，而入口脚本不是"要什么"，是"怎么连" —— 放错了层

**这里把它变成一张表 + 一个函数**：入口只传名字，其余由 `Backend` 声明决定。

## 为什么不是"读 IR 决定"

IR 里是**要什么**（`Capability` 需求），不是**连哪台机器**（endpoint / kind）。
把 endpoint 塞进 IR 会让同一份施工单不能在不同机器上重放 —— 那正好毁掉
"后端可替换"这个卖点。

所以分工是：

```
IR        → 要什么（算子/约束/分辨率）
Backend   → 连哪、怎么连（kind / endpoint / 权重是否齐）
本模块     → 把"名字"翻译成"声明"，并造出通道
```

**判断标准**：同一份 IR 换个 `--backend` 名字就能在另一台机器上跑 = 分层对了。
"""
from __future__ import annotations

from typing import Any

from little_crew.models.spec import BackendKind

__all__ = [
    "BACKEND_CHOICES",
    "DEFAULT_BACKEND",
    "BackendChoiceError",
    "pick_backend",
    "make_channel",
]

#: 名字 → 后端种类。**这是唯一的映射表**，入口脚本只认名字。
BACKEND_CHOICES: dict[str, BackendKind] = {
    "localgen": BackendKind.HTTP,
    "comfyui": BackendKind.COMFYUI,
}

#: 默认后端。**保持 local-gen** —— 与改造前行为逐字节一致（A 步的要求）。
DEFAULT_BACKEND = "localgen"


class BackendChoiceError(RuntimeError):
    """选不到后端时抛。**不静默退回默认后端** —— 那会让"配置写错"变成"悄悄换了一台机器"。"""


def pick_backend(spec: Any, name: str = DEFAULT_BACKEND) -> Any:
    """从模型声明里挑出名为 `name` 的后端声明（`Backend`）。

    **明确失败**，不退回默认：名字拼错、或声明里根本没有这一类后端时抛错。
    """
    key = str(name or DEFAULT_BACKEND).strip().lower()
    kind = BACKEND_CHOICES.get(key)
    if kind is None:
        raise BackendChoiceError(
            f"未知后端 {name!r}；可选：{', '.join(sorted(BACKEND_CHOICES))}")
    backends = list(getattr(spec, "backends", ()) or ())
    for be in backends:
        if getattr(be, "kind", None) is kind:
            return be
    raise BackendChoiceError(
        f"模型 {getattr(spec, 'model_id', '?')} 没有声明 {kind} 后端；"
        f"已声明：{[str(getattr(b, 'kind', '?')) for b in backends]}")


def make_channel(backend: Any, *, timeout_s: int = 1800, **kw: Any) -> Any:
    """按后端声明造通道。**只有这里 import 通道实现**（入口脚本不必知道有几种通道）。"""
    kind = getattr(backend, "kind", None)
    if kind is BackendKind.COMFYUI:
        from little_crew.runtime.channels.comfy import ComfyChannel

        return ComfyChannel(backend, timeout_s=timeout_s, **kw)
    if kind is BackendKind.HTTP:
        from little_crew.runtime.channels.http import HttpChannel

        return HttpChannel(backend, timeout_s=timeout_s, **kw)
    raise BackendChoiceError(f"暂不支持的后端种类：{kind}")


def describe(backend: Any) -> str:
    """一行说明，给命令行打印用（**让人看得见换了后端**）。"""
    kind = getattr(backend, "kind", "?")
    ep = getattr(backend, "endpoint", "") or "(未设 endpoint)"
    return f"{kind} · {ep}"
