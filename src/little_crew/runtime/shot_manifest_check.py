"""`runtime/shot_manifest.py` 自检（2026-11-18，0.9.0）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("每镜资产记录（shot manifest）—— 自检")
    print("=" * 66)
    from little_crew.runtime import shot_manifest as SM

    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        sid = "SH-CH01-001-001"
        print(); print("A. 记录读写 / 版本")
        r1 = SM.upsert(wd, shot_id=sid, prompt="站台 夜景", model="m1",
                       anchor="/tmp/a.png", out="/tmp/o1.png", status="ok")
        check("A1 ★★★ 首次写入 version=1", r1["version"] == 1, str(r1["version"]))
        check("A2 ★★ 落盘到 assets/shots/<sid>/shot.json",
              SM.record_path(wd, sid).is_file(), str(SM.record_path(wd, sid)))
        check("A3 ★★ promptHash 非空", len(str(r1.get("promptHash") or "")) >= 8, "")
        r2 = SM.upsert(wd, shot_id=sid, prompt="站台 夜景", out="/tmp/o2.png")
        check("A4 ★★★ 同提示词 -> 版本不变", r2["version"] == 1, str(r2["version"]))
        r3 = SM.upsert(wd, shot_id=sid, prompt="站台 夜景 + 同空间同光线",
                       out="/tmp/o3.png", status="ok")
        check("A5 ★★★ 提示词变化 -> 版本+1", r3["version"] == 2, str(r3["version"]))
        check("A6 ★★★ 旧版进 history",
              bool(r3.get("history")) and r3["history"][-1]["version"] == 1, "")
        check("A7 ★★ 成本累加", abs(float(r3.get("costCny") or 0)) >= 0, "")

        print(); print("B. B2：只改一镜的 hint")
        b = SM.bump_hint(wd, sid, "保持与上一章同一空间/同一光线")
        check("B1 ★★★ 只改这一条记录（版本+1）", b["version"] == 3, str(b["version"]))
        check("B2 ★★★ hint 写进 prompt", "同一空间" in str(b.get("prompt")), "")
        check("B3 ★★★ 状态变 pending（待重渲）", b["status"] == "pending", str(b["status"]))
        check("B4 ★★ hint 记录在 meta", "同一空间" in str((b.get("meta") or {}).get("hint")), "")

        print(); print("C. 整集视图")
        v = SM.view(wd)
        check("C1 ★★★ 视图含该镜", sid in v["shots"], str(list(v["shots"])))
        check("C2 ★★★ pending -> 整集不完整",
              (not v["complete"]) and sid in v["missing"], str(v["missing"]))
        SM.upsert(wd, shot_id=sid, prompt=str(b.get("prompt")),
                  out="/tmp/o4.png", status="ok")
        v2 = SM.view(wd)
        check("C3 ★★★ 重渲成功后整集完整", v2["complete"] and not v2["missing"], "")
        check("C4 ★★ 缺记录 -> 视图 missing 反映",
              (not SM.load(wd, "SH-NOPE")) and SM.load(wd, sid) is not None, "")

    print(); print("D. manifest -> CachedBatch 适配（下游不改代码）")
    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        png = wd / "a.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 40)
        SM.upsert(wd, shot_id="SH-1", prompt="p1", out=str(png), status="ok")
        b = SM.to_batch(wd, episode_id="E1")
        check("D1 ★★★ 适配器产出整集 batch", len(b.outcomes) == 1, str(len(b.outcomes)))
        check("D2 ★★★ 产物字节可读（下游拿得到图）",
              bool(b.outcomes[0].artifacts), "")
        check("D3 ★★ graph_id 按集+镜命名（下游按前缀取）",
              "E1" in b.outcomes[0].graph_id and "SH-1" in b.outcomes[0].graph_id,
              b.outcomes[0].graph_id)
        SM.upsert(wd, shot_id="SH-2", prompt="p2", out="", status="pending")
        b2 = SM.to_batch(wd, episode_id="E1")
        check("D4 ★★★ pending 记录 -> 下游判失败（不会静默混入）",
              bool(b2.failed), "")

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
