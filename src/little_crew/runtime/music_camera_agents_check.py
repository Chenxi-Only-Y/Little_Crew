"""composer / camera_operator 两个职位的自检（真跑 ffmpeg，不联网）。"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from little_crew.runtime.camera_operator_agent import CameraOperatorAgent
from little_crew.runtime.composer_agent import ComposerAgent
from little_crew.agent.protocol import Budget, CallEnvelope

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def _ffmpeg() -> str:
    for c in (r"D:\AI\FFmpeg\bin\ffmpeg.exe", "ffmpeg"):
        if c == "ffmpeg" or Path(c).is_file():
            return c
    return ""


class _Shot:
    def __init__(self, sid, secs):
        self.shot_id, self.duration_sec = sid, secs


def _env(agent, payload):
    return CallEnvelope.root(agent, "x", payload, Budget.unlimited(),
                            callee_kind="agent")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("配乐 / 摄影（两个职位）—— 自检")
    print("=" * 66)
    ff = _ffmpeg()
    if not ff:
        print("  跳过：找不到 ffmpeg")
        return 0

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        print(); print("A. 配乐（shot.list -> shot.music）")
        ag = ComposerAgent(ffmpeg=ff)
        shots = [_Shot("S1", 5.0), _Shot("S2", 7.0)]
        r = ag(envelope=_env("composer", {"shot.list": shots, "outDir": str(d)}))
        plan = r.output["shot.music"]
        check("A1 ★★★ 产出 shot.music", plan["kind"] == "shot.music", plan["kind"])
        check("A2 ★★★ 时长够（>= 总时长）", plan["seconds"] >= 12.0 - 0.5,
              f"{plan['seconds']} vs 12.0")
        check("A3 ★★★ 文件真的在", Path(plan["path"]).is_file(), plan["path"])
        check("A4 ★★★ source 如实标注（垫音不能冒充配乐）",
              plan["source"].startswith(("asset:", "generated:")), plan["source"])
        check("A5 ★★ 写明没做什么",
              any("作曲" in x for x in plan["notDone"]), str(plan["notDone"]))
        r2 = ag(envelope=_env("composer", {"shot.list": []}))
        check("A6 ★★★ 没有时长 -> 明确失败", r2.ok is False, "")

        print(); print("B. 摄影（shot.image -> shot.video）")
        # 造一张图
        img = d / "a.png"
        subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "color=c=gray:s=320x240:d=1",
                        "-frames:v", "1", str(img)], check=True)
        cam = CameraOperatorAgent(backend="kenburns", ffmpeg=ff,
                                  size=(320, 240), fps=12)
        r3 = cam(envelope=_env("camera_operator", {
            "shot.list": [_Shot("S1", 2.0), _Shot("S2", 2.0)],
            "images": {"S1": str(img)}, "outDir": str(d / "clips")}))
        pv = r3.output["shot.video"]
        check("B1 ★★★ 产出 shot.video", pv["kind"] == "shot.video", pv["kind"])
        check("B2 ★★★ 成功一段（S1）", pv["clipCount"] == 1, str(pv["clipCount"]))
        check("B3 ★★★ 缺图的镜记为 failed（不静默丢）",
              any(x["shotId"] == "S2" for x in pv["failed"]), str(pv["failed"])[:60])
        check("B4 ★★ 片段文件存在",
              all(Path(c["path"]).is_file() for c in pv["clips"]), "")
        check("B5 ★★ 写明没做什么（口型）",
              any("口型" in x for x in pv["notDone"]), str(pv["notDone"]))
        r4 = cam(envelope=_env("camera_operator", {"shot.list": []}))
        check("B6 ★★★ 没有镜头表 -> 明确失败", r4.ok is False, "")

    print(); print("C. 接线（结构断言：防止「写了但没接」）")
    ep = (Path(__file__).resolve().parent / "episode.py").read_text(encoding="utf-8")
    check("C1 ★★★ episode 里有 composer 调用点", "run_stage(\n" in ep
          and '"composer", "shot.music"' in ep, "")
    check("C2 ★★★ episode 里有 camera_operator 调用点",
          '"camera_operator", "shot.video"' in ep, "")
    check("C3 ★★ 配乐失败要打印（否则「没接上」与「没跑」分不出）",
          "配乐职位未产出" in ep and "配乐职位未绑定" in ep, "")
    check("C4 ★★★ camera_operator 默认关闭（env 开关）",
          "LITTLE_CREW_CLIPS_AGENT" in ep, "")

    n = sum(1 for _, ok, _ in _r if ok)
    print()
    for name, ok, detail in _r:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 66)
    print(f"结果：{n}/{len(_r)} 通过")
    return 0 if n == len(_r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
