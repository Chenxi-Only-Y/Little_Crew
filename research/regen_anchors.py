"""只重出 age_band 被改过的角色（少女 / 阿杏）。"""
import json, shutil, subprocess, sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
P = Path(r"D:\AI\ds.8\Little_Crew")
BASE = P / "assets" / "characters"
LG = r"C:\Users\OnlyY\.dsh\skills\local-gen\scripts\localgen.py"
SUFFIX = "，正面半身定妆照，中性灰色背景，柔和均匀布光，清晰五官，电影质感"
TARGETS = ["少女", "阿杏"]
for name in TARGETS:
    d = BASE / name
    f = d / "sheet.json"
    j = json.loads(f.read_text(encoding="utf-8"))
    frag = str(j.get("promptFragment") or "").strip()
    print(f"  [{name}] {frag[:30]}…", flush=True)
    cmd = [sys.executable, LG, "image", frag + SUFFIX,
           "--out", str(d), "--size", "768x1024", "--seed", "7"]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=1800)
    saved = next((l[6:].strip() for l in (r.stdout or "").splitlines()
                  if l.startswith("SAVED ")), "")
    if saved and Path(saved).is_file():
        shutil.copyfile(saved, d / "anchor.png")
        print(f"     ✅ 已更新 {name}", flush=True)
    else:
        print(f"     ❌ 失败 {(r.stderr or '')[-120:]}", flush=True)
