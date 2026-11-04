"""`tools/verify_evidence.py` 与「产物文件哈希」的自检 —— 全离线。

分两半：
  A 证据对象要真的记下 `artifactHashes`（不然脚本无从核验）
  B 独立校验脚本要能区分 ok / MISMATCH / missing / no-hash-recorded
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def _load_tool(root: Path):
    spec = importlib.util.spec_from_file_location(
        "verify_evidence", root / "tools" / "verify_evidence.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                    # type: ignore[union-attr]
    return mod


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("证据链可校验 —— 自检")
    print("=" * 66)
    root = Path(__file__).resolve().parents[3]

    print(); print("A. 证据对象要记产物文件哈希")
    from little_crew.runtime.episode import DeliveryEvidence

    check("A1 ★ 有 artifactHashes 字段",
          "artifactHashes" in DeliveryEvidence(episode_id="E").as_dict(), "")
    ev = DeliveryEvidence(episode_id="E")
    ev.artifact_hashes = {"a.png": "sha256:deadbeef"}
    d = ev.as_dict()
    check("A2 ★★ as_dict 会输出它",
          d["artifactHashes"] == {"a.png": "sha256:deadbeef"}, str(d.get("artifactHashes")))
    check("A3 与 irHashes 分开（不是同一张表）",
          d["irHashes"] != d["artifactHashes"], "")
    check("A4 默认空字典（旧证据兼容）",
          DeliveryEvidence(episode_id="E").as_dict()["artifactHashes"] == {}, "")

    print(); print("B. 独立脚本能分辨四种情况")
    mod = _load_tool(root)
    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        (wd / "delivery").mkdir()
        (wd / "cache" / "t1").mkdir(parents=True)
        good = wd / "cache" / "t1" / "ok.png"
        good.write_bytes(b"hello")
        h = "sha256:" + hashlib.sha256(b"hello").hexdigest()
        bad = wd / "cache" / "t1" / "bad.png"
        bad.write_bytes(b"tampered")
        master = {"shots": [
            {"shotId": "S1", "artifacts": ["ok.png"]},
            {"shotId": "S2", "artifacts": ["bad.png"]},
            {"shotId": "S3", "artifacts": ["gone.png"]},
        ]}
        (wd / "delivery" / "episode.master.json").write_text(
            json.dumps(master), encoding="utf-8")
        (wd / "delivery" / "delivery.evidence.json").write_text(json.dumps(
            {"artifactHashes": {"ok.png": h, "bad.png": h}}), encoding="utf-8")

        import io
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            rc = mod.main([str(wd), "--json"])
        finally:
            sys.stdout = old
        out = json.loads(buf.getvalue())
        st = {r["file"]: r["status"] for r in out["rows"]}
        check("B1 ★ 一致的判 ok", st.get("ok.png") == "ok", str(st))
        check("B2 ★★ 改过的判 MISMATCH", st.get("bad.png") == "MISMATCH", str(st))
        check("B3 ★ 找不到的判 missing", st.get("gone.png") == "missing", str(st))
        check("B4 有坏项时退出码非 0", rc != 0, str(rc))

        (wd / "delivery" / "delivery.evidence.json").write_text(
            json.dumps({"artifactHashes": {}}), encoding="utf-8")
        old = sys.stdout
        sys.stdout = io.StringIO()
        try:
            mod.main([str(wd), "--json"])
            note_out = sys.stdout.getvalue()
        finally:
            sys.stdout = old
        check("B5 ★★ 无文件哈希时明确说明（不假装通过）",
              "artifactHashes" in note_out, note_out[:60])

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
