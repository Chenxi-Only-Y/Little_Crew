"""`tools/save_voice.py` 自检 —— 只验**防护路径**，不真的存音色（避免污染 voices/）。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def _load(root: Path):
    spec = importlib.util.spec_from_file_location("save_voice",
                                                  root / "tools" / "save_voice.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                    # type: ignore[union-attr]
    return mod


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("存音色工具 —— 自检")
    print("=" * 66)
    root = Path(__file__).resolve().parents[3]
    mod = _load(root)

    print(); print("A. 防护路径")
    r = mod.save(str(root / "不存在.wav"), "测试角色")
    check("A1 ★★★ 参考音不存在 -> 明确失败", r["ok"] is False and "不存在" in r["error"],
          r["error"])
    check("A2 ★★ 失败时不留 path", r["path"] == "", r["path"])

    print(); print("B. 列出音色")
    rc = mod.main(["--list"])
    check("B1 ★★ --list 正常返回", rc == 0, str(rc))

    print(); print("C. 参数校验")
    rc2 = mod.main([])
    check("C1 ★★ 缺参数 -> 非零退出且提示", rc2 == 2, str(rc2))

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
