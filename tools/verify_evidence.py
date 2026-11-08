#!/usr/bin/env python3
"""独立校验一集的证据链：**重算每个产物的 sha256，与证据文件比对**。

## 为什么是"独立脚本"

`docs/00-项目策划书.md` §3.10.2 第三层：证据链要能**被第三方核验**。
所以这个脚本：

- **只用标准库**，不 import `little_crew` —— 不依赖被测系统的任何代码
- **自己找文件、自己算哈希**，不信证据文件里的自述
- 退出码：0 = 全部一致；1 = 有缺失或不一致

## 用法

    python tools/verify_evidence.py outputs/ep_white
    python tools/verify_evidence.py outputs/ep_white --json

## 它回答的问题

> **"这一集说它渲染了 40 个镜头、每个都有哈希 —— 这是真的吗？"**
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def find_json(workdir: Path, name: str):
    p = workdir / "delivery" / name
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def find_hashes(node, depth=0):
    """在证据 JSON 里递归找「shotId -> sha256:...」那张表。"""
    if depth > 4 or not isinstance(node, dict):
        return None
    vals = [v for v in node.values() if isinstance(v, str)]
    if len(node) >= 5 and vals and all(v.startswith("sha256:") for v in vals):
        return node
    for v in node.values():
        got = find_hashes(v, depth + 1)
        if got:
            return got
    return None


def locate(workdir: Path, fname: str) -> Path | None:
    """产物文件名是裸名，实际落在 cache/t*/<hash>/ 下 —— 递归找。"""
    direct = workdir / fname
    if direct.is_file():
        return direct
    for cand in workdir.rglob(fname):
        if cand.is_file():
            return cand
    return None


def check_fingerprint(root: Path, manifest: dict) -> list[str]:
    """校验 provider 指纹：文件在不在、记录完不完整、摘要对不对。

    返回问题列表（空 = 通过）。
    """
    import hashlib as _hl

    problems: list[str] = []
    spec = (manifest or {}).get("fingerprint")
    if not spec:
        return ["指纹：manifest 里没有 fingerprint 段"
                "（可能是旧包 —— 无法证明用了哪个模型）"]
    if spec.get("error"):
        return [f"指纹：导出时就出错了（{spec['error']}）"]
    rel = str(spec.get("file") or "")
    fp = root / rel if rel else None
    if fp is None or not fp.is_file():
        return [f"指纹：包里缺 {rel or '（未写文件名）'}"]
    try:
        j = json.loads(fp.read_text(encoding="utf-8"))
    except Exception as e:                                    # noqa: BLE001
        return [f"指纹：解析失败 {type(e).__name__}: {e}"]
    recs = j.get("records") or []
    if int(spec.get("total", -1)) != len(recs):
        problems.append(f"指纹：记录数不一致（manifest {spec.get('total')} vs 实际 {len(recs)}）")
    digest = "sha256:" + _hl.sha256(
        json.dumps(recs, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()
    if digest != spec.get("digest"):
        problems.append("指纹：**摘要不匹配**（记录被改过）")
    need = ("provider", "model", "requestHash")
    bad = [i for i, r in enumerate(recs) if any(not str(r.get(k) or "") for k in need)]
    if bad:
        problems.append(f"指纹：{len(bad)} 条记录缺必填字段（provider/model/requestHash）")
    miss = sum(1 for r in recs if not str(r.get("servedModel") or ""))
    if miss != int(spec.get("missingServedModel") or 0):
        problems.append(f"指纹：缺 servedModel 的条数不一致（manifest "
                        f"{spec.get('missingServedModel')} vs 实际 {miss}）")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="校验一集的证据链（重算 sha256）")
    ap.add_argument("workdir", nargs="?", default=".", help="某一集的输出目录")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = ap.parse_args(argv)
    try:                                        # 被 StringIO 捕获时没有 reconfigure
        sys.stdout.reconfigure(encoding="utf-8")              # type: ignore[attr-defined]
    except Exception:                                         # noqa: BLE001
        pass

    workdir = Path(args.workdir)
    ev = find_json(workdir, "delivery.evidence.json")
    master = find_json(workdir, "episode.master.json")
    if ev is None or master is None:
        print(f"  缺证据文件：{workdir}/delivery/ 下需要 "
              f"delivery.evidence.json 与 episode.master.json", file=sys.stderr)
        return 1

    # **优先用产物文件哈希**（artifactHashes: 文件名 -> sha256）。
    # 只有它才能回答"交付的东西有没有被改过"；
    # irHashes 是施工单哈希，与文件字节无关，**不能拿来比文件**。
    file_hashes = ev.get("artifactHashes") or {}
    # ---- ① 先按 manifest 校验包内每个文件（防夹带/防改说明）----
    # manifest 在**包根**（不是 delivery/ 下）—— 与证据文件不同位置
    _manp = workdir / "manifest.json"
    man = (json.loads(_manp.read_text(encoding="utf-8"))
           if _manp.is_file() else None)
    man_rows, man_bad = [], 0
    if man and isinstance(man.get("files"), dict):
        for rel, want in man["files"].items():
            f = workdir / rel
            if not f.is_file():
                man_rows.append({"file": rel, "status": "missing"})
                man_bad += 1
                continue
            # manifest 存纯 hex，`sha256_of` 带 `sha256:` 前缀 —— 统一去掉再比
            got_h = sha256_of(f).split(":", 1)[-1]
            want_h = str(want).split(":", 1)[-1]
            if got_h != want_h:
                man_rows.append({"file": rel, "status": "MISMATCH",
                                 "expected": want_h, "actual": got_h})
                man_bad += 1
            else:
                man_rows.append({"file": rel, "status": "ok"})

    hashes = find_hashes(ev) or {}
    rows, ok, bad, miss = [], 0, 0, 0
    for sh in (master.get("shots") or ()):
        sid = str(sh.get("shotId") or "")
        arts = sh.get("artifacts") or ()
        for a in arts:
            fname = a if isinstance(a, str) else str((a or {}).get("path", ""))
            want = file_hashes.get(fname, "") or ""
            f = locate(workdir, fname) if fname else None
            if f is None:
                miss += 1
                rows.append({"shotId": sid, "file": fname, "status": "missing"})
                continue
            got = sha256_of(f)
            if not want:
                rows.append({"shotId": sid, "file": fname, "status": "no-hash-recorded"})
                bad += 1
            elif got != want:
                rows.append({"shotId": sid, "file": fname, "status": "MISMATCH",
                             "expected": want, "actual": got})
                bad += 1
            else:
                ok += 1
                rows.append({"shotId": sid, "file": fname, "status": "ok"})

    note = ""
    if not file_hashes:
        note = ("证据里没有 artifactHashes（产物文件哈希）—— "
                "这一集是在补该字段之前生成的，只能核验\"有没有文件\"，"
                "不能核验\"文件有没有被改\"")
    # ---- ② provider 指纹（2026-11-08）----
    # 只有文件哈希还不够：它只能证明"文件没被换"，不能证明"记录本身完整"。
    fp_problems = check_fingerprint(workdir, man or {}) if man else \
        ["指纹：没有 manifest（不是证据包，无法校验指纹）"]
    total = len(rows)
    verdict = ("PASS" if (bad == 0 and miss == 0 and total > 0
                          and man_bad == 0 and not fp_problems)
               else "FAIL")
    result = {"kind": "evidence.verification", "workdir": str(workdir),
              "verdict": verdict, "total": total, "ok": ok,
              "mismatch": bad, "missing": miss,
              "shotsWithHash": len(hashes),
              "fileHashes": len(file_hashes), "note": note, "rows": rows,
              "manifestFiles": len(man_rows), "manifestBad": man_bad,
              "manifestRows": man_rows,
              "fingerprint": (man or {}).get("fingerprint") or {},
              "fingerprintProblems": fp_problems}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"  目录: {workdir}")
        print(f"  产物: {total} 个 · 一致 {ok} · 不一致 {bad} · 缺失 {miss}")
        if man_rows:
            print(f"  manifest：{len(man_rows)} 个文件 · 不一致/缺失 {man_bad}")
        else:
            print("  manifest：无（不是证据包，只按产物哈希校验）")
        print(f"  证据里 IR 哈希: {len(hashes)} 镜 · 产物文件哈希: {len(file_hashes)} 个")
        _fp = (man or {}).get("fingerprint") or {}
        if _fp and not _fp.get("error"):
            print(f"  provider 指纹: {_fp.get('total', 0)} 条 · "
                  f"提供方 {len(_fp.get('providers') or [])} 个 · "
                  f"缺 servedModel {_fp.get('missingServedModel', 0)} 条")
        for _p in fp_problems:
            print(f"    [指纹] {_p}")
        if note:
            print(f"  ⚠️ {note}")
        for r in man_rows:
            if r["status"] != "ok":
                print(f"    [manifest {r['status']}] {r['file']}")
        for r in rows:
            if r["status"] != "ok":
                print(f"    [{r['status']}] {r['shotId']}  {r['file']}")
        print(f"  结论: {verdict}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
