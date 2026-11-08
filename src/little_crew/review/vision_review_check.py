"""视觉闸自检 —— 用假客户端，不联网。"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from little_crew.review import vision_review as VR

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


class _Reply:
    def __init__(self, text):
        self.text = text


class _FakeClient:
    """按调用顺序返回预设文本。"""
    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = []

    def chat_image(self, prompt, b64, *, what="", max_tokens=0):
        self.calls.append({"prompt": prompt, "n": len(b64), "max_tokens": max_tokens})
        return self.texts.pop(0) if self.texts else ""


class _Shot:
    def __init__(self, sid, prompt=""):
        self.shot_id, self.prompt = sid, prompt


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("视觉闸（真的看图判崩）—— 自检")
    print("=" * 66)

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        img = d / "a.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 200)
        shots = [_Shot("S1", "少女喂药"), _Shot("S2", "空镜"), _Shot("S3", "他倒下")]

        print(); print("A. 正常与崩坏")
        c = _FakeClient([
            json.dumps({"blank": False, "faces": "ok", "matches": 4, "problems": []}),
            json.dumps({"blank": False, "faces": "broken", "matches": 3,
                        "problems": ["五官糊成一团"]}),
            json.dumps({"blank": True, "faces": "none", "matches": 1,
                        "problems": ["画面全黑"]}),
        ])
        r = VR.review(shots, {s.shot_id: str(img) for s in shots}, client=c)
        kinds = {f["kind"]: f["severity"] for f in r["findings"]}
        check("A1 ★★★ 脸崩 -> error", kinds.get("vision.faces-broken") == "error", str(kinds))
        check("A2 ★★★ 空白 -> error", kinds.get("vision.blank") == "error", str(kinds))
        check("A3 ★★ 低一致度只记 warn（不自动重渲）",
              kinds.get("vision.low-match") == "warn", str(kinds))
        check("A4 ★★ 三镜都审了", r["reviewedCount"] == 3, str(r["reviewedCount"]))

        print(); print("B. 失败不许当通过")
        c2 = _FakeClient(["这不是 JSON，只是一段推理过程"])
        r2 = VR.review([_Shot("S1", "x")], {"S1": str(img)}, client=c2)
        check("B1 ★★★ 模型不给 JSON -> reviewed=False",
              r2["rows"][0]["reviewed"] is False, str(r2["rows"][0]))
        check("B2 ★★★ 且不产生任何 finding（未审 != 通过）",
              r2["findings"] == [], str(r2["findings"]))
        check("B3 错误被记下来", r2["errors"] == 1, str(r2["errors"]))

        print(); print("C. 边界")
        r3 = VR.review([_Shot("S9", "x")], {}, client=_FakeClient([]))
        check("C1 ★★ 没有图 -> reviewed=False 且原因写明",
              r3["rows"][0]["reviewed"] is False and "没有图" in r3["rows"][0]["reason"],
              str(r3["rows"][0]))
        c4 = _FakeClient([json.dumps({"faces": "ok", "matches": 5}) for _ in range(3)])
        r4 = VR.review(shots, {s.shot_id: str(img) for s in shots}, client=c4, max_shots=1)
        check("C2 ★★ max_shots 限制生效（只审 1 镜）", r4["reviewedCount"] == 1,
              str(r4["reviewedCount"]))
        check("C3 ★ 超出上限的标 skipped", any(x.get("reason") == "超出本次上限"
                                           for x in r4["rows"]), "")
        check("C4 ★★★ 提示词里写明 max_tokens 要够（推理模型会吃光额度）",
              c4.calls and c4.calls[0]["max_tokens"] >= 1000, str(c4.calls[:1]))
        check("C5 ★★ 明确列出「没判什么」",
              any("美不美" in x for x in r["notJudged"]), str(r["notJudged"])[:40])
        r5 = VR.review([], {}, client=_FakeClient([]))
        check("C6 空输入不崩", r5["total"] == 0, "")

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
