"""剧本**分批生成**实验：一次 8 场 vs 分 3 批（3+3+2）（2026-11-08）。

## 依据

§6.18 实测：一次要求 8 场，`deepseek-flash` 只给 7.1 场、`mimo` 7.8 场 ——
**连更好的模型也填不满**。分镜那边早就给出同一条教训（§5.x：一次喂八场就截断）。

## 设计

同一前提、同一模型，两种粒度：

    A 一次 8 场（现状）
    B 分 3 批：先 3 场 -> 带上已写的场次再写 3 场 -> 最后 2 场

判据：**场数齐全率**（8/8？）、结构完整率、耗时、台词数。

## 纪律

- 同一模型（deepseek-flash），避免混淆模型差异
- 每批都要求**接着已有的场次写**，并给出已用场号（防止重复 S01）
- 输出只要 JSON，不要解释（避免推理吃光 token）
"""
import json, statistics as st, sys, time
from pathlib import Path

P = Path(r"D:\AI\ds.8\Little_Crew")
sys.path.insert(0, str(P / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from little_crew.agent.llm import LlmClient

MODEL = "deepseek-flash"
NEED = 8
REPEATS = int(sys.argv[1]) if len(sys.argv) > 1 else 2

PREMISE = """题材：北方农村，冬天的集市。
人物：阿杏（十六岁，独生女）、父亲（腿有旧伤，脾气硬）、
      旅人（外乡人，话少，来路不明）、老奶奶（邻居，爱管闲事）。
要求：一集讲"父亲病倒、阿杏决定留下"这件事；
      要有铺陈、要有一次冲突、要有一个安静的收尾；
      对白要短，不要说满；结局不要解释。"""

SCHEMA = """只输出 JSON（不要解释、不要 markdown）：
{"scenes":[{"scene_id":"S01","heading":"内景·小屋·夜",
"characters":["阿杏"],"text":"本场发生了什么（1-2 句）",
"dialogue":[{"character":"阿杏","line":"台词"}]}]}"""


def _ask(client, prompt: str) -> dict:
    txt = client.chat(prompt, system="你是漫剧编剧，只输出 JSON。",
                      temperature=0.7, json_mode=True)
    raw = txt if isinstance(txt, str) else getattr(txt, "text", "")
    i, j = raw.find("{"), raw.rfind("}")
    if i < 0 or j <= i:
        return {}
    try:
        return json.loads(raw[i:j + 1])
    except Exception:                                         # noqa: BLE001
        return {}


def _scenes(obj) -> list[dict]:
    ss = obj.get("scenes") if isinstance(obj, dict) else None
    return [s for s in (ss or ()) if isinstance(s, dict)]


def gen_one_shot(client) -> dict:
    t0 = time.time()
    prompt = (f"【要求】改编成 {NEED} 场以内的漫剧分场剧本（**要写满 {NEED} 场**）。\n"
              f"【素材】\n{PREMISE}\n\n{SCHEMA}")
    ss = _scenes(_ask(client, prompt))
    return {"arm": "A 一次 8 场", "scenes": ss, "seconds": round(time.time() - t0, 1)}


def gen_batched(client, batches=(3, 3, 2)) -> dict:
    t0 = time.time()
    got: list[dict] = []
    for bi, n in enumerate(batches):
        done = [f"{s.get('scene_id')}({s.get('heading','')})" for s in got]
        prompt = (
            f"【进度】已有 {len(got)} 场，现在**只写接下来的 {n} 场**。\n"
            + (f"【已写场次】{'; '.join(done)}\n" if got else "")
            + f"【要求】整集 {NEED} 场，必须接着往下写，"
              f"场号从 S{len(got)+1:02d} 开始连续编号，不要重复已写的场。\n"
            + f"【素材】\n{PREMISE}\n\n{SCHEMA}")
        ss = _scenes(_ask(client, prompt))
        got.extend(ss[:n])
    return {"arm": "B 分 3 批", "scenes": got,
            "seconds": round(time.time() - t0, 1)}


def score(rec) -> dict:
    ss = rec["scenes"]
    need = ("scene_id", "heading", "characters", "text")
    complete = sum(1 for s in ss if all(str(s.get(k) or "").strip() for k in need))
    ids = [str(s.get("scene_id") or "") for s in ss]
    dup = len(ids) - len(set(ids))
    dlg = sum(len(s.get("dialogue") or ()) for s in ss)
    return {**rec, "n": len(ss), "complete": complete, "dup": dup,
            "dialogue": dlg, "fill": round(len(ss) / NEED, 3)}


def main() -> int:
    out = Path(__file__).parent / "raw.json"
    recs = []
    for i in range(REPEATS):
        for arm in ("A", "B"):
            client = LlmClient(MODEL)
            r = gen_one_shot(client) if arm == "A" else gen_batched(client)
            s = score(r)
            recs.append({**s, "repeat": i + 1})
            print(f"  [{i+1}/{REPEATS}] {s['arm']:10s} 场={s['n']:2d}/{NEED} "
                  f"结构完整={s['complete']:2d} 重复场号={s['dup']} "
                  f"台词={s['dialogue']:2d} {s['seconds']:5.0f}s")
    out.write_text(json.dumps(recs, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n  === 汇总 ===")
    for arm in ("A 一次 8 场", "B 分 3 批"):
        rs = [r for r in recs if r["arm"] == arm]
        if not rs:
            continue
        print(f"  {arm:10s} 场数均 {st.mean([r['n'] for r in rs]):4.1f}/{NEED} · "
              f"结构完整 {st.mean([r['complete'] for r in rs]):4.1f} · "
              f"重复场号 {st.mean([r['dup'] for r in rs]):.1f} · "
              f"耗时 {st.mean([r['seconds'] for r in rs]):3.0f}s")
    print(f"\n  原始数据: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
