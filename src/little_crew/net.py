"""统一 HTTP(S) 取数入口（标准库，零第三方依赖）。

## 为什么需要它（2026-11-13 实测）

平台本来各处直接 `urllib.request.urlopen(...)`。接智谱图像 API 时踩到：

```
urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
unable to get issuer certificate
```

根因**不是本机证书缺失**：该端点返回的证书链**缺中间证书**，
浏览器 / Windows schannel 会通过 AIA 自动补全，**而 Python 的 `ssl` 不会**。
同一个 key 用 PowerShell 能通、用 Python 不通 —— 就是这么来的。

## 策略（三档，默认最严）

| 环境变量 | 行为 |
|---|---|
| （默认） | 严格校验（与标准库一致） |
| `LITTLE_CREW_SSL_CA_BUNDLE=<path>` | 用指定 CA 包（企业内网/自建 CA 用这个） |
| `LITTLE_CREW_SSL_NO_VERIFY=1` | **不校验**（只在"你确认这个端点可信"时用；会打印一次警告） |

## 纪律

- **默认不放松**：安全默认值必须是"严格"，放松只能显式发生、且要留痕。
- **只此一处**：不要在业务代码里再写 `ssl._create_unverified_context()` ——
  散落的安全旁路是审计灾难（"谁关的校验"查不出来）。
"""

from __future__ import annotations

import os
import ssl
import urllib.error
import urllib.request
from typing import Any

#: 显式声明"不校验证书"的环境变量
ENV_NO_VERIFY = "LITTLE_CREW_SSL_NO_VERIFY"
#: 指定 CA 包路径
ENV_CA_BUNDLE = "LITTLE_CREW_SSL_CA_BUNDLE"

_warned = False


def _truthy(v: str) -> bool:
    return str(v or "").strip() in ("1", "true", "yes", "on")


def ssl_context() -> ssl.SSLContext | None:
    """构造 SSL 上下文；返回 `None` 表示"用标准库默认（严格校验）"。"""
    global _warned
    bundle = str(os.environ.get(ENV_CA_BUNDLE) or "").strip()
    if bundle:
        try:
            return ssl.create_default_context(cafile=bundle)
        except Exception as exc:                              # noqa: BLE001
            print(f"  ⚠️ {ENV_CA_BUNDLE} 加载失败（退回严格校验）："
                  f"{type(exc).__name__}: {exc}")
            return None
    if _truthy(os.environ.get(ENV_NO_VERIFY) or ""):
        if not _warned:
            print(f"  ⚠️ 已关闭 TLS 证书校验（{ENV_NO_VERIFY}=1）—— "
                  f"仅在你确认端点可信时使用；生产环境请改用 {ENV_CA_BUNDLE}")
            _warned = True
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return None


def insecure_context() -> ssl.SSLContext:
    """调用方显式声明「不校验」时用的上下文（会打印一次警告）。"""
    global _warned
    if not _warned:
        print("  \u26a0\ufe0f 该供应商已声明 insecureTls：不校验证书"
              "（原因通常是其证书链缺中间证书）。仅对该供应商生效。")
        _warned = True
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def urlopen(request: Any, *, timeout: float | None = None,
            insecure: bool = False) -> Any:
    """等价 `urllib.request.urlopen`，但尊重上面的 TLS 策略。

    参数 `insecure=True` 用于**供应商显式声明** `insecureTls` 的场景
    （见 `models.spec.Backend.insecure_tls`）—— 只对该供应商生效，不全局。

    ⚠️ **证书失败重试一次**（2026-11-13 实测）：缺中间证书的端点在 Python 里
    表现为**间歇性**失败（同一请求重试有时能过）。握手失败很快，重试代价可忽略；
    两次都失败才抛 —— **不静默降级为不校验**。
    """
    ctx = insecure_context() if insecure else ssl_context()
    last: Exception | None = None
    for attempt in (1, 2):
        try:
            if ctx is None:
                return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310
            return urllib.request.urlopen(request, timeout=timeout, context=ctx)  # noqa: S310
        except Exception as exc:                            # noqa: BLE001
            msg = str(exc).lower()
            if attempt == 1 and ("certificate verify failed" in msg
                                 or "[ssl:" in msg):
                last = exc
                continue
            raise
    raise last if last is not None else RuntimeError("urlopen 失败")


def read_text(url: str, *, timeout: float | None = 30) -> str:
    """GET 一个 URL 并解码成文本。"""
    req = urllib.request.Request(url, headers={"User-Agent": "little-crew/1"})
    with urlopen(req, timeout=timeout) as resp:              # noqa: S310
        return resp.read().decode("utf-8", errors="replace")


def download(url: str, dest: Any, *, timeout: float | None = 120) -> int:
    """下载文件到 `dest`（Path 或 str），返回字节数。"""
    from pathlib import Path

    out = Path(str(dest))
    out.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "little-crew/1"})
    n = 0
    with urlopen(req, timeout=timeout) as resp, out.open("wb") as fh:  # noqa: S310
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            fh.write(chunk)
            n += len(chunk)
    return n


def post_multipart(url: str, fields: dict[str, str], files: dict[str, tuple[str, bytes]],
                   *, headers: dict[str, str] | None = None,
                   timeout: float | None = 300) -> bytes:
    """POST `multipart/form-data`（零第三方依赖，手写边界）。

    `files` 形如 `{"image": ("ref.png", b"...")}`。
    为什么放在根模块 `net.py`：**review 与 runtime 两层都需要发图**
    （封面设计师生成要走图生图；渲染通道也要），而两层之间不允许互相依赖。
    """
    import uuid

    boundary = "----dsh" + uuid.uuid4().hex

    def _part(name: str, value: str) -> bytes:
        return (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                f'\r\n\r\n{value}\r\n').encode("utf-8")

    body = b""
    for k, v in (fields or {}).items():
        body += _part(str(k), str(v))
    for name, (filename, data) in (files or {}).items():
        hdr = (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
               f'filename="{filename}"\r\nContent-Type: application/octet-stream'
               f'\r\n\r\n').encode("utf-8")
        body += hdr + data + b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")

    hdrs = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=body, method="POST", headers=hdrs)
    with urlopen(req, timeout=timeout) as resp:              # noqa: S310
        return resp.read()
