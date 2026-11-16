"""`ir/shot_lexicon.py` 自检（2026-11-16）。"""
from __future__ import annotations

import sys

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("景别/运镜词表 —— 自检")
    print("=" * 66)

    from little_crew.ir import shot_lexicon as L

    print(); print("A. 词表规模（吸收用户 4 张图 + 公开资料）")
    check("A1 ★★★ 景别 ≥ 13 种（含双人/群像/插入/反应/空镜）",
          len(L.SHOT_SIZES) >= 13, str(len(L.SHOT_SIZES)))
    check("A2 ★★★ 运镜 ≥ 25 种（推拉摇移跟升降甩/环绕/变焦/斯坦尼康/航拍）",
          len(L.CAMERA_MOVES) >= 25, str(len(L.CAMERA_MOVES)))
    check("A3 ★★★ 特殊镜头 ≥ 10 种（子弹时间/延时/定格/分屏/匹配/跳切/焦点转移）",
          len(L.SPECIAL_SHOTS) >= 10, str(len(L.SPECIAL_SHOTS)))
    check("A4 ★★ 动态幅度 small/medium/large",
          set(L.MOTION_AMPLITUDES) == {"small", "medium", "large"}, "")

    print(); print("B. 中文别名归一（用户图里的写法）")
    cases = [("推镜头", "push_in"), ("拉", "pull_out"), ("摇镜头", "pan_left"),
             ("甩镜头", "whip_pan"), ("移镜头", "truck_left"),
             ("环绕", "orbit"), ("跟拍", "follow"), ("希区柯克变焦", "dolly_zoom"),
             ("特写", "close"), ("大远景", "extreme_long"), ("空镜", "empty"),
             ("双人镜头", "two_shot")]
    bad = []
    ok = True
    for raw, want in cases:
        try:
            got = L.canonical_shot_size(raw) if want in L.SHOT_SIZES else L.canonical_move(raw)
            if got != want:
                ok = False
                bad.append((raw, want, got))
        except ValueError as e:
            ok = False
            bad.append((raw, want, str(e)[:40]))
    check("B1 ★★★ 中文别名全部归一", ok, str([b for b in bad if len(b) == 3][:4]))
    check("B2 ★★★ 中文景别名归一", L.canonical_shot_size("近景") == "medium_close", "")
    check("B3 ★★ 动态幅度中文归一", L.canonical_motion_amplitude("大动态") == "large", "")

    print(); print("C. 提示词片段（中英）")
    check("C1 ★★★ 中文片段是中文",
          "推进" in L.move_prompt("push_in", "zh") and "push" not in L.move_prompt("push_in", "zh"), "")
    check("C2 ★★★ 英文片段是英文",
          "pushing" in L.move_prompt("推镜头", "en"), "")
    check("C3 ★★ 景别片段", "close-up" in L.shot_size_prompt("特写", "en"), "")
    check("C4 ★★ 未知值原文回退（不吞字段）",
          L.move_prompt("unknown_move", "en") == "unknown_move", "")

    print(); print("D. 严格校验 + 归属")
    check("D1 ★★★ 未知运镜抛 ValueError",
          _raises(lambda: L.canonical_move("不存在")), "")
    check("D2 ★★★ 未知景别抛 ValueError",
          _raises(lambda: L.canonical_shot_size("xxl")), "")
    terms = L.validate_shot_terms(shot_size="近景", move="甩镜头", amplitude="小动态")
    check("D3 ★★★ validate_shot_terms 归一三字段",
          terms == {"shotSize": "medium_close", "cameraMove": "whip_pan",
                    "motionAmplitude": "small"}, str(terms))
    check("D4 ★★★ 归属表（分镜/导演/摄影/剪辑）齐全",
          {"shot_size", "camera_move", "angle", "special", "cut_rhythm"} <= set(L.OWNERS), "")

    n = sum(1 for _, ok, _ in _r if ok)
    print()
    for name, ok, detail in _r:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 66)
    print(f"结果：{n}/{len(_r)} 通过")
    return 0 if n == len(_r) else 1


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except ValueError:
        return True


if __name__ == "__main__":
    raise SystemExit(main())
