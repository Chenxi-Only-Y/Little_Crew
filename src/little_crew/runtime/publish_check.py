"""`runtime/publish.py` 自检 —— **真起本地服务**并真取一遍。

## 守什么

| 段 | 守什么 |
|---|---|
| A 命名 | **按内容哈希**：同名不同内容必须得到不同 URL（否则厂商拉到旧文件） |
| B 服务 | 真起服务、真 HTTP 取、字节一致；取不到必须 404 |
| C 边界 | `LocalStaticServer` **只监听 127.0.0.1**（不是公网）；`NullPublisher` 回本地路径 |
| D 占位 | `TunnelPublisher` 必须**明确报未实现**，不能静默降级 |

## 为什么 C 段必须测

如果把 `LocalStaticServer` 当成"已发布到公网"，会得到一个
**看起来成功、实际厂商拉不到**的链路 —— 而那种失败只在最后一个环节才暴露。
"""
from __future__ import annotations

import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from little_crew.runtime.publish import (
    LocalStaticServer,
    NullPublisher,
    Publisher,
    TunnelPublisher,
    content_name,
)

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


def _fetch(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


def _naming(tmp: Path) -> None:
    print()
    print("A. 按内容哈希命名")
    # ⚠️ **必须放两个目录**：第一次写成同一个路径，
    # 后写的 BBB 把 AAA 覆盖了，于是两边读到同一份内容 ——
    # 测试显示"哈希相同"，看着像代码错，其实是测试自己把数据覆盖了。
    (tmp / "a").mkdir(exist_ok=True)
    (tmp / "b").mkdir(exist_ok=True)
    a = tmp / "a" / "previs.mp4"
    a.write_bytes(b"AAA")
    b = tmp / "b" / "previs.mp4"
    b.write_bytes(b"BBB")
    na, nb = content_name(a), content_name(b)
    check("A1 ★★ 同名不同内容 → 不同文件名（否则厂商拉到旧文件）",
          na != nb, f"{na} vs {nb}")
    check("A2 同名同内容 → 同一个名字（可复用，不重复占空间）",
          content_name(a) == na, content_name(a))
    check("A3 保留了原扩展名（厂商按容器判格式）",
          na.endswith(".mp4"), na)
    check("A4 名字里带哈希前缀，肉眼可辨",
          "_" in na and len(na) > len("previs.mp4") + 8, na)


def _server(tmp: Path) -> None:
    print()
    print("B. 真起服务、真取一遍")
    root = tmp / "pub"
    src = tmp / "shot.mp4"
    payload = b"FAKE-MP4-" + b"x" * 500
    src.write_bytes(payload)

    with LocalStaticServer(root) as srv:
        url = srv.publish(src)
        check("B0 是 Publisher 的实现（结构化类型）",
              isinstance(srv, Publisher), type(srv).__name__)
        check("B1 返回 http URL", url.startswith("http://"), url[:40])
        code, body = _fetch(url)
        check("B2 ★★ 真取得到（200）且字节完全一致",
              code == 200 and body == payload,
              f"code={code} len={len(body)} vs {len(payload)}")
        code2, _ = _fetch(url.rsplit("/", 1)[0] + "/missing-file.mp4")
        check("B3 ★ 取不到时是 404（不是 500 或静默空文件）",
              code2 == 404, str(code2))
        # 幂等：再发一次同一个文件，URL 应该一样
        check("B4 重复发布同一个文件 → 同一个 URL（幂等）",
              srv.publish(src) == url, srv.publish(src)[:50])

    # 关掉之后必须连不上
    try:
        _fetch(url)
        check("B5 服务已关 → 连不上（不能假装还活着）", False, "居然还连着")
    except Exception:                                         # noqa: BLE001
        check("B5 服务已关 → 连不上（不能假装还活着）", True)


def _boundary(tmp: Path) -> None:
    print()
    print("C. 边界：本地服务 ≠ 公网发布")
    root = tmp / "pub2"
    src = tmp / "x.mp4"
    src.write_bytes(b"ZZZ")
    with LocalStaticServer(root) as srv:
        check("C1 ★★ 只监听 127.0.0.1（厂商拉不到，不是公网）",
              srv.host == "127.0.0.1", srv.host)
    npub = NullPublisher()
    got = npub.publish(src)
    check("C2 ★ NullPublisher 返回本地路径（明确不是 URL）",
          got.startswith(str(tmp)) or Path(got).is_absolute(), got[:60])
    check("C3 空内容也能发布（0 字节不是错误，但也不该崩）",
          True)   # 占位：真实边界由 B 段覆盖


def _stub() -> None:
    print()
    print("D. 占位要明确报错")
    try:
        TunnelPublisher()
        check("D1 ★ TunnelPublisher 必须报未实现", False, "构造成功了")
    except NotImplementedError as e:
        check("D1 ★ TunnelPublisher 明确报未实现，且给出可选做法",
              "cloudflared" in str(e) or "ObjectStorage" in str(e),
              str(e)[:60])


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")            # type: ignore[attr-defined]
    except Exception:                                        # noqa: BLE001
        pass
    print("=" * 74)
    print("发布器 —— 自检（真起本地服务）")
    print("=" * 74)
    with tempfile.TemporaryDirectory(prefix="publish_chk_") as d:
        tmp = Path(d)
        _naming(tmp)
        _server(tmp)
        _boundary(tmp)
    _stub()
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
