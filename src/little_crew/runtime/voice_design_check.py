"""音色设计层自检：设定规整 · 描述生成 · ASCII 映射 · 物化。

**不真的合成**（物化要跑 TTS）；只验规则与失败语义。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from little_crew.governance.voice_design import (
    CHOICES, DEFAULTS, from_sheet, instruction, normalize, prompt_for_llm, slug,
)
from little_crew.runtime.voice_forge import forge

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("音色设计（LLM 设定 -> TTS 物化）—— 自检")
    print("=" * 66)

    print(); print("A. 设定规整")
    n = normalize({"gender": "男", "age": "中年", "texture": "沙哑",
                   "pace": "慢", "pitch": "低"})
    check("A1 ★★ 合法值原样保留",
          n["gender"] == "男" and n["age"] == "中年" and n["texture"] == "沙哑", str(n))
    bad = normalize({"gender": "机器人", "texture": "金属感"})
    check("A2 ★★★ 非法值回落默认（不猜）",
          bad["gender"] == DEFAULTS["gender"] and bad["texture"] == DEFAULTS["texture"], str(bad))
    check("A3 ★★ 非法值被记录（可审计）",
          len(bad["_fixed"]) == 2, str(bad["_fixed"]))
    check("A4 七项齐全", all(k in normalize({}) for k in CHOICES), "")
    check("A5 空输入不崩", normalize(None)["gender"] == DEFAULTS["gender"], "")

    print(); print("B. 描述生成")
    s1 = instruction({"gender": "女", "age": "少女", "texture": "清亮", "pace": "快"})
    s2 = instruction({"gender": "男", "age": "中年", "texture": "沙哑", "pace": "慢", "pitch": "低"})
    check("B1 ★★ 男女描述不同", "女性" in s1 and "男性" in s2, f"{s1} / {s2}")
    check("B2 ★★ 语速与音高进入描述",
          "语速偏快" in s1 and "音高偏低" in s2, f"{s1} / {s2}")
    check("B3 ★★★ 描述里**不含情绪**（情绪要按句给，否则音色会漂）",
          "质问" not in s1 and "叹" not in s2, s1)

    print(); print("C. ASCII 映射（TTS 只收 [A-Za-z0-9_-]）")
    check("C1 ★★★ 中文角色名 -> ASCII",
          slug("少女") == "girl" and slug("父亲") == "father", f"{slug('少女')}/{slug('父亲')}")
    check("C2 ★★ 已是 ASCII 的原样返回", slug("Tom") == "Tom", slug("Tom"))
    check("C3 ★★ 未收录的中文 -> role_ 稳定哈希",
          slug("某角色").startswith("role_") and slug("某角色") == slug("某角色"), slug("某角色"))

    print(); print("D. 物化的失败语义")
    def no_synth(text, path, **kw):
        return {"ok": False, "seconds": 0.0, "error": "stub"}
    with tempfile.TemporaryDirectory() as td:
        r = forge("少女", {"gender": "女"}, synth=no_synth, workdir=td,
                  save=lambda *a, **k: {"ok": True, "path": "x"})
        check("D1 ★★★ 试音失败 -> ok=False 且不留音色",
              r["ok"] is False and "试音合成失败" in r["error"], r["error"])
        r2 = forge("", {"gender": "女"}, synth=no_synth, workdir=td)
        check("D2 ★★ 空角色名 -> 明确失败", r2["ok"] is False and "角色名" in r2["error"], r2["error"])

    print(); print("E. LLM 提示词")
    p = prompt_for_llm(["少女", "父亲"], {"少女": {"gender": "女性", "ageBand": "少女"}})
    check("E1 ★★ 提示词含角色与可选值",
          "少女" in p and "沙哑" in p and "JSON" in p, p[:60])
    check("E2 ★★ 提示词明确禁写台词", "不要写台词" in p, "")

    n_ = sum(1 for _, ok, _ in _r if ok)
    print()
    for name, ok, detail in _r:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 66)
    print(f"结果：{n_}/{len(_r)} 通过")
    return 0 if n_ == len(_r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
