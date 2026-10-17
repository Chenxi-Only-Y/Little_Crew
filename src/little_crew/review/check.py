"""确定性验收门禁 —— 自检。

断言的是**机制**：算子的确定性与边界、三态裁定、硬/软/审计分级、
返工指令的可执行性、以及"缺观测不等于通过"这条底线。

运行： python -m little_crew.review.check
"""

from __future__ import annotations

import sys

from little_crew.review import (
    REGISTRY,
    AssertionError_,
    Enforcement,
    Gate,
    GateVerdict,
    Observation,
    UnknownOperatorError,
)

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


def _obs(values, *, aid="A", source="test"):
    return Observation(artifact_id=aid, values=values, source=source)


def _c(ref, op, enforcement="hard", **kw):
    return {"ref": ref, "assertion": {"op": op, **kw},
            "enforcement": enforcement}


# ==========================================================================
# A. 注册表
# ==========================================================================

def _a_registry() -> None:
    check("A1 内置算子已注册（import 即注册，非懒加载）",
          len(REGISTRY.names()) >= 10, f"{len(REGISTRY.names())} 个")

    # 重复注册必须失败 —— 否则"哪个实现在生效"变得不确定
    def _dummy(e, o):  # pragma: no cover
        raise AssertionError_("不应被执行")
    try:
        REGISTRY.register("identity_similarity", _dummy)
        check("A2 ★ 重复注册被拒绝", False, "竟然允许了")
    except AssertionError_ as e:
        check("A2 ★ 重复注册被拒绝（避免静默换掉判定逻辑）",
              "已注册" in str(e), str(e)[:60])

    # 替换必须显式且带新版本
    try:
        REGISTRY.replace("__no_such_op__", _dummy, version="2")
        check("A3 替换未注册算子被拒绝", False)
    except AssertionError_:
        check("A3 替换未注册算子被拒绝", True)

    check("A4 未知算子抛 UnknownOperatorError（不是 KeyError 泛型）",
          isinstance(_try_unknown(), UnknownOperatorError),
          str(_try_unknown())[:60])

    d = REGISTRY.describe()
    check("A5 describe() 给出名称+版本+摘要（UI/文档可用）",
          all({"name", "version", "summary"} <= set(x) for x in d))


def _try_unknown():
    try:
        REGISTRY.get("__definitely_not_registered__")
    except Exception as e:  # noqa: BLE001
        return e
    return None


# ==========================================================================
# B. 算子的确定性与边界
# ==========================================================================

def _b_operators() -> None:
    ver, ident = REGISTRY.get("identity_similarity")

    # 确定性：同输入 100 次同输出
    outs = [ident({"min": 0.82}, {"similarity": 0.7123456789})
            for _ in range(100)]
    check("B1 ★★ identity_similarity 是纯函数（100 次同输入同输出）",
          len({(o.passed, repr(o.observed), repr(o.evidence))
               for o in outs}) == 1)

    # 边界：恰好在阈值上算通过（闭区间）
    check("B2 恰好在阈值上 -> 通过（闭区间语义）",
          ident({"min": 0.82}, {"similarity": 0.82}).passed)
    check("B3 差一点点 -> 不通过",
          not ident({"min": 0.82}, {"similarity": 0.8199999}).passed)

    # 缺参数必须抛错（不能取隐含默认）
    try:
        ident({}, {"similarity": 0.9})
        check("B4 ★ 缺 min 参数时抛错（不取隐含默认阈值）", False)
    except AssertionError_ as e:
        check("B4 ★ 缺 min 参数时抛错（不取隐含默认阈值）",
              "min" in str(e), str(e)[:56])

    # 约束自相矛盾
    try:
        ident({"min": 0.9, "max": 0.5}, {"similarity": 0.7})
        check("B5 min > max 时抛错（约束本身矛盾）", False)
    except AssertionError_:
        check("B5 min > max 时抛错（约束本身矛盾）", True)

    # 缺观测 -> 不通过（不是通过）
    o = ident({"min": 0.5}, {})
    check("B6 ★★ 缺观测值 -> **不通过**（未测 ≠ 通过）",
          not o.passed and "缺观测不等于通过" in o.detail, o.detail[:60])

    # evidence 里必须带方法名 —— 否则换实现无法追溯
    o2 = ident({"min": 0.5}, {"similarity": 0.9, "method": "cosine-v1"})
    check("B7 evidence 记录相似度算法（换实现可追溯）",
          o2.evidence.get("method") == "cosine-v1", str(o2.evidence))

    # ---- 其余算子的关键边界 ----
    ver, dur = REGISTRY.get("duration_within")
    check("B8 duration 上限=下界时恰好通过（10.0 秒对 max=10）",
          dur({"max": 10.0}, {"durationSec": 10.0}).passed)
    check("B9 duration 浮点容差：10.0000001 也算通过（可复现）",
          dur({"max": 10.0}, {"durationSec": 10.0000001}).passed)
    check("B10 duration 明显超限不通过",
          not dur({"max": 10.0}, {"durationSec": 10.5}).passed)

    ver, cnt = REGISTRY.get("count_at_most")
    check("B11 count_at_most 边界（3 <= 3 通过）",
          cnt({"max": 3}, {"count": 3}).passed)
    check("B12 count_at_most 超限不通过（4 > 3）",
          not cnt({"max": 3}, {"count": 4}).passed)

    ver, tok = REGISTRY.get("token_diff")
    check("B13 token_diff 完全相同 -> 差异 0",
          abs(tok({"maxRatio": 0.1},
                  {"script": "你好世界", "produced": "你好世界"}
                  ).observed["diffRatio"]) < 1e-9)
    check("B14 token_diff 完全不同 -> 差异 1.0",
          abs(tok({"maxRatio": 0.1},
                  {"script": "ABCDEF", "produced": "123456"}
                  ).observed["diffRatio"] - 1.0) < 1e-9)
    try:
        tok({"maxRatio": 1.5}, {"script": "a", "produced": "b"})
        check("B15 token_diff 的 maxRatio 越界时抛错", False)
    except AssertionError_:
        check("B15 token_diff 的 maxRatio 越界时抛错", True)

    ver, res = REGISTRY.get("resolution_matches")
    check("B16 resolution 精确匹配",
          res({"width": 832, "height": 480},
              {"width": 832, "height": 480}).passed)
    check("B17 resolution 差 1 像素即不通过（精确语义）",
          not res({"width": 832, "height": 480},
                  {"width": 831, "height": 480}).passed)

    ver, byt = REGISTRY.get("byte_size_within")
    o3 = byt({"minBytes": 1000}, {"bytes": 12})
    check("B18 ★ 文件过小时的提示指向「编码失败」（可诊断）",
          not o3.passed and "编码失败" in o3.detail, o3.detail[:60])

    ver, ne = REGISTRY.get("not_empty")
    for v in ("", [], {}, None):
        if ne({"field": "value"}, {"value": v}).passed:
            check("B19 not_empty 对空值判不通过", False)
            break
    else:
        check("B19 not_empty 对 '' / [] / {} / None 都判不通过", True)


# ==========================================================================
# C. 三态裁定
# ==========================================================================

def _c_verdicts() -> None:
    g = Gate()

    r = g.evaluate([_c("cr1", "identity_similarity", min=0.8)],
                   _obs({"similarity": 0.9}))
    check("C1 全通过 -> KEEP", r.verdict is GateVerdict.KEEP, r.verdict.value)
    check("C2 KEEP 时无返工指令", not r.repairs)

    r = g.evaluate([_c("cr1", "identity_similarity", min=0.8)],
                   _obs({"similarity": 0.5}))
    check("C3 硬门禁不通过 -> RETRY", r.verdict is GateVerdict.RETRY)
    check("C4 RETRY 时产出返工指令", bool(r.repairs))
    check("C5 返工指令带约束 ref（可追溯）",
          r.repairs and "cr1" in r.repairs[0], r.repairs[0][:50] if r.repairs else "")
    check("C6 返工指令说清差多少",
          r.repairs and "0.300" in r.repairs[0], r.repairs[0][:70] if r.repairs else "")

    r = g.evaluate([_c("cr1", "no_such_operator")], _obs({}))
    check("C7 ★★ 未知算子 -> **ESCALATE**，不是 RETRY",
          r.verdict is GateVerdict.ESCALATE, r.verdict.value)
    check("C8 ESCALATE 时**不给返工指令**（判定不可信，返工没意义）",
          not r.repairs)
    check("C9 ESCALATE 说明原因", bool(r.escalations), str(r.escalations[:1])[:60])

    # 约束结构非法 -> ESCALATE（不是 RETRY）
    r = g.evaluate([_c("cr1", "identity_similarity")],  # 缺 min
                   _obs({"similarity": 0.9}))
    check("C10 ★ 约束结构非法 -> ESCALATE（是配置错，不是产物不合格）",
          r.verdict is GateVerdict.ESCALATE, r.verdict.value)

    # ★ ESCALATE 优先于 RETRY
    r = g.evaluate(
        [_c("cr_bad", "no_such_operator"),
         _c("cr_fail", "identity_similarity", min=0.99)],
        _obs({"similarity": 0.1}))
    check("C11 ★★ ESCALATE 优先于 RETRY（判定不可信时不该去返工产物）",
          r.verdict is GateVerdict.ESCALATE, r.verdict.value)


# ==========================================================================
# D. 强制级别
# ==========================================================================

def _d_enforcement() -> None:
    g = Gate()
    hard = Enforcement.HARD.value
    soft = Enforcement.SOFT.value
    audit = Enforcement.AUDIT.value

    r = g.evaluate([_c("cr_soft", "identity_similarity", soft, min=0.99)],
                   _obs({"similarity": 0.1}))
    check("D1 软门禁不通过 -> 仍 KEEP（不阻断）",
          r.verdict is GateVerdict.KEEP, r.verdict.value)
    check("D2 软门禁不通过被记录在 notes",
          any("软门禁" in n for n in r.notes), str(r.notes[:1])[:70])

    r = g.evaluate([_c("cr_audit", "identity_similarity", audit, min=0.99)],
                   _obs({"similarity": 0.1}))
    check("D3 审计级不通过 -> KEEP 且单列（用于阈值标定）",
          r.verdict is GateVerdict.KEEP
          and any("审计级" in n for n in r.notes))

    r = g.evaluate(
        [_c("cr_hard", "identity_similarity", hard, min=0.99),
         _c("cr_soft", "identity_similarity", soft, min=0.99)],
        _obs({"similarity": 0.1}))
    check("D4 硬+软同时不通过 -> RETRY（硬门禁说了算）",
          r.verdict is GateVerdict.RETRY)
    check("D5 只有硬失败进返工指令（软的不进）",
          len(r.repairs) == 1 and "cr_hard" in r.repairs[0],
          f"{len(r.repairs)} 条")

    r = g.evaluate([_c("cr1", "identity_similarity", min=0.5)],
                   _obs({"similarity": 0.9}, source="ffprobe"))
    check("D6 断言记录观测来源（可追溯数字从哪来）",
          r.assertions[0].evidence.get("observedSource") == "ffprobe",
          str(r.assertions[0].evidence))

    check("D7 断言记录算子版本（换了实现能看出来）",
          r.assertions[0].evidence.get("operatorVersion") == "1",
          str(r.assertions[0].evidence))


# ==========================================================================
# E. 确定性（类层面）
# ==========================================================================

def _e_determinism() -> None:
    g = Gate()
    constraints = [
        _c("cr_a", "identity_similarity", min=0.8),
        _c("cr_b", "duration_within", max=10.0),
        _c("cr_c", "resolution_matches", width=832, height=480),
        _c("cr_d", "no_such_operator"),
    ]
    obs = _obs({"similarity": 0.7, "durationSec": 12.0,
                "width": 832, "height": 480})

    dicts = []
    for _ in range(50):
        dicts.append(repr(g.evaluate(constraints, obs).as_dict()))
    check("E1 ★★ 门禁无状态：50 次求值结果完全一致",
          len(set(dicts)) == 1)

    g2 = Gate()
    check("E2 不同实例给出同一结论（不依赖实例状态）",
          repr(g2.evaluate(constraints, obs).as_dict()) == dicts[0])

    # 约束顺序不该改变结论
    r1 = g.evaluate(constraints, obs)
    r2 = g.evaluate(list(reversed(constraints)), obs)
    check("E3 约束顺序不改变裁定",
          r1.verdict is r2.verdict, f"{r1.verdict.value} vs {r2.verdict.value}")

    # 报告可序列化
    d = r1.as_dict()
    import json
    check("E4 报告可 JSON 序列化（进交付证据链）",
          isinstance(json.dumps(d, ensure_ascii=False), str))
    check("E5 报告含计数汇总，且**未被检查的约束也在其中**",
          "counts" in d and d["counts"]["total"] == 4,
          str(d.get("counts")))
    # ★ 这条锁住一个真实缺陷：未知算子的约束曾**从报告里消失** ——
    # 4 条约束的报告只列 3 条，而缺的那条恰是"没被检查"的那条。
    unevaluated = [a for a in r1.assertions
                   if not a.evidence.get("evaluated", False)]
    check("E6 ★★ 无法判定的约束**仍出现在报告里**（不静默消失）",
          len(unevaluated) == 1
          and unevaluated[0].constraint_ref == "cr_d",
          f"{len(unevaluated)} 条未判定")
    check("E7 未判定的断言标注了原因",
          unevaluated
          and unevaluated[0].evidence.get("reason") == "unknown_operator",
          str(unevaluated[0].evidence) if unevaluated else "")


# ==========================================================================
# F. 与 IR 约束的对接
# ==========================================================================

def _f_ir_binding() -> None:
    """IR 的约束边 -> 断言。这是 C1「约束边变可执行断言」的兑现点。"""
    import copy

    from little_crew.ir.canonical import content_hash, normalize

    ir = {
        "irVersion": "little_crew.ir/v1",
        "graphId": "G-ep001-sh005",
        "scope": "shot",
        "nodes": [
            {"id": "n_ckpt", "kind": "model.checkpoint",
             "params": {"ckpt_name": "sd_xl_base_1.0.safetensors"}},
        ],
        "edges": [],
        "constraints": [
            {"ref": "cr_face_007",
             "assertion": {"op": "identity_similarity",
                           "characterRef": "CHAR-0007", "min": 0.82},
             "enforcement": "hard",
             "description": "本镜必须长得像角色 7"},
            {"ref": "cr_dur_001",
             "assertion": {"op": "duration_within", "max": 10.0},
             "enforcement": "soft"},
        ],
        "bindings": {},
    }
    canon = normalize(ir)
    # ⚠️ 规范化**按 ref 排序**，所以不能用下标假定顺序 ——
    # 用查找。（`normalize` 的排序是刻意的：让哈希与顺序无关。）
    by_ref = {c["ref"]: c for c in canon["constraints"]}
    check("F1 IR 约束经规范化后保留 ref/assertion/enforcement",
          set(by_ref) == {"cr_face_007", "cr_dur_001"}
          and by_ref["cr_face_007"]["enforcement"] == "hard"
          and "description" in by_ref["cr_face_007"],
          str(sorted(by_ref)))

    check("F2 IR 哈希对约束**敏感**（改阈值 -> 换 hash）",
          content_hash(ir) != content_hash(_bump(ir)),
          "改 min 0.82->0.90 后哈希未变")

    g = Gate()
    r = g.evaluate(canon["constraints"],
                   _obs({"similarity": 0.71, "durationSec": 8.0}))
    check("F3 IR 的约束能直接喂给门禁",
          len(r.assertions) == 2, f"{len(r.assertions)} 条")
    check("F4 硬约束失败 -> RETRY，软约束通过不干扰",
          r.verdict is GateVerdict.RETRY, r.verdict.value)
    face = next((a for a in r.assertions
                 if a.constraint_ref == "cr_face_007"), None)
    check("F5 断言保留了 IR 里的 characterRef（语义可追溯）",
          face is not None and face.expected.get("characterRef") == "CHAR-0007",
          str(face.expected) if face else "(未找到 cr_face_007)")


def _bump(ir):
    import copy
    o = copy.deepcopy(ir)
    o["constraints"][0]["assertion"]["min"] = 0.90
    return o


# ---------- I. cast_consistent_with_text（2026-10-17 新增算子）----------
#
# ## 这一段守什么
#
# 跑整集时发现的**真实缺陷**：某镜 `characters = ["少女","旅人"]`，
# 而描述写的是"少女 **独自** 走在山坡上采花"。
# 外观块按角色表注入两份外貌 -> 模型画出两个人 ->
# **画面错了，而分辨率/时长/哈希全部通过。**
#
# 这类矛盾**不需要看图**：比对"文字说了几个人"与"结构声明了几个人"就够。
#
# ## 刻意不查的方向
#
# 「描述提到某个角色但他不在角色表里」——看似该查，其实**经常合法**：
# `她想起母亲` 里的母亲并不在画面里。默认查它会产生大量误报，
# 而**误报会让断言失去信任**（一条不被信的断言比没有更糟）。
# 所以那个方向做成 `checkUnlisted` 显式开启。

def _i_cast_text() -> None:
    from little_crew.review.ops import cast_consistent_with_text as f

    def run(chars, desc, **exp):
        return f(exp, {"characters": chars, "description": desc})

    # ---- 核心：复现真实缺陷 ----
    r = run(["少女", "旅人"], "少女 独自走在山坡上采花")
    check("I1 ★ 描述说「独自」而角色表 2 人 -> 不通过（复现真实缺陷）",
          not r.passed, r.detail[:80])
    check("I2 失败说明写清了「会画出两个人」这个后果",
          "2 个人" in r.detail or "画出" in r.detail, r.detail[:100])

    # ---- 不该误报 ----
    check("I3 描述说「独自」且角色表 1 人 -> 通过",
          run(["少女"], "少女 独自走在山坡上采花").passed)
    check("I4 ★「一个人影」不算「独自」（避免误报）",
          run(["少女", "旅人"], "远处有一个人影").passed,
          run(["少女", "旅人"], "远处有一个人影").detail)
    check("I5 无任何标记的多角色镜头 -> 通过",
          run(["少女", "旅人"], "两人在田里干活").passed)

    # ---- 显式人数 ----
    r2 = run(["少女"], "两个人并肩站着")
    check("I6 描述写「两个人」但角色表 1 人 -> 不通过", not r2.passed, r2.detail[:70])
    check("I7 描述写「两个人」且角色表 2 人 -> 通过",
          run(["少女", "旅人"], "两个人并肩站着").passed)
    check("I8 描述写「三个人」但角色表 2 人 -> 不通过",
          not run(["少女", "旅人"], "三个人围坐").passed)

    # ---- 缺观测：不通过（铁律①）----
    r3 = f({}, {"description": "少女独自走"})
    check("I9 缺 characters -> 不通过（缺观测不等于通过）",
          not r3.passed and "缺失" in r3.detail, r3.detail[:70])
    r4 = f({}, {"characters": ["少女"]})
    check("I10 缺 description -> 不通过", not r4.passed)

    # ---- 可选方向：checkUnlisted ----
    r5 = f({"checkUnlisted": True},
           {"characters": ["少女"], "description": "少女看见母亲站在门口",
            "castNames": ["少女", "母亲"]})
    check("I11 ★ checkUnlisted 开启 -> 提到但未列入 -> 不通过",
          not r5.passed and "母亲" in r5.detail, r5.detail[:70])
    check("I12 checkUnlisted 关闭（默认）-> 同一情形通过（不误报）",
          f({}, {"characters": ["少女"], "description": "少女看见母亲站在门口"}).passed)
    r6 = f({"checkUnlisted": True}, {"characters": ["少女"],
                                    "description": "少女站着"})
    check("I13 checkUnlisted 开启但没给 castNames -> 不通过（缺观测）",
          not r6.passed, r6.detail[:70])

    # ---- 字符串形式的 characters ----
    check("I14 characters 是逗号分隔字符串也能解析",
          not run("少女，旅人", "少女独自走").passed)

    # ---- 算子已注册、可经门禁调用 ----
    from little_crew.review.constraint import REGISTRY
    check("I15 算子已注册到 REGISTRY", "cast_consistent_with_text" in REGISTRY.names())


# ---------- J. audio_track_present（2026-10-18 新增算子）----------
#
# ## 为什么这一段守
#
# `minimax-h3` 的 `capability.native_audio=True` 早就声明了，
# 但在此之前**没有任何断言能验证"成片真的有声音"**。
# **声明与验证是两件事** —— 少了这一层，"加了音轨"永远只是一句话。
#
# ## 判据来自容器字节，不依赖 ffprobe
#
# 本机没装 ffprobe（实测 FileNotFoundError），所以
# `observe._media_flags` 自己看容器：MP4 找 `soun` / `mp4a`，
# Matroska 找 `A_*`，WAV 直接算有。

def _j_audio() -> None:
    from little_crew.review.observe import _media_flags
    from little_crew.review.ops import audio_track_present as f

    check("J1 有音轨 + 有视频轨 -> 通过",
          f({}, {"hasAudio": True, "hasVideo": True, "container": "mp4"}).passed)
    check("J2 ★ 没有音轨 -> 不通过",
          not f({}, {"hasAudio": False, "hasVideo": True,
                     "container": "mp4"}).passed)
    check("J3 只有音轨没有画面 -> 不通过（只有声音不构成成片）",
          not f({}, {"hasAudio": True, "hasVideo": False,
                     "container": "wav"}).passed)
    check("J4 requireVideo=false 时纯音频可以过",
          f({"requireVideo": False},
            {"hasAudio": True, "hasVideo": False, "container": "wav"}).passed)
    r = f({}, {"width": 832, "height": 480})
    check("J5 ★ 图像产物测不了音轨 -> 不通过（缺观测不等于通过）",
          not r.passed and "缺失" in r.detail, r.detail[:70])

    # ---- 容器探测本身 ----
    mp4 = (b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00"
           b"isomiso2avc1mp41" + b"\x00" * 40
           + b"moov" + b"trak" + b"vide" + b"trak" + b"soun" + b"mp4a")
    fl = _media_flags(mp4)
    check("J6 ★ MP4 字节里能查出视频轨与音轨",
          fl.get("container") == "mp4" and fl.get("hasVideo") is True
          and fl.get("hasAudio") is True, str(fl))

    mp4_noaudio = (b"\x00\x00\x00\x20ftypisom" + b"\x00" * 40
                   + b"moov" + b"trak" + b"vide")
    fl2 = _media_flags(mp4_noaudio)
    check("J7 无音轨的 MP4 -> hasAudio=False",
          fl2.get("hasAudio") is False, str(fl2))

    wav = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 20
    fl3 = _media_flags(wav)
    check("J8 WAV -> 有音频、无视频",
          fl3.get("hasAudio") is True and fl3.get("hasVideo") is False,
          str(fl3))

    check("J9 非媒体字节 -> 不产出任何字段（不编造）",
          _media_flags(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40) == {},
          str(_media_flags(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)))

    check("J10 太短的字节 -> 不崩",
          _media_flags(b"\x00\x01") == {})

    from little_crew.review.constraint import REGISTRY
    check("J11 算子已注册", "audio_track_present" in REGISTRY.names())


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    print("=" * 78)
    print("确定性验收门禁 —— 自检")
    print("=" * 78)

    _a_registry()
    _b_operators()
    _c_verdicts()
    _d_enforcement()
    _i_cast_text()
    _j_audio()
    _e_determinism()
    _f_ir_binding()
    # L3 盲读算子 + 门禁对它的响应
    _h_operator()
    _h_gate_response()

    print()
    npass = sum(1 for _, ok, _ in _results if ok)
    for name, ok, detail in _results:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 78)
    print(f"结果：{npass}/{len(_results)} 通过")
    return 0 if npass == len(_results) else 1


# ==========================================================================
# H. L3 跨模型族盲读（C3 的第三层）
# ==========================================================================
#
# ⚠️ 这几段**必须在 `review` 层测**，不能放进 `agent/blind_check.py`：
# `agent`(6) 不能 import `review`(7) —— 架构守卫强制。
# 而"盲读结论如何影响门禁裁定"是**门禁的行为**，归属 review。

BLIND_CONS = [{"ref": "cr_blind", "enforcement": "hard",
               "assertion": {"op": "blind_read_verdict",
                             "allow": ["PASS"]}}]


def _blind_op():
    return REGISTRY.get("blind_read_verdict")[1]


def _h_operator() -> None:
    op = _blind_op()

    out = op({"allow": ["PASS"]}, {"blindVerdict": "PASS"})
    check("H1 PASS 裁定 -> 通过", out.passed, str(out.observed))
    check("H2 证据里带审核方与生成方模型（可复核跨族）",
          "reviewerModel" in out.observed and "generatorModel" in out.observed,
          str(out.observed))

    out = op({"allow": ["PASS"]}, {"blindVerdict": "REVISE"})
    check("H3 ★ REVISE -> 不通过（这是返工信号）",
          not out.passed, str(out.observed))
    check("H4 且**不是** inconclusive（产物真的不合格）",
          not out.inconclusive, str(out.inconclusive))

    # ★★ 判定作废 ≠ 产物不合格
    out = op({"allow": ["PASS"]},
             {"blindVerdict": "", "blindInvalidReason": "与生成方同族"})
    check("H5 ★★ 判定作废 -> 不通过", not out.passed, str(out.observed))
    check("H6 ★★ 且标 `invalidated`，与「不合格」区分开",
          out.observed.get("invalidated") is True, str(out.observed))
    check("H7 ★★ `inconclusive=True`（门禁据此走 ESCALATE 而非 RETRY）",
          out.inconclusive, str(out.inconclusive))
    check("H8 ★★ detail 明确否认这是产物不合格",
          "不是" in (out.detail or "") and "不合格" in (out.detail or ""),
          (out.detail or "")[:96])

    # ★★ 未知裁定绝不能当成通过
    out = op({"allow": ["PASS"]}, {"blindVerdict": "looks fine"})
    check("H9 ★★ 未知裁定 -> 不通过（不能读成「没问题」）",
          not out.passed, str(out.observed))
    check("H10 且 `inconclusive=True`（是这次交互出问题，不是产物坏）",
          out.inconclusive, str(out.inconclusive))
    check("H11 提示里列出合法取值集",
          "PASS" in (out.detail or ""), (out.detail or "")[:96])

    out = op({"allow": ["PASS"]}, {"blindVerdict": "  pass  "})
    check("H12 大小写/空白被规范化（模型输出不规整）",
          out.passed, str(out.observed))

    out = op({"allow": ["PASS"]}, {})
    check("H13 ★ 缺观测 -> 不通过（「没测」不等于「没问题」）",
          not out.passed and out.observed.get("available") is False,
          str(out.observed))

    out = op({"allow": ["PASS"]}, {"blindVerdict": "ESCALATE"})
    check("H14 ESCALATE 裁定 -> 不通过", not out.passed, str(out.observed))

    out = op({"allow": ["PASS", "REVISE"]}, {"blindVerdict": "REVISE"})
    check("H15 `allow` 放宽后 REVISE 可通过（有意的降级）",
          out.passed, str(out.observed))


def _h_gate_response() -> None:
    # 本文件在 `review` 层内部，可以直连 `GateRunner`
    from little_crew.review.gate_runner import GateRunner

    cons = BLIND_CONS

    rep = GateRunner().run(constraints_by_shot={"SH-1": cons},
                           artifacts_by_shot={"SH-1": {}},
                           resolve=None, context_by_shot={})
    check("H16 ★★ 无盲读观测 -> 约束**被跳过**并留审计（非静默通过）",
          not rep.results and bool(rep.skipped),
          f"裁定 {len(rep.results)} / 跳过 {len(rep.skipped)}")
    check("H17 跳过原因指明缺哪个观测",
          any("blindVerdict" in x for x in rep.skipped),
          str(rep.skipped[:1])[:88])

    rep = GateRunner().run(
        constraints_by_shot={"SH-1": cons},
        artifacts_by_shot={"SH-1": {}}, resolve=None,
        context_by_shot={"SH-1": {"blindVerdict": "PASS"}})
    check("H18 盲读 PASS -> KEEP",
          bool(rep.results) and rep.results[0].passed,
          f"kept={rep.kept} retry={rep.retry}")

    rep = GateRunner().run(
        constraints_by_shot={"SH-1": cons},
        artifacts_by_shot={"SH-1": {}}, resolve=None,
        context_by_shot={"SH-1": {"blindVerdict": "REVISE"}})
    r = rep.results[0]
    check("H19 ★★ 盲读 REVISE -> **RETRY**（产物不合格 -> 返工）",
          r.verdict.value == "RETRY", r.verdict.value)
    check("H20 且给出返工指令", bool(r.repairs), str(r.repairs[:1])[:80])

    # ★★ 最关键的一条：作废 -> ESCALATE，且**不给返工指令**
    rep = GateRunner().run(
        constraints_by_shot={"SH-1": cons},
        artifacts_by_shot={"SH-1": {}}, resolve=None,
        context_by_shot={"SH-1": {"blindVerdict": "",
                                    "blindInvalidReason": "与生成方同族"}})
    r = rep.results[0]
    check("H21 ★★ 判定作废 -> **ESCALATE**（不是 RETRY）",
          r.verdict.value == "ESCALATE", r.verdict.value)
    check("H22 ★★ 且**不给返工指令**（不能基于不可信的观察指挥返工）",
          not r.repairs, str(r.repairs))
    check("H23 升级原因写进证据", bool(r.escalations),
          str(r.escalations[:1])[:96])

    rep = GateRunner().run(
        constraints_by_shot={"SH-1": cons},
        artifacts_by_shot={"SH-1": {}}, resolve=None,
        context_by_shot={"SH-1": {"blindVerdict": "???"}})
    check("H24 ★★ 未知裁定 -> **ESCALATE**（不猜成通过，也不猜成不合格）",
          rep.results[0].verdict.value == "ESCALATE",
          rep.results[0].verdict.value)
    check("H25 且不给返工指令", not rep.results[0].repairs,
          str(rep.results[0].repairs)[:80])

    # 与 L2 的对照：审计级不阻断，硬级阻断
    audit = [{"ref": "cr_b", "enforcement": "audit",
              "assertion": {"op": "blind_read_verdict",
                            "allow": ["PASS"]}}]
    rep = GateRunner().run(
        constraints_by_shot={"SH-1": audit},
        artifacts_by_shot={"SH-1": {}}, resolve=None,
        context_by_shot={"SH-1": {"blindVerdict": "REVISE"}})
    check("H26 `audit` 级盲读失败**不阻断**（级别语义一致）",
          rep.results[0].verdict.value == "KEEP",
          rep.results[0].verdict.value)


if __name__ == "__main__":
    raise SystemExit(main())
