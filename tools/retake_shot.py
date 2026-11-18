"""B2：只重渲一镜（0.9.0，shot manifest 架构）。

用法：
  python tools/retake_shot.py --workdir outputs/.../CH02 \
      --shot SH-CH02-001-001 --hint "保持上一章同空间/同背景/同光线"
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--shot", required=True)
    ap.add_argument("--hint", required=True)
    ap.add_argument("--ref-frame", default="",
                    help="上一章末帧参考图（图生图锁空间/光线，B2 闭环）")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    wd = Path(a.workdir)
    from little_crew.runtime import shot_manifest as SM
    rec = SM.load(wd, a.shot)
    if not rec:
        print(f"  ❌ 没有镜头记录 {a.shot}（先跑一次普通渲染生成 shot.json）")
        return 2
    old = dict(rec)
    new = SM.bump_hint(wd, a.shot, a.hint)
    print(f"  {a.shot}: v{old.get('version')} -> v{new.get('version')}（只改这一镜）")
    if a.dry_run:
        return 0
    from little_crew.runtime import cloud_render as CR
    fake = SimpleNamespace(shot_id=a.shot, description=str(new.get("prompt") or ""),
                           characters="")
    out_dir = wd / "assets" / "cloud_render"
    _ref = str(a.ref_frame or new.get("anchor") or "")
    res = CR.render_one(fake, _ref, out_dir,
                        model=str(new.get("model") or CR.DEFAULT_MODEL), extra="")
    if not res.get("ok"):
        SM.upsert(wd, shot_id=a.shot, prompt=str(new.get("prompt") or ""),
                  status="failed", meta={"error": str(res.get("error"))[:160]})
        print("  ❌ 重渲失败：", res.get("error"))
        return 1
    upd = SM.upsert(wd, shot_id=a.shot, prompt=str(new.get("prompt") or ""),
                    model=str(res.get("model") or ""),
                    anchor=str(res.get("anchor") or ""),
                    out=str(res.get("out") or ""), status="ok",
                    cost_cny=0.0 if res.get("reused") else 0.05,
                    meta={"retake": True, "reused": bool(res.get("reused"))})
    ev = wd / "delivery" / "retake-shot.json"
    ev.parent.mkdir(parents=True, exist_ok=True)
    ev.write_text(json.dumps({"kind": "boundary.retake_shot", "shotId": a.shot,
                              "oldVersion": old.get("version"),
                              "newVersion": upd.get("version"),
                              "hint": a.hint, "refFrame": str(a.ref_frame or ""), "out": upd.get("out"),
                              "reused": bool(res.get("reused")),
                              "costCny": 0.0 if res.get("reused") else 0.05,
                              "promptHash": upd.get("promptHash")},
                             ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  ✅ 重渲完成：{upd.get('out')} · 新版本 v{upd.get('version')} · 证据 {ev}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
