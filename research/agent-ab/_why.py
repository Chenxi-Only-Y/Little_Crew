import json, pathlib, sys
sys.path.insert(0, r"D:\AI\ds.8\Little_Crew\src")
sys.stdout.reconfigure(encoding="utf-8")
R = pathlib.Path(r"D:\AI\ds.8\Little_Crew\research\agent-ab")
raw = json.loads((R / "raw.json").read_text(encoding="utf-8"))
sys.path.insert(0, str(R))
import run_ab
S = run_ab.SCRIPTS
for r in raw:
    if r.get("ok"):
        continue
    name = r["script"]
    sd = S.get(name)
    print("  === %s  r%s  %s" % (name, r.get("repeat"), r["condition"]))
    print("      原因:", str(r.get("reason"))[:120])
    if sd is None:
        print("      (剧本对象没取到)"); continue
    print("      场数:", len(sd.scenes))
    for sc in sd.scenes:
        sid = sc.get("scene_id")
        txt = str(sc.get("text") or "")
        mark = "  <<< 就是这场" if sid == "S02" else ""
        print("        %s  字数 %3d  %s%s" % (sid, len(txt), txt[:46], mark))
    print()
