"""配音轨自检：抽台词 · 挂镜头 · 合成+对齐（用假后端，不跑真 TTS）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from little_crew.runtime.voice_track import assign_to_shots, build, parse_dialogue

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


class _Shot:
    def __init__(self, sid, scene, secs):
        self.shot_id, self.scene_id, self.duration_sec = sid, scene, secs


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("配音轨（抽台词 -> 挂镜头 -> 合成对齐）—— 自检")
    print("=" * 66)

    print(); print("A. 抽台词")
    t = '汤溅出来「别碰」。少女：「我不去。」\n父亲：你走吧。\n母亲说道：“别去了。”'
    bare = parse_dialogue(t)
    check("A1 ★★ 没白名单时会误抽动作（这是已知弱点）",
          len(bare) > 2, str([x["character"] for x in bare]))
    guarded = parse_dialogue(t, known={"少女", "父亲", "母亲"})
    names = [x["character"] for x in guarded]
    check("A2 ★★★ 给白名单只认真人名", set(names) == {"少女", "父亲", "母亲"}, str(names))
    check("A3 ★★ 名字后的「说道」被削掉", "母亲" in names and "母亲说道" not in names, str(names))
    check("A4 ★ 三种引号都认（「」“”\"\"）",
          len(parse_dialogue('甲：「一」乙：“二”', known={"甲", "乙"})) == 2, "")
    check("A5 ★★ 内景/字幕这类前缀不被当成人名",
          parse_dialogue("内景：「夜」", known=None) == [], "")
    check("A6 空文本返回空", parse_dialogue("") == [], "")

    print(); print("B. 挂到镜头上")
    shots = [_Shot("SH1", "S01", 5.0), _Shot("SH2", "S01", 6.0),
             _Shot("SH3", "S02", 8.0)]
    lines = [{"character": "甲", "text": "一", "sceneId": "S01"},
             {"character": "甲", "text": "二", "sceneId": "S01"},
             {"character": "乙", "text": "三", "sceneId": "S09"}]
    rows, dropped = assign_to_shots(lines, shots)
    check("B1 ★★ 同场两句挂到该场第 1、2 镜",
          [r["shotId"] for r in rows] == ["SH1", "SH2"], str([r["shotId"] for r in rows]))
    check("B2 ★★★ 挂不上的台词进 dropped（不许悄悄消失）",
          len(dropped) == 1 and dropped[0]["reason"] == "该场没有镜头", str(dropped))
    check("B3 ★ 镜头时长被带过来", rows[0]["shotSeconds"] == 5.0, "")

    print(); print("C. 合成 + 对齐（假后端）")
    def fake_synth(text, path, voice="", instruction=""):
        Path(str(path)).parent.mkdir(parents=True, exist_ok=True)
        Path(str(path)).write_bytes(b"RIFFfake")
        return {"ok": True, "seconds": 7.0, "voice": voice,
                "instruction": instruction, "error": "", "sha256": "abc"}

    with tempfile.TemporaryDirectory() as td:
        rep = build([{"character": "甲", "text": "一", "sceneId": "S01"},
                     {"character": "乙", "text": "二", "sceneId": "S02"}],
                    shots, synth=fake_synth,
                    cast_map={"甲": {"voice": "", "instruction": "甲声"},
                              "乙": {"voice": "", "instruction": "乙声"}},
                    out_dir=td)
        check("C1 ★★ 两句都合成成功", len(rep["utterances"]) == 2, str(len(rep["utterances"])))
        check("C2 ★★ 音频 7s vs 镜头 5s -> 拉长镜头",
              rep["utterances"][0]["align"]["action"] == "extend",
              str(rep["utterances"][0]["align"]))
        check("C3 ★★★ 指标算出来了（对齐/一致性/失败率）",
              rep["report"]["total"] == 2 and "align" in rep["report"], "")
        check("C4 ★★★ 明确写出「没做什么」",
              any("混流" in x for x in rep["notDone"]), str(rep["notDone"])[:40])

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
