"""`runtime/anchors` 自检 —— 纯文件系统，不联网。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from little_crew.runtime.anchors import discover, sets_for, summary

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("角色锚点发现 —— 自检")
    print("=" * 66)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        chars = root / "assets" / "characters"
        (chars / "少女").mkdir(parents=True)
        (chars / "少女" / "anchor.png").write_bytes(b"x")
        (chars / "旅人").mkdir()
        (chars / "旅人" / "anchor.png").write_bytes(b"y")
        (chars / "无名").mkdir()                       # 没 anchor.png
        (chars / "少女@5BF9").mkdir()                  # 同名不同 ID
        (chars / "少女@5BF9" / "anchor.png").write_bytes(b"z")

        a = discover(root / "assets")
        print(); print("A. 发现")
        check("A1 ★ 只收有 anchor.png 的", set(a) == {"少女", "旅人"}, str(sorted(a)))
        check("A2 ★★ 同名不同 ID 归到同名前缀", "少女@5BF9" not in a, str(sorted(a)))
        check("A3 路径是绝对可用字符串", all(Path(v).is_file() for v in a.values()), "")

        print(); print("B. 按出场角色匹配")
        s = sets_for(["少女", "旅人", "路人"], a)
        check("B1 ★ 只返回存在的两个", set(s) == {"少女", "旅人"}, str(sorted(s)))
        check("B2 ★★ 缺图角色不返回（不编路径）", "路人" not in s, "")
        check("B3 空锚点表 -> 空", sets_for(["少女"], {}) == {}, "")
        check("B4 空角色 -> 空", sets_for(None, a) == {}, "")

        print(); print("C. 摘要")
        check("C1 摘要带数量与名字", summary(a)["count"] == 2, str(summary(a)))

    with tempfile.TemporaryDirectory() as td:
        check("D1 目录不存在 -> 空表（不抛）", discover(Path(td) / "nope") == {}, "")

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
