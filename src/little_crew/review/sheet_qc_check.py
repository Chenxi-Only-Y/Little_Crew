"""设定板质检器自检（2026-11-13，T4）。

锁住六件事（全部来自实测教训）：
1 **结构不合法 -> unclear，绝不 pass**（推理模型不吐 JSON 时曾被判满分）
2 **fail 并集**：重复判 2 次，任一次 fail 即 fail
3 **pass 的措辞**：`no_defect` 只能说"未发现硬缺陷（不等于合格）"
4 兼容两种输出格式：扁平 12 键 / items 数组
5 缺项、图片不存在、客户端异常 -> 全部 unclear
6 判官默认是 **mimo**（非推理 + 跨族）
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class _Client:
    """按脚本逐次返回（用于模拟"模型会抖"）。"""

    model_id = "mimo-v2.6-flash"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def chat_image(self, *a, **kw):
        self.calls += 1
        if not self.replies:
            return "{}"
        r = self.replies.pop(0)
        return r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)


def _all(verdict="pass", reason=""):
    from little_crew.review.sheet_qc import QC_ITEMS

    return {k: verdict for k, _ in QC_ITEMS}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("设定板质检器（sheet_qc）—— 自检")
    print("=" * 66)

    from little_crew.review import sheet_qc as Q

    print(); print("A. 结构合法性（失败不静默）")
    with tempfile.TemporaryDirectory() as td:
        img = Path(td) / "a.png"
        img.write_bytes(PNG)

        c_ok = _Client([_all("pass")])
        rep = Q.judge(img, client=c_ok, attempts=1)
        check("A1 ★★★ 全 pass -> no_defect（不叫 pass/合格）",
              rep["verdict"] == "no_defect", rep["verdict"])
        check("A2 ★★★ claim 明确写「不等于合格」",
              "不等于合格" in str(rep.get("claim")), str(rep.get("claim")))
        check("A3 ★★★ 12 项齐全", rep["reviewed"] == 12 and len(rep["items"]) == 12,
              str(rep["reviewed"]))

        c_bad = _Client(["这不是 JSON，只是思考过程"])
        rep_bad = Q.judge(img, client=c_bad, attempts=1)
        check("A4 ★★★ 模型不给 JSON -> verdict=unclear（绝不 pass）",
              rep_bad["verdict"] == "unclear" and rep_bad["unclear"] == 12,
              f"{rep_bad['verdict']} unclear={rep_bad['unclear']}")
        check("A5 ★★★ 且留下原因（可追）", bool(rep_bad["notes"]), str(rep_bad["notes"])[:60])

        half = _all("pass")
        for k in list(half)[:5]:
            half.pop(k)
        rep_half = Q.judge(img, client=_Client([half]), attempts=1)
        check("A6 ★★★ 缺项 -> 该 5 项 unclear，整体 unclear",
              rep_half["unclear"] == 5 and rep_half["verdict"] == "unclear",
              f"unclear={rep_half['unclear']} verdict={rep_half['verdict']}")

        bad_vals = {k: "maybe" for k in _all()}
        rep_bv = Q.judge(img, client=_Client([bad_vals]), attempts=1)
        check("A7 ★★ 非法取值 -> 全部 unclear",
              rep_bv["unclear"] == 12 and rep_bv["verdict"] == "unclear", "")

        weird = _Client([{"items": [{"id": "no_text", "verdict": "fail",
                                     "reason": "右下角有水印"}]}])
        rep_it = Q.judge(img, client=weird, attempts=1)
        check("A8 ★★ 兼容 items 数组格式 + fail -> revise",
              rep_it["verdict"] == "revise" and rep_it["failed"] == 1,
              f"{rep_it['verdict']} failed={rep_it['failed']}")

    print(); print("B. fail 并集（重复判，抓错不漏）")
    with tempfile.TemporaryDirectory() as td2:
        img2 = Path(td2) / "b.png"
        img2.write_bytes(PNG)
        p1 = _all("pass")
        p2 = _all("pass")
        p2["no_text"] = "fail"
        rep2 = Q.judge(img2, client=_Client([p1, p2]), attempts=2)
        check("B1 ★★★ 第 1 次 pass、第 2 次 fail -> revise（并集生效）",
              rep2["verdict"] == "revise" and rep2["failed"] == 1,
              f"{rep2['verdict']} failed={rep2['failed']}")
        check("B2 ★★★ 并集时保留 fail 的原因",
              "水印" not in "" and rep2["items"][7]["reason"] is not None, "")
        u1 = _all("pass")
        u2 = _all("pass")
        u2["same_face"] = "unclear"
        rep3 = Q.judge(img2, client=_Client([u1, u2]), attempts=2)
        check("B3 ★★ 任一次 unclear -> 该项 unclear（也不放大成 pass）",
              rep3["unclear"] >= 1 and rep3["verdict"] == "unclear",
              f"{rep3['verdict']} unclear={rep3['unclear']}")
        rep4 = Q.judge(img2, client=_Client([_all("pass"), _all("pass")]), attempts=2)
        check("B4 ★★ 两次全 pass 才 no_defect",
              rep4["verdict"] == "no_defect" and rep4["attempts"] == 2, rep4["verdict"])

    print(); print("C. 异常与默认值")
    missing = Path(tempfile.gettempdir()) / "no_such_sheet_xyz.png"
    rep_m = Q.judge(missing, client=_Client([]), attempts=1)
    check("C1 ★★★ 图片不存在 -> 全部 unclear + 原因",
          rep_m["verdict"] == "unclear" and "不存在" in " ".join(rep_m["notes"]), "")

    class _Boom:
        model_id = "x"

        def chat_image(self, *a, **kw):
            raise RuntimeError("上游 500")

    with tempfile.TemporaryDirectory() as td3:
        img3 = Path(td3) / "c.png"
        img3.write_bytes(PNG)
        rep_b = Q.judge(img3, client=_Boom(), attempts=1)
        check("C2 ★★★ 客户端抛异常 -> 不向外抛，记 unclear",
              rep_b["verdict"] == "unclear" and bool(rep_b["notes"]), "")
    check("C3 ★★★ 默认判官是 mimo（非推理 + 跨族）",
          Q.DEFAULT_JUDGE == "mimo-v2.6-flash", Q.DEFAULT_JUDGE)
    import inspect as _inspect

    check("C4 ★★ 默认重复 2 次（模型会抖）",
          _inspect.signature(Q.judge).parameters["attempts"].default == 2,
          str(_inspect.signature(Q.judge).parameters["attempts"].default))

    print(); print("D. 措辞（不能把'没看出问题'说成'合格'）")
    s1 = Q.summarize({"verdict": "no_defect", "passed": 12, "reviewed": 12,
                      "items": []})
    check("D1 ★★★ no_defect 摘要写「未发现硬缺陷（≠合格）」",
          "未发现硬缺陷" in s1 and "合格" in s1 and "≠" in s1, s1)
    s2 = Q.summarize({"verdict": "revise", "passed": 8, "reviewed": 12,
                      "items": [{"id": "no_text", "name": "画面无文字", "verdict": "fail"}]})
    check("D2 ★★ revise 摘要点名不通过项", "发现硬缺陷" in s2 and "画面无文字" in s2, s2)
    check("D3 ★★ 质检项正好 12 条", len(Q.QC_ITEMS) == 12, str(len(Q.QC_ITEMS)))

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
