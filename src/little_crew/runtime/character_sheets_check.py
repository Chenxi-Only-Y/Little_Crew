"""角色设计师·图像半自检（2026-11-13）：`runtime/character_sheets.py`。

锁住五件事：
1 题面含角色名与外观设定（**不编造**：只取已有字段）
2 限量（`limit`）生效，多出来的记录为 skipped
3 失败**记录而不抛**（图集是辅助资料，不能拖垮整集）
4 产物与 `sheet.json` 落盘（题面 / sha256 / 授权状态 / 未验证声明）
5 默认**关**（按张计费，不能默认开）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class _Ref:
    def __init__(self, uri: str):
        self.uri = uri


def _lib(n: int = 3):
    from little_crew.agent.character_sheet import CharacterLibrary, CharacterSheet

    sheets = tuple(
        CharacterSheet(
            name=f"角色{i}", character_id=f"c{i}",
            appearance={"hair": f"发型{i}", "outfit": f"服装{i}"},
            scenes=("S01",))
        for i in range(1, n + 1))
    return CharacterLibrary(episode_id="T", sheets=sheets, seed="s")


class _Painter:
    """测试替身：不发网络请求，直接给字节。"""

    def __new__(cls, *, fail_ids=(), **kw):
        from little_crew.runtime.character_sheets import CharacterSheetPainter

        p = CharacterSheetPainter(**kw)
        p._fake_fail = set(fail_ids)

        def _channel():
            return (None, None, object())

        def _render_one(_spec, _be, _ch, prompt, *, idempotency_key):
            cid = idempotency_key.split(":")[-1]
            if cid in p._fake_fail:
                raise RuntimeError("模拟上游失败")
            return _Ref(f"mem://{cid}")

        p._channel = _channel
        p._render_one = _render_one
        p._license_note = lambda: {"allowed": True, "reason": "测试", "rule": "TEST"}
        return p


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("角色设计师·图像半（character_sheets）—— 自检")
    print("=" * 66)

    import tempfile

    from little_crew.runtime import character_sheets as cs

    print(); print("A. 题面")
    lib = _lib(2)
    prompt = cs.build_prompt(lib.sheets[0])
    check("A1 ★★★ 题面含角色名", "角色1" in prompt, prompt[:40])
    check("A2 ★★★ 题面含外观设定（只取已有字段）",
          "发型1" in prompt and "服装1" in prompt, prompt[:80])
    check("A3 ★★★ 题面按四视图模板：构图/一致性句/负面词齐全",
          all(k in prompt for k in ("左侧约占四成半", "四个视图必须属于同一角色",
                                    "严格侧面", "背面全身", "【负面词】")), "")
    check("A4 ★★ 外观缺失时不编造（空串也不崩）",
          isinstance(cs.build_prompt(type("X", (), {"name": "无名", "character_id": "x",
                                                    "appearance": {}})()), str), "")

    print(); print("B. 限量与产物")
    with tempfile.TemporaryDirectory() as td:
        # `_read_artifact` 在不触网时从 uri 读：这里换成直接给字节
        cs._read_artifact = lambda ref: PNG
        p = _Painter(limit=2)
        # 3 个角色 + limit=2 -> 第 3 个应进 skipped（原来这里传的是 2 个角色，测不到）
        rep = p.paint(_lib(3), Path(td))
        check("B1 ★★★ limit 生效：只画前 2 个", rep["count"] == 2, str(rep["count"]))
        check("B2 ★★★ 多出来的记录为 skipped",
              rep["skipped"] == ["c3"], str(rep["skipped"]))
        img = Path(td) / "c1" / "sheet.png"
        check("B3 ★★★ 图片落盘且扩展名按魔数（PNG）",
              img.is_file() and img.read_bytes()[:8] == PNG[:8], str(img))
        meta_p = Path(td) / "c1" / "sheet.json"
        meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.is_file() else {}
        check("B4 ★★★ sheet.json 含题面/sha256/授权状态",
              bool(meta.get("prompt")) and len(str(meta.get("sha256"))) == 64
              and isinstance(meta.get("license"), dict), json.dumps(meta, ensure_ascii=False)[:80])
        check("B5 ★★★ 声明「未验证身份一致性」（不冒充已验证）",
              "身份度量" in str(meta.get("notVerified")), str(meta.get("notVerified"))[:60])

    print(); print("C. 失败不抛、留痕")
    with tempfile.TemporaryDirectory() as td2:
        p2 = _Painter(limit=3, fail_ids=("c2",))
        rep2 = p2.paint(_lib(3), Path(td2))
        check("C1 ★★★ 单角色失败不抛异常", isinstance(rep2, dict), "")
        check("C2 ★★★ 失败被记录（含原因）",
              len(rep2["failed"]) == 1 and "模拟上游失败" in str(rep2["failed"][0]),
              str(rep2["failed"])[:80])
        check("C3 ★★ 其余角色照常产出", rep2["count"] == 2, str(rep2["count"]))

    print(); print("D. 开关与不含图时的行为")
    import os

    os.environ.pop("LITTLE_CREW_CHAR_SHEETS", None)
    check("D1 ★★★ 默认关（按张计费，不能默认开）", cs.enabled_by_env() is False, "")
    os.environ["LITTLE_CREW_CHAR_SHEETS"] = "1"
    try:
        check("D2 ★★ 环境变量可开", cs.enabled_by_env() is True, "")
    finally:
        os.environ.pop("LITTLE_CREW_CHAR_SHEETS", None)

    print(); print("A2b. 模板结构（用户提供的四视图模板）")
    from little_crew.runtime.character_sheet_prompt import (
        NEGATIVE_GROUPS, QC_CHECKLIST, build_prompt as _bp, slots_for,
    )

    check("A5 ★★★ 12 槽位有值（含配色/细节不丢）",
          len(slots_for(lib.sheets[0])) >= 12, str(len(slots_for(lib.sheets[0]))))
    check("A6 ★★★ 负面词四组齐全",
          len(NEGATIVE_GROUPS) == 4 and all(NEGATIVE_GROUPS.values()), "")
    check("A7 ★★★ 质检表 10 条（模板 §六）", len(QC_CHECKLIST) == 10, str(len(QC_CHECKLIST)))
    p_ok = _bp(lib.sheets[0])
    check("A8 ★★★ 同一角色两次组装**完全一致**（确定性）",
          p_ok == _bp(lib.sheets[0]), "")
    check("A9 ★★ 手工卡字段不被丢（hair/outfit/palette/mark 都进题面）",
          all(k in _bp(type("S", (), {"name": "x", "character_id": "x1",
                                      "appearance": {"hair": "双环髻", "outfit": "青衣",
                                                     "palette": "月白", "mark": "袖口暗纹"}})())
              for k in ("双环髻", "青衣", "月白", "袖口暗纹")), "")

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
