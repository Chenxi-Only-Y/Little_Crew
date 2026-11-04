import json, pathlib, sys
sys.stdout.reconfigure(encoding="utf-8")
R = pathlib.Path(r"D:\AI\ds.8\Little_Crew\research\agent-ab")
sys.path.insert(0, str(R)); sys.path.insert(0, r"D:\AI\ds.8\Little_Crew\src")
import run_ab
raw = json.loads((R / "raw.json").read_text(encoding="utf-8"))
S = run_ab.SCRIPTS
TW = ("夜", "日", "晨", "午", "黄昏", "傍晚", "凌晨", "天亮")
shown = 0
for r in raw:
    if not r.get("ok") or "多智能体" not in r["condition"]:
        continue
    sd = S.get(r["script"])
    if sd is None:
        continue
    head = {str(sc.get("scene_id")): str(sc.get("heading") or "") for sc in sd.scenes}
    for s in (r.get("shotList") or []):
        h = head.get(s.get("sceneId"), "")
        d = str(s.get("description") or "")
        for w in TW:
            if w in d and ("夜" in h) != ("夜" in d) and w in ("夜", "日", "晨", "黄昏"):
                print("  场 %s（heading: %s）" % (s.get("sceneId"), h))
                print("    描述含「%s」: %s" % (w, d[:90]))
                shown += 1
                break
        if shown >= 5:
            break
    if shown >= 5:
        break
