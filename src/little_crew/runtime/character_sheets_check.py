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
    check("A6 ★★★ 负面词至少模板四组、且非空（另加「面部崩坏」组）",
          len(NEGATIVE_GROUPS) >= 4 and all(NEGATIVE_GROUPS.values()), "")
    check("A7 ★★★ 质检表 ≥10 条（模板 §六 + 面部 2 条）",
          len(QC_CHECKLIST) >= 10, str(len(QC_CHECKLIST)))
    p_ok = _bp(lib.sheets[0])
    check("A8 ★★★ 同一角色两次组装**完全一致**（确定性）",
          p_ok == _bp(lib.sheets[0]), "")
    check("A9 ★★ 手工卡字段不被丢（hair/outfit/palette/mark 都进题面）",
          all(k in _bp(type("S", (), {"name": "x", "character_id": "x1",
                                      "appearance": {"hair": "双环髻", "outfit": "青衣",
                                                     "palette": "月白", "mark": "袖口暗纹"}})())
              for k in ("双环髻", "青衣", "月白", "袖口暗纹")), "")

    print(); print("A2c. 面部细节（参考图 2026-11-13 的重点）")
    from little_crew.runtime.character_sheet_prompt import build_prompt as _bp2

    pf = _bp2(lib.sheets[0])
    check("A10 ★★★ 题面有「面部细节·最高优先级」块", "面部细节" in pf and "最高优先级" in pf, "")
    check("A11 ★★★ 面部要素齐全（眼/眉鼻唇/妆容/皮肤/发丝空气感）",
          all(k in pf for k in ("眼", "卧蚕", "睫毛", "腮红", "唇", "皮肤", "空气感")), "")
    check("A12 ★★★ 负面词含「面部崩坏」组（小眼睛/呆滞/油光等）",
          "面部崩坏" in NEGATIVE_GROUPS
          and all(k in NEGATIVE_GROUPS["面部崩坏"] for k in ("小眼睛", "呆滞", "油光")), "")

    print(); print("A2d. 画风预设（用户要求：2.5D/绘画感/风格化写实/次世代）")
    from little_crew.runtime.character_sheet_prompt import (
        STYLE_PRESETS, build_prompt as _bp3, resolve_style,
    )

    for key in ("3d-guoman", "2.5d-film", "painting-cg", "stylized-real", "nextgen-game"):
        check(f"A13 预设 {key} 可解析且前缀非空",
              key in STYLE_PRESETS and bool(resolve_style(key).get("prefix")), "")
    p1 = _bp3(lib.sheets[0], style="2.5d-film")
    p2 = _bp3(lib.sheets[0], style="painting-cg")
    check("A14 ★★★ 换风格：前缀不同，但**骨架不变**（布局/一致性句仍在）",
          p1.splitlines()[0] != p2.splitlines()[0]
          and all(k in p1 and k in p2 for k in ("左侧约占四成半", "四个视图必须属于同一角色")), "")
    p3 = _bp3(lib.sheets[0], style="随便写的画风")
    check("A15 ★★ 未知画风当作自定义前缀（不报错、不丢骨架）",
          p3.startswith("随便写的画风") and "四个视图必须属于同一角色" in p3, p3[:40])

    print(); print("A2e. 比例锁（2026-11-13 实测：窄长 V 脸 -> 马脸+高眼位）")
    from little_crew.runtime.character_sheet_prompt import (
        build_prompt as _bp4,
    )

    pl = _bp4(lib.sheets[0])
    check("A16 ★★★ 题面含「比例锁」（三庭等分/眼位百分比/脸宽比）",
          "比例锁" in pl and "1/3" in pl and "45%~50%" in pl and "0.72~0.75" in pl, "")
    check("A17 ★★★ 负面词含「比例崩坏」组（长脸/眼位过高/中庭过长）",
          "比例崩坏" in NEGATIVE_GROUPS
          and all(k in NEGATIVE_GROUPS["比例崩坏"] for k in ("长脸", "眼位过高", "中庭过长")), "")
    check("A18 ★★ 脸宽比不再是 0.6x 的窄长脸（硬指标 0.72~0.75）",
          "0.6" not in pl.split("【比例锁")[0].split("脸宽约")[-1][:6], "")

    print(); print("A2f. 比例档 adult / cute")
    from little_crew.runtime.character_sheet_prompt import (
        PROPORTION_PROFILES, build_prompt as _bp5, resolve_proportion,
    )

    check("A19 ★★★ 两个比例档都存在且文本非空",
          set(PROPORTION_PROFILES) == {"adult", "cute"}
          and all(PROPORTION_PROFILES.values()), "")
    pa = _bp5(lib.sheets[0], proportion="adult")
    pc = _bp5(lib.sheets[0], proportion="cute")
    check("A20 ★★★ cute 档（v1 数值）：脸宽 0.78~0.82/中庭 0.32/眼位 42%~45%/眼裂 1/4",
          all(k in pc for k in ("0.78~0.82", "0.32", "42%~45%", "1/4")), "")
    check("A21 ★★★ cute 档负面词换成「成熟感」组（不再用「比例崩坏」）",
          "成熟御姐感" in pc and "马脸" not in pc, "")
    check("A22 ★★ 未知比例档退回 adult（不崩）",
          resolve_proportion("xxx")[0] == "adult", "")
    # ⚠️ 2026-11-13 回退：v2 曾给 cute 单独加"特写 90%~95%"布局，
    # 与"眼裂 1/3 + 无毛孔磨皮"同时生效后出现过拟合娃娃脸（用户判定"这版不行"）。
    # 现在**两档共用布局**，只靠比例锁区分。
    check("A23 ★★★ 两档共用布局（cute 不再单独放大特写）",
          "90%~95%" not in pc and "左侧约占四成半" in pc and "左侧约占四成半" in pa, "")
    check("A24 ★★ cute 档皮肤要求「轻微磨皮但保留极细纹理」（不是无毛孔）",
          "保留极细纹理" in pc and "哑光瓷肌" not in pc, "")

    print(); print("E. 保图微调（refine，2026-11-13）")
    with tempfile.TemporaryDirectory() as td5:
        base = _Painter(limit=1)
        rep_no = base.paint(_lib(1), Path(td5))
        check("E1 ★★★ 默认不精修（按张计费，默认关）",
              rep_no.get("refine") is False and not rep_no.get("refined"), "")
        calls = {"n": 0}

        def fake_refiner(src: str) -> dict:
            calls["n"] += 1
            out = Path(src).with_name("sheet_refined.png")
            out.write_bytes(PNG)
            return {"ok": True, "out": str(out), "model": "gpt-image-2.6-sunburst",
                    "seconds": 46.3}

        rep_yes = _Painter(limit=1).paint(_lib(1), Path(td5) / "r", refine=True,
                                          refiner=fake_refiner)
        check("E2 ★★★ refine=True 会调用精修并记录产物",
              calls["n"] == 1 and len(rep_yes.get("refined") or []) == 1,
              str(rep_yes.get("refined"))[:80])
        item = (rep_yes.get("painted") or [{}])[0]
        check("E3 ★★★ 精修结果带 file/model/sha256（可追溯）",
              bool(item.get("refined", {}).get("file"))
              and item["refined"].get("model") == "gpt-image-2.6-sunburst"
              and len(str(item["refined"].get("sha256"))) == 16, str(item.get("refined"))[:80])

        def bad_refiner(src: str) -> dict:
            return {"ok": False, "error": "模拟上游 502"}

        rep_bad = _Painter(limit=1).paint(_lib(1), Path(td5) / "b", refine=True,
                                          refiner=bad_refiner)
        check("E4 ★★★ 精修失败只记录、不抛（不拖垮整集）",
              len(rep_bad.get("refineFailed") or []) == 1
              and len(rep_bad.get("painted") or []) == 1, str(rep_bad.get("refineFailed"))[:80])

        # 质检：只判"最终交付那张"（有精修判精修，否则原图）；attempts 透传
        seen: list[str] = []

        def fake_judge(path: str, *, context: str = "", attempts: int = 1) -> dict:
            seen.append(f"{Path(path).name}|attempts={attempts}")
            return {"kind": "sheet.qc", "verdict": "no_defect", "passed": 12,
                    "reviewed": 12, "failed": 0, "unclear": 0, "items": [],
                    "claim": "未发现硬缺陷（不等于合格）"}

        rep_q1 = _Painter(limit=1).paint(_lib(1), Path(td5) / "q1", qc=True,
                                         qc_judge=fake_judge, qc_attempts=1)
        check("E7 ★★★ 无精修时只判 1 张（原图）",
              len(rep_q1.get("qcReports") or []) == 1 and "sheet.png" in seen[0],
              str(seen))
        seen.clear()
        rep_q2 = _Painter(limit=1).paint(
            _lib(1), Path(td5) / "q2", refine=True,
            refiner=lambda s: (lambda p: {"ok": True, "out": str(p),
                                          "model": "gpt-image-2.6-sunburst",
                                          "seconds": 1.0})(Path(s).with_name("sheet_refined.png")),
            qc=True, qc_judge=fake_judge, qc_attempts=2)
        # 上面 refiner 没真正写文件 -> 会跳过；换成真写
        seen.clear()

        def refiner2(src: str) -> dict:
            out = Path(src).with_name("sheet_refined.png")
            out.write_bytes(PNG)
            return {"ok": True, "out": str(out), "model": "gpt-image-2.6-sunburst",
                    "seconds": 1.0}

        rep_q3 = _Painter(limit=1).paint(_lib(1), Path(td5) / "q3", refine=True,
                                         refiner=refiner2, qc=True,
                                         qc_judge=fake_judge, qc_attempts=2)
        check("E8 ★★★ 有精修时只判精修那张（省一半）",
              len(rep_q3.get("qcReports") or []) == 1 and "sheet_refined.png" in seen[0],
              str(seen))
        check("E9 ★★ attempts 透传给质检（严格模式=2）",
              "attempts=2" in seen[0], str(seen))

    from little_crew.runtime import sudorelay_edits as SE

    check("E5 ★★ 精修默认模型是 2.6（早期 502 记录已过期）",
          SE.DEFAULT_EDIT_MODEL == "gpt-image-2.6-sunburst", SE.DEFAULT_EDIT_MODEL)
    check("E6 ★★ 微调提示词写明「不改设计」且只修三类",
          ("不改变任何设计" in SE.REFINE_PROMPT
           and all(k in SE.REFINE_PROMPT for k in ("手部", "鞋型", "发丝"))), "")

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
