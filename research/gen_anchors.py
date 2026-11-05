"""一次生成 6 张定妆图 -> assets/characters/<名字>/anchor.png

提示词直接取 sheet.json 的 promptFragment（已有），后面接定妆照风格后缀。
调用 skill 的 localgen.py（中文必须走它，PowerShell 自己拼请求会把汉字变 ?）。
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
P = Path(r"D:\AI\ds.8\Little_Crew")
SRC = P / "outputs" / "ep_white" / "assets" / "characters"
DST = P / "assets" / "characters"
LG = r"C:\Users\OnlyY\.dsh\skills\local-gen\scripts\localgen.py"
SUFFIX = "，正面半身定妆照，中性灰色背景，柔和均匀布光，清晰五官，电影质感"

DST.mkdir(parents=True, exist_ok=True)
done, failed = [], []
for d in sorted(SRC.iterdir()):
    f = d / "sheet.json"
    if not f.is_file():
        continue
    name = d.name
    sheet = json.loads(f.read_text(encoding="utf-8"))
    frag = str(sheet.get("promptFragment") or "").strip()
    if not frag:
        failed.append(f"{name}: 没有 promptFragment")
        continue
    out = DST / name
    out.mkdir(parents=True, exist_ok=True)
    print(f"  [{name}] 生成中…（{frag[:28]}…）", flush=True)
    cmd = [sys.executable, LG, "image", frag + SUFFIX,
           "--out", str(out), "--size", "768x1024", "--seed", "7"]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=1800)
    saved = ""
    for line in (r.stdout or "").splitlines():
        if line.startswith("SAVED "):
            saved = line[6:].strip()
    if not saved or not Path(saved).is_file():
        failed.append(f"{name}: 未产出（{(r.stdout or r.stderr or '')[-160:]}）")
        print(f"     失败", flush=True)
        continue
    shutil.copyfile(saved, out / "anchor.png")
    shutil.copyfile(f, out / "sheet.json")
    done.append(name)
    print(f"     ✅ {out / 'anchor.png'}", flush=True)

print()
print(f"  成功 {len(done)}: {done}")
print(f"  失败 {len(failed)}: {failed}")
