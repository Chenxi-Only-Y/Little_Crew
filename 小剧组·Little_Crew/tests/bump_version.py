"""一次性维护脚本：版本号提升 + 测试计数同步 + 一致性校验。

用法（从 platform 目录）： python tests/_bump.py 0.4.0-alpha.1 0.4.0-alpha.2

**本脚本是一次性工具**，完成后可删除。之所以用 Python 而非 PowerShell：
本机 pwsh 5.1 的路径拼接（`Join-Path` 参数个数、`[IO.Path]::Combine` 与
中文路径交互）反复出问题，而 Python 的 pathlib 行为明确可控。
"""

from __future__ import annotations

import pathlib
import re
import sys

BASE = pathlib.Path(__file__).resolve().parents[1]

#: 需要同步版本号的文件
VERSION_FILES = [
    "pyproject.toml",
    "config/project.toml",
    "src/little_crew/version.py",
    "README.md",
]

#: 需要同步测试计数的文件
COUNT_FILES = ["README.md", "ARCHITECTURE.md", "STRUCTURE.md"]


def pep440(v: str) -> str:
    """0.4.0-alpha.2 -> 0.4.0a2"""
    m = re.match(r"^(\d+\.\d+\.\d+)-(alpha|beta|rc)\.(\d+)$", v)
    if not m:
        return v
    tag = {"alpha": "a", "beta": "b", "rc": "rc"}[m.group(2)]
    return f"{m.group(1)}{tag}{m.group(3)}"


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    old, new = sys.argv[1], sys.argv[2]
    old_p, new_p = pep440(old), pep440(new)

    print("=" * 70)
    print(f"版本提升: {old} -> {new}   (PEP440: {old_p} -> {new_p})")
    print("=" * 70)

    for rel in VERSION_FILES:
        p = BASE / rel
        if not p.exists():
            print(f"  skip (不存在) {rel}")
            continue
        t = p.read_text(encoding="utf-8")
        o = t
        t = t.replace(old, new).replace(old_p, new_p)
        if t != o:
            p.write_text(t, encoding="utf-8")
            print(f"  updated  {rel}")
        else:
            print(f"  nochange {rel}")

    print()
    print("=" * 70)
    print("测试计数同步: 190 -> 209")
    print("=" * 70)
    subs = [
        ("190 项功能测试", "209 项功能测试"),
        ("功能用例 190/190", "功能用例 209/209"),
        ("功能用例 190", "功能用例 209"),
        ("190/190", "209/209"),
        ("编译器与能力矩阵                 94 项", "编译器与能力矩阵                 94 项"),
    ]
    for rel in COUNT_FILES:
        p = BASE / rel
        if not p.exists():
            continue
        t = p.read_text(encoding="utf-8")
        o = t
        for a, b in subs:
            t = t.replace(a, b)
        if t != o:
            p.write_text(t, encoding="utf-8")
            print(f"  updated  {rel}")
        else:
            print(f"  nochange {rel}")

    # ---------- 一致性校验 ----------
    print()
    print("=" * 70)
    print("一致性校验")
    print("=" * 70)

    sys.path.insert(0, str(BASE / "src"))
    import tomllib

    from little_crew.version import PEP440_VERSION, VERSION, version_summary

    ok = True

    def chk(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(f"  [{'v' if cond else 'X'}] {name}" + (f"   {detail}" if detail else ""))
        if not cond:
            ok = False

    chk("version.py VERSION", VERSION == new, VERSION)
    chk("version.py PEP440_VERSION", PEP440_VERSION == new_p, PEP440_VERSION)

    pp = tomllib.loads((BASE / "pyproject.toml").read_text(encoding="utf-8"))
    chk("pyproject.toml（须 PEP440 写法）", pp["project"]["version"] == new_p,
        pp["project"]["version"])

    cp = tomllib.loads((BASE / "config" / "project.toml").read_text(encoding="utf-8"))
    chk("config/project.toml", cp["project"]["version"] == new, cp["project"]["version"])

    cl = (BASE / "CHANGELOG.md").read_text(encoding="utf-8")
    chk(f"CHANGELOG 含 [{new}]", f"## [{new}]" in cl)
    # 同版本自检（幂等性验证）时 old == new，这条检查没有意义，跳过
    if old != new:
        chk(f"CHANGELOG 仍含 [{old}]（历史保留）", f"## [{old}]" in cl)

    chk("包路径指向 little_crew",
        pp["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/little_crew"])

    print()
    print(f"  协议版本: {version_summary()['protocols']}")
    print()
    print("  结果:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
