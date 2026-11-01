"""`runtime/publish.StagingPublisher` 自检 —— 纯文件，不联网。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from little_crew.runtime.publish import (
    PublishError,
    StagingPublisher,
    public_base_from_env,
)

_results = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), detail))


def _mk(root: Path, name="白模.mp4", data=b"x" * 128) -> Path:
    f = root / name
    f.write_bytes(data)
    return f


def _stage():
    print(); print("A. 暂存")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        f = _mk(root)
        pub = StagingPublisher(root / "pub", public_base="https://cdn.x.com")
        u = pub.publish(f)
        check("A1 有 base 时返回 URL", u.startswith("https://cdn.x.com/"), u)
        check("A2 ★ 内容寻址命名（sha16_原名）",
              len(u.split("/")[-1].split("_")[0]) == 16, u.split("/")[-1])
        check("A3 文件真的被复制", len(list((root / "pub").glob("*"))) == 1, "")
        check("A4 ★ 幂等：同内容再发一次 URL 不变", pub.publish(f) == u, "")
        check("A5 不同内容 -> 不同文件",
              pub.publish(_mk(root, "另.mp4", b"y" * 128)) != u, "")
        m = pub.manifest()
        check("A6 ★★ 清单以磁盘为准（2 条）", len(m["entries"]) == 2,
              str(len(m["entries"])))


def _no_base():
    print(); print("B. 缺公网基址")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        f = _mk(root)
        pub = StagingPublisher(root / "pub")
        try:
            pub.publish(f)
            check("B1 ★★★ 缺 base 必须报错（不许返回本地 URL）", False, "没报错")
        except PublishError as e:
            check("B1 ★★★ 缺 base 必须报错（不许返回本地 URL）", True, "")
            check("B2 报错里说清怎么办", "public_base" in str(e) or "公网" in str(e),
                  str(e)[:60])
        check("B3 ★ 报错前文件已暂存（不白干）",
              len(list((root / "pub").glob("*"))) == 1, "")
        check("B4 不存在的文件 -> 明确报错", True)
        try:
            pub.publish(root / "没有这个.mp4")
            check("B4 不存在的文件 -> 明确报错", False, "没报错")
        except PublishError:
            check("B4 不存在的文件 -> 明确报错", True, "")


def _env():
    print(); print("C. 环境探测")
    check("C1 缺隧道工具时返回列表（不抛）",
          isinstance(StagingPublisher.missing_tunnel_tools(), list), "")
    check("C2 未设环境变量 -> 空串", public_base_from_env() == "",
          public_base_from_env())


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 70); print("暂存发布 —— 自检"); print("=" * 70)
    _stage(); _no_base(); _env()
    n = sum(1 for _, ok, _ in _results if ok)
    print()
    for name, ok, detail in _results:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 70); print(f"结果：{n}/{len(_results)} 通过")
    return 0 if n == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
