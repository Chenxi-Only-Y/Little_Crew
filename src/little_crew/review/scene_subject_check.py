"""`review/scene_subject.py` 自检 —— 纯字符串，不联网。

守的是：**下游越权新增场景主体**能被检出，而**上游自己的问题不被误判成下游的错**。
"""
from __future__ import annotations

import sys

from little_crew.review.scene_subject import (
    SCENE_NOUNS,
    check_scene_subject,
    subjects_in,
)

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


SCRIPT = ("[场1] 普罗旺斯·山中·午后 @少女 独自走在山坡上采花，"
          "竹篮里已有半篮紫色花穗。她忽然停住——前方的岩石边，@旅人 倒在那里。")
#: 2026-10-29 实测撞出来的真提示词（分镜师从"紫色花穗"推理出"花田"）
PROMPT_BAD = ("午后斜阳把整片山坡照成浅金色，一垄垄紫色花穗顺着风倒向一边。"
              "少女穿粗布长裙站在花田中间，臂弯挽着半篮花穗。")


def _extract() -> None:
    print()
    print("A. 抽取")
    check("A1 认出山坡/岩石", {"山坡", "岩石"} <= subjects_in(SCRIPT),
          str(subjects_in(SCRIPT)))
    check("A2 ★ 认出「花田」（由「花田」字面命中）",
          "花田" in subjects_in(PROMPT_BAD), str(subjects_in(PROMPT_BAD)))
    check("A3 同义词归一（花海 -> 花田）",
          "花田" in subjects_in("一片花海铺到天边"), str(subjects_in("一片花海")))
    check("A4 空文本 -> 空集", subjects_in("") == set(), "")
    check("A5 名词表非空且都是元组",
          all(isinstance(v, tuple) and v for v in SCENE_NOUNS.values()), "")


def _conflict() -> None:
    print()
    print("B. 冲突判定")
    r = check_scene_subject(SCRIPT, PROMPT_BAD)
    check("B1 ★★★ 真实案例判 RETRY", r["verdict"] == "RETRY", r["verdict"])
    check("B2 ★★★ 越权项正好是「花田」", r["intruded"] == ["花田"], str(r["intruded"]))
    check("B3 理由里说清剧本有什么",
          "山坡" in r["reason"] and "花田" in r["reason"], r["reason"][:80])
    ok = check_scene_subject(SCRIPT, "少女走在山坡上，篮里是紫色花穗。旁边的岩石很冷。")
    check("B4 ★ 同一场景（不新增主体）-> KEEP", ok["verdict"] == "KEEP", ok["reason"])
    lost = check_scene_subject(SCRIPT, "少女的脸，泪水滑落。")
    check("B5 ★ 剧本有场景、提示词全丢 -> RETRY（丢掉场景也算问题）",
          lost["verdict"] == "RETRY" and "一个都没" in lost["reason"],
          lost["reason"][:60])


def _no_false_positive() -> None:
    print()
    print("C. 不误判（**这条最重要**）")
    # 上游自己有两个主体：那是上游的问题，不该判下游越权
    two = check_scene_subject("山坡上的花田里站着一个少女", "山坡和花田都在画面里")
    check("C1 ★★ 剧本自己就有两个主体 -> 不判越权", two["intruded"] == [],
          str(two["intruded"]))
    # 剧本完全没提场景 -> 无从谈"越权"，不判
    none = check_scene_subject("少女哭了。", "木屋里，少女哭了。")
    check("C2 ★★ 剧本没有场景主体 -> 不判（没有基线）",
          none["verdict"] == "KEEP", none["reason"])
    # allowed 白名单可放行
    allow = check_scene_subject(SCRIPT, PROMPT_BAD, allowed=["花田"])
    check("C3 ★ 白名单放行（美术设定里已登记的场景不算越权）",
          allow["verdict"] == "KEEP", allow["reason"])
    # 形容词不算主体
    adj = check_scene_subject(SCRIPT, "少女走在山坡上，逆光，浅金色，长焦。")
    check("C4 ★ 形容词不触发（只认名词表）", adj["verdict"] == "KEEP", adj["reason"])


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")            # type: ignore[attr-defined]
    except Exception:                                        # noqa: BLE001
        pass
    print("=" * 74)
    print("场景主体冲突 —— 自检")
    print("=" * 74)
    _extract()
    _conflict()
    _no_false_positive()
    print()
    npass = sum(1 for _, ok, _ in _results if ok)
    for name, ok, detail in _results:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 74)
    print(f"结果：{npass}/{len(_results)} 通过")
    return 0 if npass == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
