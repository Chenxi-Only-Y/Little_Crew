"""成本测算（B2，2026-11-13）：把一次运行里**可数的调用**统计出来。

## 只统计"实测得到的"，不编价格

| 指标 | 从哪来 |
|---|---|
| 计划镜数 / 实算出图 / 缓存命中 | `delivery/run.report.json` |
| 云端出图次数 | `checkpoint/checkpoint.json` 里本集 `jobId` 前缀的记录数 |
| 视觉闸调用次数 | `delivery/vision_review.json` 的 `rows[].attempts` 合计 |
| 封面调用次数 | `delivery/cover.json` 的 `judge`（含重试） |
| LLM/agent 调用次数 | `trace.json` 的 `spans` |
| 成片时长 / 体积 | `delivery/episode.master.json` + mp4 |

**价格不写死**：`--price-image` / `--price-vlm` 由使用方按自己的账单填；
脚本给出"按这些单价，这一集现金成本是多少"。

用法：

```powershell
$env:PYTHONPATH="<项目>\\src"
python tools/cost_report.py --workdir outputs/_EFCLOUD1 --episode EF-CLOUD-2 \\
    --price-image 0.4 --price-vlm 0.01
```
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def collect(workdir: Path, episode: str) -> dict:
    out: dict = {"workdir": str(workdir), "episode": episode}
    rep_f = workdir / "delivery" / "run.report.json"
    if rep_f.is_file():
        r = json.loads(rep_f.read_text(encoding="utf-8"))
        out.update({"ok": r.get("ok"), "plannedShots": r.get("plannedShots"),
                    "rendered": r.get("rendered"), "cacheHits": r.get("cacheHits"),
                    "failed": r.get("failed"), "videoSeconds": r.get("totalSeconds"),
                    "reason": (r.get("reason") or "")[:120]})
    ck_f = workdir / "checkpoint" / "checkpoint.json"
    if ck_f.is_file():
        ck = json.loads(ck_f.read_text(encoding="utf-8"))
        recs = [x for x in (ck.get("records") or [])
                if str(x.get("jobId") or "").startswith(f"G-{episode}-")]
        out["imageCalls"] = len(recs)
        out["imageFailed"] = sum(1 for x in recs if x.get("state") != "succeeded")
    vr_f = workdir / "delivery" / "vision_review.json"
    if vr_f.is_file():
        v = json.loads(vr_f.read_text(encoding="utf-8"))
        rows = v.get("rows") or []
        out["vlmReviewed"] = v.get("reviewedCount")
        out["vlmCalls"] = sum(int(r.get("attempts") or 1) for r in rows)
        out["vlmTotalShots"] = v.get("total")
    cov_f = workdir / "delivery" / "cover.json"
    if cov_f.is_file():
        c = json.loads(cov_f.read_text(encoding="utf-8"))
        out["coverMode"] = c.get("mode")
        out["coverOk"] = c.get("ok")
        j = c.get("judge") or {}
        # 一次成功 = 1 次；失败后重试 = 2 次（脚本里硬编码重试 1 次）
        out["coverVlmCalls"] = 1 if j.get("ok") else (2 if j else 0)
    tr_f = workdir / "trace.json"
    if tr_f.is_file():
        t = json.loads(tr_f.read_text(encoding="utf-8"))
        spans = t.get("spans") or []
        out["agentCalls"] = len(spans)
        from collections import Counter
        out["agentBreakdown"] = dict(Counter(str(s.get("callee")) for s in spans))
    em_f = workdir / "delivery" / "episode.master.json"
    if em_f.is_file():
        em = json.loads(em_f.read_text(encoding="utf-8"))
        out["masterSeconds"] = em.get("totalSeconds")
        out["masterShots"] = em.get("shotCount")
    mp4 = sorted(workdir.rglob("episode.master.mp4"))
    if mp4:
        out["mp4MB"] = round(mp4[0].stat().st_size / 1024 / 1024, 2)
    return out


def money(rec: dict, *, price_image: float, price_vlm: float,
          price_agent: float) -> dict:
    img = int(rec.get("imageCalls") or 0)
    vlm = int(rec.get("vlmCalls") or 0) + int(rec.get("coverVlmCalls") or 0)
    ag = int(rec.get("agentCalls") or 0)
    total = img * price_image + vlm * price_vlm + ag * price_agent
    secs = float(rec.get("masterSeconds") or 0)
    return {"imageCalls": img, "vlmCalls": vlm, "agentCalls": ag,
            "cny": round(total, 3),
            "cnyPerVideoMinute": round(total / (secs / 60.0), 3) if secs else None}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--episode", required=True)
    ap.add_argument("--price-image", type=float, default=0.05,
                    help="每次云端出图单价（元）；本项目实测口径 ¥0.05/张")
    ap.add_argument("--cloud-images", type=int, default=-1,
                    help="覆盖计费出图次数（-1=按 checkpoint 全计）。"
                         "本地跑图应为 0（不花钱），云端才算钱")
    ap.add_argument("--price-vlm", type=float, default=0.01)
    ap.add_argument("--price-agent", type=float, default=0.002)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    wd = Path(args.workdir)
    if not wd.is_absolute():
        wd = ROOT / wd
    rec = collect(wd, args.episode)
    if args.cloud_images >= 0:
        rec["imageCalls"] = int(args.cloud_images)      # 覆盖为"真正计费的出图次数"
        rec["billableOverride"] = True
    rec["prices"] = {"image": args.price_image, "vlm": args.price_vlm,
                     "agent": args.price_agent}
    rec["cost"] = money(rec, price_image=args.price_image,
                        price_vlm=args.price_vlm, price_agent=args.price_agent)

    print(f"集 {args.episode} · {wd.name}")
    print(f"  镜数：计划 {rec.get('plannedShots')} · 实算 {rec.get('rendered')} "
          f"· 缓存命中 {rec.get('cacheHits')} · 失败 {rec.get('failed')}")
    print(f"  调用：出图 {rec.get('imageCalls')} 次 · 视觉/封面 {rec['cost']['vlmCalls']} 次 "
          f"· agent {rec.get('agentCalls')} 次")
    print(f"  成片：{rec.get('masterSeconds')}s · {rec.get('mp4MB')}MB · ok={rec.get('ok')}")
    print(f"  现金（按 ¥{args.price_image}/图、¥{args.price_vlm}/判图、"
          f"¥{args.price_agent}/agent"
          + ("（计费图数已覆盖）" if rec.get("billableOverride") else "") +
          f"）：¥{rec['cost']['cny']}"
          f" · 每分钟成片 ¥{rec['cost']['cnyPerVideoMinute']}")
    if args.json_out:
        p = Path(args.json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  已写 {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
