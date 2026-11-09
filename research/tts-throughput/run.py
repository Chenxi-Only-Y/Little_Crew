"""配音吞吐测量：CLI vs Server（2026-11-09）。

背景：整集 47 句，CLI 实测 5~9 秒/句（每句重载 2GB 模型）；
server 实测 65~90 秒/句（同期 ComfyUI 占 4.7GB 显存）。
上游文档称 Q8_0 Vulkan 应有 1.5~2.4x 实时 —— 差 100 倍说明是环境问题，要量不要猜。
"""
import json
import statistics as st
import sys
import time
from pathlib import Path

P = Path(r"D:\AI\ds.8\Little_Crew")
sys.path.insert(0, str(P / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from little_crew.runtime import tts_breeze as T

LINES = ["你不能死在这里。", "……别碰我。", "烧成这样还挑人。",
         "我跟你走。", "外头冷，进来说。"]
OUT = Path(__file__).parent / "out"


def bench(which: str) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for i, line in enumerate(LINES, 1):
        p = OUT / f"{which}_{i}.wav"
        t0 = time.perf_counter()
        if which == "cli":
            r = T.synth(line, p, voice="girl", instruction="年轻少女", seed=7)
        else:
            r = T.synth_server(line, p, voice="girl",
                               instruction="年轻少女", seed=7)
        dt = round(time.perf_counter() - t0, 2)
        rows.append({"i": i, "chars": len(line), "ok": bool(r.get("ok")),
                     "wall": dt, "audio": round(float(r.get("seconds") or 0), 2),
                     "backend": r.get("backend") or which})
        print(f"  {which} #{i}: {dt:6.2f}s 音频{rows[-1]['audio']:.2f}s "
              f"{'ok' if rows[-1]['ok'] else r.get('error')}")
    walls = [x["wall"] for x in rows]
    audios = [x["audio"] for x in rows]
    return {"backend": which, "n": len(rows),
            "wallMean": round(st.mean(walls), 2),
            "wallMedian": round(st.median(walls), 2),
            "audioSum": round(sum(audios), 2),
            "rtf": round(sum(audios) / sum(walls), 2) if sum(walls) else 0,
            "rows": rows}


def main() -> int:
    which = (sys.argv[1] if len(sys.argv) > 1 else "cli").lower()
    print(f"  测 {which} · server_alive={T.server_alive()} · "
          f"cli={T.exe_path().is_file()}")
    res = bench(which)
    print(f"\n  {which}: 均 {res['wallMean']}s/句 · 中位 {res['wallMedian']}s · "
          f"实时倍率 {res['rtf']}x")
    out = Path(__file__).parent / f"result_{which}.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  原始数据: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
