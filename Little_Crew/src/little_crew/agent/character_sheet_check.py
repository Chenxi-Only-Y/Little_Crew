"""角色定妆表 —— 自检。

核心命题：**同一角色必定得到同一外观，且外观只来自封闭选项集。**

前者是缓存正确性的前提（外观不确定 → irHash 每次变 → 缓存永不命中且无法察觉）；
后者是"可复现"的前提（自由文本会让同一角色得到不同描述）。

运行： python -m little_crew.agent.character_sheet_check
"""

from __future__ import annotations

import sys

from little_crew.agent.character_sheet import (
    APPEARANCE_OPTIONS,
    OPTION_KEYS,
    Appearance,
    CharacterDesignerRules,
    CharacterLibrary,
    character_id_for,
    design_appearance,
    library_digest,
)
from little_crew.agent.protocol import CallResult

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


SCRIPT = """[场1] 山中·午后
@少女 在山坡上采花。@旅人 倒在岩石边，腿上带着伤。
少女蹲下替他扎紧伤口。

[场2] 村口·春
@旅人 的伤好了。@少女 从屋里出来，身上只有一个小包袱。
@老奶奶 把一束薰衣草塞进她手里。

[场3] 山路·黎明
两人并肩走着。@少女 忽然停下，举起花束掷了过去。
"""


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    print("=" * 78)
    print("角色定妆表 —— 自检")
    print("=" * 78)

    _determinism()
    _options_closed()
    _prompt_fragment()
    _designer()
    _overrides()
    _library()
    _protocol()
    _cache_linkage()
    _semantic_hints()

    print()
    npass = sum(1 for _, ok, _ in _results if ok)
    for name, ok, detail in _results:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail:
            print(f"       {detail}")
    print("=" * 78)
    print(f"结果：{npass}/{len(_results)} 通过")
    return 0 if npass == len(_results) else 1


# ---------- A. 确定性 ----------

def _determinism() -> None:
    a = design_appearance("少女", story_seed="s1")
    b = design_appearance("少女", story_seed="s1")
    check("A1 ★ 同角色名+同种子 → 完全相同的外观",
          a == b, a.prompt_fragment()[:56])

    c = design_appearance("少女", story_seed="s2")
    check("A2 不同故事种子 → 不同外观（两集同名角色可以是两个人）",
          a != c, c.prompt_fragment()[:56])

    d = design_appearance("旅人", story_seed="s1")
    check("A3 不同角色 → 不同外观", a != d)

    # 反复调用也必须稳定（排除"用了随机数但恰好两次相同"）
    vals = {design_appearance("少女", story_seed="s1").prompt_fragment()
            for _ in range(50)}
    check("A4 ★ 50 次调用得到同一结果（没有隐藏的随机源）",
          len(vals) == 1, f"{len(vals)} 种")

    # 空种子也要可用
    check("A5 空种子仍确定",
          design_appearance("X", story_seed="") == design_appearance("X", story_seed=""))

    # 角色 id 必须稳定且形态与剧本解析器一致
    check("A6 角色 id 稳定", character_id_for("少女") == character_id_for("少女"),
          character_id_for("少女"))
    import re
    check("A7 角色 id 形态与剧本解析器一致（CHAR-四位十六进制）",
          bool(re.fullmatch(r"CHAR-[0-9A-F]{4}", character_id_for("少女"))),
          character_id_for("少女"))
    check("A8 不同角色得到不同 id",
          character_id_for("少女") != character_id_for("旅人"))


# ---------- B. 选项封闭 ----------

def _options_closed() -> None:
    # ⚠️ 年龄段天然只有三类（少年/青年/中年），不该与其它槽位同一门槛。
    # 早期版本要求全部 >=5，那是把"选择够多"这件事写成了统一数字。
    _FLOOR = {"age_band": 3}
    thin = {k: len(v) for k, v in APPEARANCE_OPTIONS.items()
            if len(v) < _FLOOR.get(k, 5)}
    check("B1 每个槽位都有足够多的选择（年龄段下限 3，其余 5）",
          not thin, str(thin) if thin else
          str({k: len(v) for k, v in APPEARANCE_OPTIONS.items()}))

    check("B2 选项键顺序固定（否则摘要会落到不同槽位）",
          OPTION_KEYS == tuple(APPEARANCE_OPTIONS),
          str(OPTION_KEYS[:3]))

    # 抽样检查：产出的每一项都必须在选项集内
    bad: list[str] = []
    for name in ("少女", "旅人", "老奶奶", "父亲", "母亲", "X", "角色甲"):
        ap = design_appearance(name, story_seed="seed")
        for k, v in ap.as_dict().items():
            if v not in APPEARANCE_OPTIONS[k]:
                bad.append(f"{name}.{k}={v!r}")
    check("B3 ★ 所有产出的外观值都在封闭选项集内（无自由文本）",
          not bad, str(bad[:3]))

    # 多样性：不同角色不该全都一样
    seen = {design_appearance(f"角色{i}", story_seed="s").prompt_fragment()
            for i in range(40)}
    check("B4 ★ 多个角色得到足够多样的外观（不是都撞在一起）",
          len(seen) >= 30, f"40 个角色得到 {len(seen)} 种外观")

    # 逐槽位多样性：每个槽位都应被用到大半
    per_slot: dict[str, set[str]] = {k: set() for k in OPTION_KEYS}
    for i in range(200):
        ap = design_appearance(f"角色{i}", story_seed="s")
        for k, v in ap.as_dict().items():
            per_slot[k].add(v)
    thin = [k for k, vs in per_slot.items()
            if len(vs) < max(2, len(APPEARANCE_OPTIONS[k]) // 2)]
    check("B5 每个槽位都能取到大半选项（取样函数没有偏斜）",
          not thin, f"偏斜的槽位 {thin}")


# ---------- C. 提示词片段 ----------

def _prompt_fragment() -> None:
    ap = Appearance(**{k: APPEARANCE_OPTIONS[k][0] for k in OPTION_KEYS})
    frag = ap.prompt_fragment()
    check("C1 片段非空", bool(frag.strip()), frag[:60])
    check("C2 ★ 片段含全部外观维度",
          all(v in frag for v in ap.as_dict().values()),
          frag[:70])
    check("C3 片段稳定（同外观两次得到同一串）",
          ap.prompt_fragment() == frag)
    check("C4 不同外观得到不同片段",
          ap.prompt_fragment()
          != Appearance(**{k: APPEARANCE_OPTIONS[k][1]
                           for k in OPTION_KEYS}).prompt_fragment())
    check("C5 无残留的空分隔符（不能出现连续逗号）",
          "，，" not in frag and not frag.startswith("，")
          and not frag.endswith("，"), frag[-20:])
    check("C6 describe_lines 给出逐项可读描述（审片用）",
          len(ap.describe_lines()) == len(OPTION_KEYS),
          str(ap.describe_lines()[:2]))


# ---------- D. 角色设计师 ----------

def _designer() -> None:
    lib = CharacterDesignerRules().design(SCRIPT, episode_id="E100")
    names = [s.name for s in lib.sheets]
    # ⚠️ 用**集合**比较，不用 sorted 后的列表：
    # sorted 对中文是按 Unicode 码位，不是拼音 —— 写死顺序必然错。
    check("D1 识别出全部 @角色（3 个）",
          set(names) == {"少女", "老奶奶", "旅人"}, str(sorted(names)))
    check("D2 每个角色都带 id", all(s.character_id for s in lib.sheets))
    check("D3 每个角色都带外观", all(s.appearance for s in lib.sheets))
    check("D4 每个角色都记录了出场场次",
          all(s.scenes for s in lib.sheets),
          str({s.name: s.scenes for s in lib.sheets}))

    # 输出顺序稳定
    lib2 = CharacterDesignerRules().design(SCRIPT, episode_id="E100")
    check("D5 两次设计得到同一顺序（库的内容哈希不会漂移）",
          [s.character_id for s in lib.sheets]
          == [s.character_id for s in lib2.sheets])

    # 同剧本 → 同结果
    check("D6 ★ 同一份剧本两次设计得到相同外观",
          [s.prompt_fragment for s in lib.sheets]
          == [s.prompt_fragment for s in lib2.sheets])

    # 改剧本 → 换设计
    lib3 = CharacterDesignerRules().design(
        SCRIPT.replace("山路", "海边"), episode_id="E100")
    check("D7 改过的剧本会重新设计（种子来自内容）",
          [s.prompt_fragment for s in lib3.sheets]
          != [s.prompt_fragment for s in lib.sheets])

    # 空剧本
    check("D8 空剧本不崩且产出空库",
          CharacterDesignerRules().design("").sheets == ())

    # 无 @标记 的散文 → 不识别角色，但不崩
    check("D9 无角色标记的散文不崩",
          CharacterDesignerRules().design("从前有座山。").sheets == ())


# ---------- E. 人工覆盖 ----------

def _overrides() -> None:
    ov = {"少女": {"hair_style": APPEARANCE_OPTIONS["hair_style"][0]}}
    lib = CharacterDesignerRules(overrides=ov).design(SCRIPT)
    s = lib.by_name("少女")
    check("E1 覆盖生效", s is not None
          and s.appearance.hair_style == APPEARANCE_OPTIONS["hair_style"][0],
          s.appearance.hair_style if s else "-")
    check("E2 覆盖被记录（可追溯）",
          s is not None and "hair_style" in s.overridden, str(s.overridden if s else []))
    check("E3 覆盖只影响指定角色",
          lib.by_name("旅人").appearance.hair_style
          != APPEARANCE_OPTIONS["hair_style"][0])

    # 越界值必须被拒绝，而不是静默接受
    bad = CharacterDesignerRules(
        overrides={"少女": {"hair_style": "金色爆炸头"}}).design(SCRIPT)
    sb = bad.by_name("少女")
    check("E4 ★ 选项集之外的覆盖值被拒绝（不静默接受）",
          "金色爆炸头" not in sb.appearance.prompt_fragment(),
          sb.appearance.hair_style)
    check("E5 被拒绝的覆盖不出现在 overridden 里",
          "hair_style" not in sb.overridden, str(sb.overridden))

    # 未知字段被忽略
    unk = CharacterDesignerRules(
        overrides={"少女": {"不存在的字段": "值"}}).design(SCRIPT)
    check("E6 未知字段被忽略（不崩）",
          unk.by_name("少女") is not None)

    # 覆盖后仍确定
    l1 = CharacterDesignerRules(overrides=ov).design(SCRIPT)
    l2 = CharacterDesignerRules(overrides=ov).design(SCRIPT)
    check("E7 覆盖后仍然确定",
          [s.prompt_fragment for s in l1.sheets]
          == [s.prompt_fragment for s in l2.sheets])


# ---------- F. 角色库 ----------

def _library() -> None:
    lib = CharacterDesignerRules().design(SCRIPT, episode_id="E100")
    s = lib.sheets[0]

    check("F1 by_name 可查", lib.by_name(s.name) is not None)
    check("F2 by_id 可查", lib.by_id(s.character_id) is not None)
    check("F3 查不到返回 None（不抛异常）",
          lib.by_name("查无此人") is None and lib.by_id("CHAR-9999") is None)
    check("F4 prompt_prefix_for 给出外观前缀",
          lib.prompt_prefix_for(s.character_id) == s.prompt_fragment)
    check("F5 查无此人时返回空串（不崩、不猜）",
          lib.prompt_prefix_for("CHAR-9999") == "")
    check("F6 ids() 给出全部角色 id",
          set(lib.ids()) == {x.character_id for x in lib.sheets})
    check("F7 可序列化（可落盘交付）",
          isinstance(lib.as_dict(), dict) and lib.as_dict()["count"] == 3)

    # 交付物必须显式标出缺口 —— 与成片清单同一原则
    d = s.as_dict()
    check("F8 ★ 定妆表显式标出还缺锚点图（把缺口写进交付物）",
          d.get("pending") == ["anchorSet"] and bool(d.get("pendingReason")),
          str(d.get("pendingReason", ""))[:60])
    check("F9 交付物含可直接用于提示词的前缀",
          d.get("promptFragment") == s.prompt_fragment)


# ---------- G. 协议契约 ----------

def _protocol() -> None:
    r = CharacterDesignerRules()
    env = type("E", (), {"payload": {"source.novel": SCRIPT,
                                     "episodeId": "E100"}})()
    out = r(envelope=env, runtime=None)
    check("G1 impl 契约（envelope.payload）返回 CallResult",
          isinstance(out, CallResult) and out.ok, str(out.error_code))
    check("G2 产出是 CharacterLibrary",
          isinstance(out.output, CharacterLibrary))
    check("G3 带证据（可追溯）", bool(out.evidence), str(out.evidence)[:60])

    out2 = r(envelope=type("E", (), {"payload": {}})(), runtime=None)
    check("G4 缺素材时明确失败（不静默产出空库）",
          (not out2.ok) and bool(out2.error_code), str(out2.error_code))

    # script.final 字段名也要能用
    out3 = r(envelope=type("E", (), {"payload": {"script.final": SCRIPT}})(),
             runtime=None)
    check("G5 也接受 script.final 作为素材来源", out3.ok)


# ---------- H. 与缓存的连接 ----------

def _cache_linkage() -> None:
    """角色库的内容摘要要能驱动缓存失效。"""
    lib = CharacterDesignerRules().design(SCRIPT, episode_id="E100")
    d1 = library_digest(lib)
    d2 = library_digest(CharacterDesignerRules().design(SCRIPT, episode_id="E100"))
    check("H1 ★ 同剧本 → 同摘要（缓存键稳定）", d1 == d2, d1[:24])

    ov = CharacterDesignerRules(
        overrides={"少女": {"hair_style": APPEARANCE_OPTIONS["hair_style"][0]}})
    d3 = library_digest(ov.design(SCRIPT, episode_id="E100"))
    check("H2 ★ 改了某角色外观 → 摘要变化（缓存会正确失效）",
          d3 != d1, f"{d1[:12]} vs {d3[:12]}")

    check("H3 摘要长度稳定（64 位十六进制）",
          len(d1) == 64, str(len(d1)))


# ---------- I. 语义线索（从称呼读出年龄与性别）----------

def _semantic_hints() -> None:
    """**这一节防的是「确定性做到了，但荒唐」。**

    早期版本对每个槽位均匀随机，产出了：

        老奶奶  ->  青年，深栗棕高马尾
        父亲    ->  中年，墨绿挑染【编发双辫】

    确定性没错，但那种错误比不一致更伤 ——
    **不一致看起来像模型的问题，荒唐看起来像我们没动脑子。**
    """
    from little_crew.agent.character_sheet import _age_hint, _gender_hint

    check("I1 长称呼优先：老奶奶 → 老年（不被『奶奶』抢先）",
          _age_hint("老奶奶") == "老年", _age_hint("老奶奶"))
    check("I2 奶奶 → 老年", _age_hint("奶奶") == "老年")
    check("I3 父亲/母亲 → 中年",
          _age_hint("父亲") == "中年" and _age_hint("母亲") == "中年")
    check("I4 少女/少年 → 少年",
          _age_hint("少女") == "少年" and _age_hint("少年") == "少年")
    # ⚠️ 用例本身要真的「无线索」。
    # 早期版本用「旅人甲」—— 而它**含「旅人」**，匹配到「青年」是正确行为。
    # 那是测试用例写错了，不是实现有缺陷。
    check("I5 名字无线索时返回空串（不猜）",
          _age_hint("甲乙丙") == "" and _age_hint("X") == ""
          and _age_hint("阿猫") == "",
          f"甲乙丙={_age_hint('甲乙丙')!r} X={_age_hint('X')!r}")
    check("I5b 含称呼的名字会被识别（子串匹配是刻意的）",
          _age_hint("旅人甲") == "青年", _age_hint("旅人甲"))

    check("I6 性别线索：少女→f，父亲→m",
          _gender_hint("少女") == "f" and _gender_hint("父亲") == "m")
    check("I7 中性名不表态（旅人 → 空）", _gender_hint("旅人") == "",
          repr(_gender_hint("旅人")))

    # ★ 端到端：称呼里的年龄必须真的落到外观上
    lao = design_appearance("老奶奶", story_seed="s")
    check("I8 ★ 老奶奶 → 老年（直接采用，不抽样）",
          lao.age_band == "老年", lao.age_band)
    check("I9 ★ 老奶奶不会得到高马尾（年龄启发式生效）",
          lao.hair_style != "高马尾", lao.hair_style)
    check("I10 ★ 老奶奶不会得到及腰直长发",
          lao.hair_style != "及腰直长发", lao.hair_style)

    fu = design_appearance("父亲", story_seed="s")
    check("I11 ★ 父亲 → 男性造型（不会扎双辫）",
          fu.hair_style != "编发双辫", fu.hair_style)
    check("I12 父亲 → 中年", fu.age_band == "中年")

    shao = design_appearance("少女", story_seed="s")
    check("I13 ★ 少女 → 少年（不会得到低盘发）",
          shao.age_band == "少年" and shao.hair_style != "低盘发",
          f"{shao.age_band}/{shao.hair_style}")

    wrong = [(nm, want, design_appearance(nm, story_seed="s").age_band)
             for nm, want in (("老奶奶", "老年"), ("爷爷", "老年"),
                              ("妈妈", "中年"), ("哥哥", "青年"),
                              ("妹妹", "少年"))
             if design_appearance(nm, story_seed="s").age_band != want]
    check("I14 各类称呼的年龄都正确落位", not wrong, str(wrong))

    illegal: list[str] = []
    for nm in ("老奶奶", "父亲", "少女", "少年", "母亲", "爷爷"):
        ap = design_appearance(nm, story_seed="s")
        illegal += [f"{nm}.{k}={v}" for k, v in ap.as_dict().items()
                    if v not in APPEARANCE_OPTIONS[k]]
    check("I15 收窄后仍能取到合法值（没有把选项收空）",
          not illegal, str(illegal[:3]))

    check("I16 加入线索后确定性未被破坏",
          design_appearance("老奶奶", story_seed="s")
          == design_appearance("老奶奶", story_seed="s"))

    seen = {design_appearance(f"角色{i}", story_seed="s").prompt_fragment()
            for i in range(30)}
    check("I17 无线索的角色仍保持多样性（线索没把随机性吃光）",
          len(seen) >= 20, f"30 个得到 {len(seen)} 种")


if __name__ == "__main__":
    raise SystemExit(main())
