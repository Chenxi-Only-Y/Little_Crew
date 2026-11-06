"""`tools/export_evidence.py` 自检 —— 打一个包，然后**用包里自带的 verify.py** 复核。

关键点：**包里的人不需要我们的代码**，所以要按"外部人"的方式验：
子进程跑 `verify.py`，只看退出码。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def _load(root: Path, rel: str):
    spec = importlib.util.spec_from_file_location(rel, root / "tools" / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                    # type: ignore[union-attr]
    return mod


def _fake_episode(root: Path) -> Path:
    wd = root / "ep1"
    (wd / "delivery").mkdir(parents=True)
    (wd / "cache").mkdir()
    png = wd / "cache" / "a.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 5000)
    h = "sha256:" + hashlib.sha256(png.read_bytes()).hexdigest()
    (wd / "delivery" / "delivery.evidence.json").write_text(json.dumps({
        "artifactHashes": {"a.png": h}}), encoding="utf-8")
    (wd / "delivery" / "episode.master.json").write_text(json.dumps({
        "shots": [{"shotId": "S1", "artifacts": ["a.png"]}]}), encoding="utf-8")
    (wd / "delivery" / "image_review.json").write_text(json.dumps({
        "kind": "image.review", "total": 1, "findings": [],
        "notReviewed": ["画面质量"]}), encoding="utf-8")
    return wd


def _run_verify(bundle: Path) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(bundle / "verify.py"), str(bundle)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("证据包导出 —— 自检")
    print("=" * 66)
    root = Path(__file__).resolve().parents[3]
    exp = _load(root, "export_evidence.py")

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        wd = _fake_episode(base)
        out = base / "bundle"
        m = exp.build(wd, out, with_images=True)

        print(); print("A. 包结构")
        check("A1 ★ 有 manifest / README / verify.py",
              all((out / n).is_file()
                  for n in ("manifest.json", "README.md", "verify.py")), "")
        check("A2 ★★ 证据 json 都进去了",
              (out / "delivery" / "image_review.json").is_file(), "")
        check("A3 ★ 产物也进去了", (out / "artifacts" / "a.png").is_file(), "")
        man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        check("A4 ★★★ manifest 覆盖 README 与 verify.py",
              "README.md" in man["files"] and "verify.py" in man["files"],
              str(sorted(man["files"])[:3]))

        print(); print("B. 外部人复核（子进程跑包内 verify.py）")
        rc, log = _run_verify(out)
        check("B1 ★★★ 干净包 -> PASS（退出码 0）", rc == 0, log[-200:])
        check("B2 ★★ 输出里报了 manifest 校验结果",
              "manifest" in log, log[-160:])

        print(); print("C. 篡改检测")
        (out / "delivery" / "image_review.json").write_text("{}", encoding="utf-8")
        rc2, log2 = _run_verify(out)
        check("C1 ★★★ 改了证据 json -> FAIL（退出码非 0）", rc2 != 0, log2[-160:])
        check("C2 ★★ 明确指出哪个文件被改",
              "image_review.json" in log2, log2[-200:])

        print(); print("D. 篡改产物图")
        m2 = exp.build(wd, base / "bundle2", with_images=True)
        (base / "bundle2" / "artifacts" / "a.png").write_bytes(b"tampered")
        rc3, log3 = _run_verify(base / "bundle2")
        check("D1 ★★★ 改了产物 -> FAIL", rc3 != 0, log3[-160:])
        check("D2 ★★ README 写明「没审什么」",
              "notReviewed" in (out / "README.md").read_text(encoding="utf-8")
              or "没有审" in (out / "README.md").read_text(encoding="utf-8"), "")

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
