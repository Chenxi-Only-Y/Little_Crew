"""量「跨场矛盾」：只从 shotList 的描述 + 场次 heading 里算，不需要 LLM。

三个可数检查：
  ① 首尾重复：场k 末镜 vs 场k+1 首镜 的描述相似度（高 = 断裂/重拍）
  ② 时间跳变：场 heading 的时间词 vs 镜头描述里的时间词冲突
  ③ 场次漏写：某场 0 镜（结构缺陷，已在达成率里）
"""
import json
import pathlib
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8")
R = pathlib.Path(r"D:\AI\ds.8\Little_Crew\research\agent-ab")
sys.path.insert(0, str(R))
sys.path.insert(0, r"D:\AI\ds.8\Little_Crew\src")
import run_ab  # noqa: E402

raw = json.loads((R / "raw.json").read_text(encoding="utf-8"))
SCRIPTS = run_ab.SCRIPTS

TIME_WORDS = {"夜": "night", "日": "day", "晨": "day", "午": "day",
              "黄昏": "dusk", "傍晚": "dusk", "凌晨": "night", "天亮": "day"}


def bigrams(s):
    s = str(s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)}


def sim(a, b):
    A, B = bigrams(a), bigrams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def analyse(rec):
    sl = rec.get("shotList") or []
    if not sl:
        return None
    by = defaultdict(list)
    for s in sl:
        by[s.get("sceneId")].append(s)
    ids = sorted(by)
    rep, tconf = [], 0
    for a, b in zip(ids, ids[1:]):
        if by[a] and by[b]:
            rep.append(sim(by[a][-1].get("description"),
                           by[b][0].get("description")))
    sd = SCRIPTS.get(rec["script"])
    if sd is not None:
        head = {str(sc.get("scene_id")): str(sc.get("heading") or "")
                for sc in sd.scenes}
        for sid, ss in by.items():
            h = head.get(sid, "")
            hw = next((TIME_WORDS[w] for w in TIME_WORDS if w in h), "")
            if not hw:
                continue
            for s in ss:
                d = str(s.get("description") or "")
                for w, tag in TIME_WORDS.items():
                    if w in d and tag != hw and (tag == "night" or hw == "night"):
                        tconf += 1
                        break
    return {"rep": sum(rep) / len(rep) if rep else 0.0, "tconf": tconf}


agg = defaultdict(list)
for r in raw:
    if not r.get("ok"):
        continue
    a = analyse(r)
    if a is None:
        continue
    arm = "B" if "多智能体" in r["condition"] else "A"
    agg[arm].append(a)

for arm in ("A", "B"):
    xs = agg[arm]
    if not xs:
        continue
    rep = sum(x["rep"] for x in xs) / len(xs)
    tc = sum(x["tconf"] for x in xs)
    print("  %s  n=%d   首尾相似度 %.3f   时间冲突 %d 次（均 %.2f）"
          % (arm, len(xs), rep, tc, tc / len(xs)))
