"""把产物**变成公网可取的 URL** —— 视频模型只收 URL，不收本地文件。

## 为什么抽象成"发布器"而不是"选定某家"

三家（Seedance / MiniMax / Wan Animate）的参考视频字段都要求
**publicly accessible HTTP(S) URL**，没有一家接受本地文件。
但"怎么变成 URL"至少有三种做法，而且将来会换：

| 实现 | 成本 | 何时用 |
|---|---|---|
| `LocalStaticServer` | 0 | 本机自测（只在 localhost 可见） |
| `TunnelPublisher` | 0（快隧道免账号） | 临时跑通 |
| `ObjectStoragePublisher` | 几毛钱/月 | 长期、固定域名 |

**所以今天定的不是"选哪家"，而是"换家只改一处"。**

## 一条必须遵守的规矩

**按内容哈希命名**（`previs_<sha16>.mp4`），不用原名。

理由与 `_stage_control_image` 相同：原名会互相覆盖，
而"换了白模但 URL 没变"是**看不出来**的失效 —— 厂商侧还能拉到旧文件。

## 一条必须说清的边界

`LocalStaticServer` 只监听 **127.0.0.1**，**厂商拉不到**。
它是给本地自测与假实现用的，**不是**"发布到公网"。

真正的公网发布要么走隧道，要么走对象存储 —— 都要外部依赖，
所以本模块把接口留好，实现按需加。
"""
from __future__ import annotations

import hashlib
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = [
    "LocalStaticServer",
    "NullPublisher",
    "Publisher",
    "TunnelPublisher",
    "content_name",
]

#: 视频模型能拉的 URL 必须以此开头（`LocalStaticServer` 产出的不行）。
PUBLIC_PREFIXES = ("https://", "http://")


def content_name(path: Path) -> str:
    """按**内容哈希**给一个稳定、不冲突的文件名。

    用原名会让"换了白模但文件名没变"变成无法察觉的失效 ——
    厂商侧还能拉到旧文件，而链路看起来一切正常。
    """
    raw = Path(path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()[:16]
    stem = Path(path).stem
    return f"{stem}_{digest}{Path(path).suffix}"


@runtime_checkable
class Publisher(Protocol):
    """发布器的唯一契约。**换实现不影响调用方。**"""

    def publish(self, path: Path) -> str:
        """把一个本地文件发布成 URL，返回可取的地址。"""
        ...


class NullPublisher:
    """什么都不做，直接回本地路径 —— 给"不需要发布"的场景与测试用。

    ⚠️ 它返回的是**本地路径**，不是 URL。**厂商拉不到。**
    显式叫 `Null` 而不是悄悄降级，是为了让"没配发布器"这件事看得见。
    """

    def publish(self, path: Path) -> str:
        return str(Path(path).resolve())


class LocalStaticServer:
    """本机静态文件服务，按内容哈希命名。

    **只监听 127.0.0.1** —— 给本地自测与假实现用，**不是公网发布**。
    用法：

    ```python
    with LocalStaticServer(root) as srv:
        url = srv.publish(video)      # http://127.0.0.1:PORT/<hash>.mp4
    ```
    """

    def __init__(self, root: Path, *, host: str = "127.0.0.1",
                 port: int = 0) -> None:
        self.root = Path(root)
        self.host = host
        self._port = int(port)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ---- 生命周期 ----

    def __enter__(self) -> "LocalStaticServer":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def start(self) -> None:
        if self._httpd is not None:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        root = self.root

        class _Handler(SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):                    # noqa: ANN002
                super().__init__(*a, directory=str(root), **kw)

            def log_message(self, *a):                        # noqa: ANN002
                pass                                          # 静音，别刷屏

        self._httpd = ThreadingHTTPServer((self.host, self._port), _Handler)
        self._port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def base_url(self) -> str:
        if self._httpd is None:
            raise RuntimeError("服务未启动 —— 先 start() 或用 with")
        return f"http://{self.host}:{self._port}"

    # ---- 发布 ----

    def publish(self, path: Path) -> str:
        """把文件（按内容哈希命名）放进根目录，返回它的 URL。"""
        self.start()
        src = Path(path)
        if not src.is_file():
            raise FileNotFoundError(f"要发布的文件不存在：{src}")
        dest = self.root / content_name(src)
        if not dest.is_file() or dest.stat().st_size != src.stat().st_size:
            dest.write_bytes(src.read_bytes())
        return f"{self.base_url}/{dest.name}"


class TunnelPublisher:
    """隧道发布器（占位）。

    **刻意不实现** —— 理由写在这里，免得下一轮重猜：

    1. 快隧道（cloudflared / ngrok）的启动参数**不是稳定 API**，
       写进来就等于把一个会过期的假设固化进生产代码
    2. 现在**没有 API key**，做了也验不到"厂商真能拉到"，
       只能验到"URL 200" —— 那不是同一个承诺
    3. 它只该是 `Publisher` 的**第四个实现**，不该长在调用方里

    真正要接的时候，实现这一层即可，**调用方一行不用改**。
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError(
            "TunnelPublisher 尚未实现。可选做法：\n"
            "  · cloudflared tunnel --url http://127.0.0.1:<port>（免账号快隧道）\n"
            "  · 或直接用 ObjectStoragePublisher\n"
            "接口已留好：实现 publish(path) -> str 即可。")

    def publish(self, path: Path) -> str:                     # pragma: no cover
        raise NotImplementedError

# ---------------------------------------------------------------------------
# 落地暂存 + 公网基址（2026-11-01）
# ---------------------------------------------------------------------------

class PublishError(RuntimeError):
    """发不出去（缺公网基址 / 文件不存在 / 名字非法）。**明确失败，不返回假 URL。**"""


class StagingPublisher:
    """把产物**暂存到一个目录**，并拼出公网 URL。

    ## 为什么这样设计

    实测（`docs/05-云API接入调研.md`）：多数厂商要求**公网可访问的素材 URL**，
    不接受本地文件。而本机**没有任何隧道工具**（cloudflared / ngrok / frpc 全无）。

    所以真正缺的不是"上传代码"，是**一个公网地址**。这一步拆成两半：

        ① 暂存（本类负责）：内容寻址地复制到 `root/`，写 manifest
        ② 暴露（外部负责）：对象存储上传 / 隧道 / 已有域名，把 base 填进来

    **对象存储和隧道将来只是"填 base_url"的两种方式，不用改本类。**

    ## 三个刻意的决定

    **① 内容寻址命名**：`<sha16>_<原名>` —— 同一份内容重复发布不会产生新文件，
       也避免同名覆盖（之前踩过"两个测试文件写同一路径导致哈希相同"的坑）。

    **② 没有公网基址就报错**，不返回 `http://127.0.0.1/...` 冒充可访问。
       本地 URL 传给云端会得到一个莫名其妙的 404，那比直接报错更难查。

    **③ 记 manifest**：发布过什么、多大、哈希多少 —— 事后能对账。
    """

    def __init__(self, root: Path, *, public_base: str = "") -> None:
        self.root = Path(root)
        self.public_base = (public_base or "").rstrip("/")
        self._entries: list[dict] = []

    # ---- 发布 ----
    def publish(self, path: Path) -> str:
        import hashlib
        import shutil

        src = Path(path)
        if not src.is_file():
            raise PublishError(f"文件不存在：{src}")
        self.root.mkdir(parents=True, exist_ok=True)
        data = src.read_bytes()
        digest = hashlib.sha256(data).hexdigest()[:16]
        name = f"{digest}_{src.name}"
        dst = self.root / name
        if not dst.is_file():
            shutil.copyfile(src, dst)
            self._entries.append({"name": name, "bytes": len(data),
                                  "sha256_16": digest, "source": str(src)})
        if not self.public_base:
            raise PublishError(
                "没有公网基址，无法给出可访问 URL。"
                "设置 public_base（环境变量 LITTLE_CREW_PUBLIC_BASE）"
                "或改用对象存储上传后填它的域名。"
                f"文件已暂存于 {dst}")
        return f"{self.public_base}/{name}"

    # ---- 观测 ----
    def files(self) -> list:
        return self.root.glob("*") if self.root.is_dir() else []

    def manifest(self) -> dict:
        """**以磁盘为准**，不以实例内存为准。

        踩过：第一个实例发布后报错，第二个实例发布同一文件时
        "已存在则跳过"，于是第二个实例的 entries 是空的 ——
        清单看起来"什么都没发布"，但文件明明在那儿。
        """
        entries = []
        if self.root.is_dir():
            for f in sorted(self.root.glob("*")):
                if f.is_file():
                    entries.append({"name": f.name, "bytes": f.stat().st_size,
                                    "hasPublicUrl": bool(self.public_base)})
        return {"kind": "publish.manifest",
                "root": str(self.root), "publicBase": self.public_base,
                "entries": entries}

    # ---- 环境探测（给人看的） ----
    @staticmethod
    def missing_tunnel_tools() -> list:
        """本机缺哪些隧道工具。**返回缺失列表，不抛** —— 用来打印提示。"""
        import shutil as _sh

        return [t for t in ("cloudflared", "ngrok", "frpc")
                if _sh.which(t) is None]


def public_base_from_env() -> str:
    """从 `LITTLE_CREW_PUBLIC_BASE` 取公网基址（空串 = 未配置）。"""
    import os

    return (os.environ.get("LITTLE_CREW_PUBLIC_BASE") or "").strip()
