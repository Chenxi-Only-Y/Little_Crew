"""按当前规则重算 6 个角色的外观并重出定妆图（story_seed='ep1'）。

为什么整体重算：存量 sheet 是**旧版规则**生成的（男性可穿长裙、无性别字段），
只补字段会留下自相矛盾的组合。重算后所有字段都来自同一版规则。
"""
import json, shutil, subprocess, sys
from pathlib import Path

sys.path.insert(0, r"D:\AI\ds.8\Little_Crew\src")
sys.stdout.reconfigure(encoding="utf-8")
from little_crew.agent.character_sheet import design_appearance  # noqa: E402

P = Path(r"D:\AI\ds.8\Little_Crew")
BASE = P / "assets" / "characters"
LG = r"C:\Users\OnlyY\.dsh\skills\local-gen\scripts\localgen.py"
SUFFIX = "，正面半身定妆照，中性灰色背景，柔和均匀布光，清晰五官，电影质感"
SEED = "ep1"

for d in sorted(BASE.iterdir()):
    f = d / "sheet.json"
    if not f.is_file():
        continue
    j = json.loads(f.read_text(encoding="utf-8"))
    name = j["name"]
    ap = design_appearance(name, story_seed=SEED)
    j["appearance"] = ap.as_dict()
    j["promptFragment"] = ap.prompt_fragment()
    j["storySeed"] = SEED
    f.write_text(json.dumps(j, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  [{name}] {j['promptFragment'][:44]}…", flush=True)

    cmd = [sys.executable, LG, "image", j["promptFragment"] + SUFFIX,
           "--out", str(d), "--size", "768x1024", "--seed", "7"]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=1800)
    saved = next((l[6:].strip() for l in (r.stdout or "").splitlines()
                  if l.startswith("SAVED ")), "")
    if saved and Path(saved).is_file():
        shutil.copyfile(saved, d / "anchor.png")
        Path(saved).unlink(missing_ok=True)
        print("     ✅", flush=True)
    else:
        print("     ❌", (r.stderr or "")[-100:], flush=True)
