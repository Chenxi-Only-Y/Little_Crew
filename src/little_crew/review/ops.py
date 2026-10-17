"""确定性断言算子（C3 的 L1 层）。

铁律
----
**每个算子都必须是纯函数**：`(expected, observed) -> AssertionOutcome`。

    同输入 -> 必然同输出，且必然同 evidence

因此算子**不做 IO、不读时间、不用随机**。观测值由调用方（`gate.py`）
从产物里取好后传进来 —— 这样算子可以：
  · 用假数据做穷尽的单元测试（不需要真产物）
  · 在报告里被反复复核（改了阈值可以回算）
  · 被引用为"可执行断言"的语义定义

凡是不满足这条的都不允许注册（见 `constraint.OperatorRegistry`）。

算子契约
--------
`expected` 来自 IR 的约束 `assertion` 字段（键名与算子同名参数）。
`observed` 由调用方按算子要求提取。

**缺观测值时不通过，不是通过。** 这一点是刻意的：
"没测"与"测了没问题"必须区分 —— 混为一谈就是自我签收。
"""

from __future__ import annotations

from typing import Any, Mapping

from little_crew.review.constraint import (
    AssertionError_,
    AssertionOutcome,
    register,
)

__all__ = ["ensure_registered"]


def _num(d: Mapping[str, Any], key: str, *, where: str) -> float:
    """取一个必须存在的数值参数。

    **缺失即抛错**（而不是取默认值）—— 约束少写一个 `min` 就静默用一个
    隐含默认阈值，那是最危险的一类配置错误：报告看起来正常，判定却错了。
    """
    if key not in d:
        raise AssertionError_(f"{where} 缺少必需参数 {key!r}")
    try:
        return float(d[key])
    except (TypeError, ValueError) as e:
        raise AssertionError_(
            f"{where} 的参数 {key!r} 不是数值: {d[key]!r}") from e


def _optional_num(d: Mapping[str, Any], key: str, default: float) -> float:
    return float(d[key]) if key in d else default


def _missing(what: str) -> AssertionOutcome:
    """观测值缺失的统一结果：**不通过**。

    见模块 docstring —— "没测"不等于"没问题"。
    """
    return AssertionOutcome(
        passed=False,
        observed={"available": False},
        detail=f"观测值缺失：{what}。**缺观测不等于通过** —— "
               f"未测的约束不能算已满足",
    )


# ---------------------------------------------------------------------------
# 相似度类
# ---------------------------------------------------------------------------

@register("identity_similarity", version="1")
def identity_similarity(expected: Mapping[str, Any],
                        observed: Mapping[str, Any]) -> AssertionOutcome:
    """身份相似度 >= 阈值。用于「这个镜头必须长得像角色 7」。

    期望参数：`min`（阈值，必填）、可选 `max`（上限，默认 1.0）。
    观测参数：`similarity`（0~1 的相似度，必填）。

    ⚠️ **相似度由谁算是调用方的事** —— 可以是嵌入余弦、可以用人脸模型。
    本算子只负责**阈值判定**，这样换相似度实现不必动门禁逻辑。
    但相似度实现必须写进 `evidence`，否则"换了实现导致通过率变了"无法追溯。
    """
    if "similarity" not in observed:
        return _missing("identity_similarity 需要 observed['similarity']")

    lo = _num(expected, "min", where="identity_similarity")
    hi = _optional_num(expected, "max", 1.0)
    sim = float(observed["similarity"])

    if lo > hi:
        raise AssertionError_(
            f"identity_similarity 的 min({lo}) 大于 max({hi})，约束本身矛盾")

    passed = lo <= sim <= hi
    return AssertionOutcome(
        passed=passed,
        observed={"similarity": sim, "min": lo, "max": hi,
                  # ⚠️ **算法名必须进 observed。**
                  # ops 的文档要求"相似度实现必须写进 evidence，
                  # 否则换了实现导致通过率变了无法追溯" ——
                  # 只放进 vidence 不够：认定不合格时人要能直接从
                  # 断言记录里看到"这个数是哪个算法算的"。
                  "method": observed.get("method", "unspecified"),
                  "weakestDimension": observed.get("weakestDimension", ""),
                  "otherShotId": observed.get("otherShotId", "")},
        evidence={
            # 记下"这个相似度是怎么算出来的" —— 换实现要能看出来
            "method": observed.get("method", "unspecified"),
            "sampleRef": observed.get("sampleRef", ""),
        },
        detail=("" if passed else
                f"身份相似度 {sim:.3f} 不在 [{lo:.3f}, {hi:.3f}] 内"
                + (f"（差 {lo - sim:.3f}）" if sim < lo else
                   f"（超上限 {sim - hi:.3f}）")),
    )


@register("pairwise_similarity", version="1")
def pairwise_similarity(expected: Mapping[str, Any],
                        observed: Mapping[str, Any]) -> AssertionOutcome:
    """两个产物之间的相似度 >= 阈值。用于**跨镜一致性**。

    与 `identity_similarity` 的区别：
      · `identity_similarity` —— 产物 vs **角色参考**（绝对基准）
      · `pairwise_similarity` —— 产物 vs **另一个产物**（相对一致）

    跨镜漂移靠后者才测得出来：单看每一镜都可能"像角色"，
    但相邻两镜之间已经不像同一个人了。

    期望参数：`min`（必填）。观测参数：`similarity`、`otherShotId`（建议给）。
    """
    if "similarity" not in observed:
        return _missing("pairwise_similarity 需要 observed['similarity']")

    lo = _num(expected, "min", where="pairwise_similarity")
    sim = float(observed["similarity"])
    passed = sim >= lo
    return AssertionOutcome(
        passed=passed,
        observed={"similarity": sim, "min": lo,
                  "otherShotId": observed.get("otherShotId", ""),
                  "method": observed.get("method", "unspecified"),
                  "weakestDimension": observed.get(
                      "weakestDimension", "")},
        evidence={"method": observed.get("method", "unspecified")},
        detail=("" if passed else
                f"与 {observed.get('otherShotId') or '对照镜'} 的相似度 "
                f"{sim:.3f} < {lo:.3f}（差 {lo - sim:.3f}）—— **跨镜漂移**"),
    )


# ---------------------------------------------------------------------------
# 文本类
# ---------------------------------------------------------------------------

@register("token_diff", version="1")
def token_diff(expected: Mapping[str, Any],
               observed: Mapping[str, Any]) -> AssertionOutcome:
    """文本 token 差异不超过上限。用于**台词/字幕与剧本一致**。

    期望参数：`maxRatio`（相对差异上限，0~1，必填）。
    观测参数：`script`（剧本原文）、`produced`（产出文本）。

    用 **difflib 的 ratio**（`1 - 相似度`）而不是编辑距离的绝对值 ——
    绝对距离对长度敏感，一段 100 字的台词和一段 10 字的台词
    用同一个绝对阈值毫无意义。
    """
    import difflib

    if "script" not in observed or "produced" not in observed:
        return _missing("token_diff 需要 observed['script'] 与 observed['produced']")

    max_ratio = _num(expected, "maxRatio", where="token_diff")
    if not 0.0 <= max_ratio <= 1.0:
        raise AssertionError_(f"token_diff 的 maxRatio 必须在 0~1，收到 {max_ratio}")

    a = str(observed["script"])
    b = str(observed["produced"])
    ratio = 1.0 - difflib.SequenceMatcher(None, a, b).ratio()
    passed = ratio <= max_ratio

    return AssertionOutcome(
        passed=passed,
        observed={"diffRatio": round(ratio, 6),
                  "maxRatio": max_ratio,
                  "scriptLen": len(a), "producedLen": len(b)},
        evidence={"script": a[:200], "produced": b[:200]},
        detail=("" if passed else
                f"文本差异 {ratio:.1%} > 上限 {max_ratio:.1%}"
                f"（剧本 {len(a)} 字 / 产出 {len(b)} 字）"),
    )


# ---------------------------------------------------------------------------
# 计数 / 时长 / 分辨率
# ---------------------------------------------------------------------------

@register("count_at_least", version="1")
def count_at_least(expected: Mapping[str, Any],
                   observed: Mapping[str, Any]) -> AssertionOutcome:
    """数量至少为 N。用于「一集至少 20 镜」这类完整性约束。

    期望参数：`min`（必填）、可选 `field`（观测里取哪个字段，默认 `count`）。
    """
    field = str(expected.get("field", "count"))
    if field not in observed:
        return _missing(f"count_at_least 需要 observed[{field!r}]")

    lo = _num(expected, "min", where="count_at_least")
    n = float(observed[field])
    passed = n >= lo
    return AssertionOutcome(
        passed=passed,
        observed={field: n, "min": lo},
        detail=("" if passed else f"{field} = {n:g} < 要求的 {lo:g}"),
    )


@register("count_at_most", version="1")
def count_at_most(expected: Mapping[str, Any],
                  observed: Mapping[str, Any]) -> AssertionOutcome:
    """数量至多为 N。用于「同框角色不超过 3 人」这类上限约束。"""
    field = str(expected.get("field", "count"))
    if field not in observed:
        return _missing(f"count_at_most 需要 observed[{field!r}]")

    hi = _num(expected, "max", where="count_at_most")
    n = float(observed[field])
    passed = n <= hi
    return AssertionOutcome(
        passed=passed,
        observed={field: n, "max": hi},
        detail=("" if passed else f"{field} = {n:g} > 上限 {hi:g}"),
    )


@register("duration_within", version="1")
def duration_within(expected: Mapping[str, Any],
                    observed: Mapping[str, Any]) -> AssertionOutcome:
    """时长在 [min, max] 秒内。

    期望参数：`min`、`max`（至少给一个）。
    观测参数：`durationSec`。

    ⚠️ 浮点比较用**半开区间的容差**（1e-6）—— 时长 10.0000001 与上限 10.0
    在数值上不等，但语义上就是"刚好 10 秒"。不容差的话，
    同一份产物在两次测量间会随机地通过/不通过（取决于第 7 位小数），
    而门禁的判定**必须可复现**。
    """
    if "durationSec" not in observed:
        return _missing("duration_within 需要 observed['durationSec']")

    d = float(observed["durationSec"])
    tol = 1e-6
    lo = _optional_num(expected, "min", 0.0)
    hi = _optional_num(expected, "max", float("inf"))
    if "min" not in expected and "max" not in expected:
        raise AssertionError_("duration_within 至少要给 min 或 max 之一")

    passed = (d >= lo - tol) and (d <= hi + tol)
    bound = f"[{lo:g}, {'∞' if hi == float('inf') else f'{hi:g}'}]"
    return AssertionOutcome(
        passed=passed,
        observed={"durationSec": round(d, 6), "min": lo,
                  "max": None if hi == float("inf") else hi},
        detail=("" if passed else f"时长 {d:.3f}s 不在 {bound} 秒内"),
    )


@register("resolution_matches", version="1")
def resolution_matches(expected: Mapping[str, Any],
                       observed: Mapping[str, Any]) -> AssertionOutcome:
    """分辨率精确匹配。期望/观测都用 `width`、`height`。"""
    for k in ("width", "height"):
        if k not in observed:
            return _missing(f"resolution_matches 需要 observed[{k!r}]")
        if k not in expected:
            raise AssertionError_(f"resolution_matches 的 expected 缺少 {k!r}")

    w, h = int(observed["width"]), int(observed["height"])
    ew, eh = int(expected["width"]), int(expected["height"])
    passed = (w, h) == (ew, eh)
    return AssertionOutcome(
        passed=passed,
        observed={"width": w, "height": h, "expectedWidth": ew,
                  "expectedHeight": eh},
        detail=("" if passed else f"分辨率 {w}x{h} != 要求 {ew}x{eh}"),
    )


@register("aspect_ratio_within", version="1")
def aspect_ratio_within(expected: Mapping[str, Any],
                        observed: Mapping[str, Any]) -> AssertionOutcome:
    """宽高比在目标值的容差内。用于「必须 16:9 ±1%」这类约束。

    期望参数：`ratio`（目标宽高比，必填）、可选 `tolerance`（相对容差，默认 0.01）。
    """
    for k in ("width", "height"):
        if k not in observed:
            return _missing(f"aspect_ratio_within 需要 observed[{k!r}]")

    target = _num(expected, "ratio", where="aspect_ratio_within")
    tol = _optional_num(expected, "tolerance", 0.01)
    w, h = int(observed["width"]), int(observed["height"])
    if h == 0:
        return AssertionOutcome(passed=False, observed={"height": 0},
                                detail="高度为 0，宽高比无定义")
    actual = w / h
    rel = abs(actual - target) / target if target else float("inf")
    passed = rel <= tol
    return AssertionOutcome(
        passed=passed,
        observed={"ratio": round(actual, 6), "target": target,
                  "relativeError": round(rel, 6), "tolerance": tol},
        detail=("" if passed else
                f"宽高比 {actual:.4f} 偏离目标 {target:.4f} 达 {rel:.2%}"
                f"（容差 {tol:.2%}）"),
    )


# ---------------------------------------------------------------------------
# 文件完整性
# ---------------------------------------------------------------------------

@register("byte_size_within", version="1")
def byte_size_within(expected: Mapping[str, Any],
                     observed: Mapping[str, Any]) -> AssertionOutcome:
    """文件大小在 [minBytes, maxBytes] 内。

    **用途不只是"别太大"** —— 它同时是一个**廉价的完整性检查**：
    一个 0 字节或几百字节的 mp4 说明编码根本没成功，
    而这类失败在下游（播放器/合成）才会暴露。放在门禁里能早一步拦住。
    """
    if "bytes" not in observed:
        return _missing("byte_size_within 需要 observed['bytes']")

    n = int(observed["bytes"])
    # ⚠️ 上限用 `None` 表示"无上限"，**不能用 `float('inf')` 再 int()** ——
    # `int(float('inf'))` 抛 OverflowError。这个 bug 是自检抓出来的：
    # 只给下限（`minBytes`）而调用 `int(inf)` 会直接崩，
    # 而"只关心文件别太小"恰恰是最常见的用法。
    hi_raw = expected.get("maxBytes")
    hi = int(hi_raw) if hi_raw is not None else None
    lo = int(_optional_num(expected, "minBytes", 0))
    passed = n >= lo and (hi is None or n <= hi)
    return AssertionOutcome(
        passed=passed,
        observed={"bytes": n, "minBytes": lo, "maxBytes": hi},
        detail=("" if passed else
                (f"文件仅 {n} 字节，低于下限 {lo} —— **很可能是编码失败**"
                 if n < lo else f"文件 {n} 字节，超过上限 {hi}")),
    )


@register("not_empty", version="1")
def not_empty(expected: Mapping[str, Any],
              observed: Mapping[str, Any]) -> AssertionOutcome:
    """观测值非空。最基础也最常被漏掉的一条。

    期望参数：`field`（要检查的字段，默认 `value`）。

    为什么值得单独一个算子：`count_at_least(min=1)` 对一个**不存在的字段**
    会返回"缺失 -> 不通过"，这没问题；但对一个**空列表** `[]` 会取
    `len` 么？不会 —— 它取的是字段值本身。空列表转 float 会抛类型错。
    专门一个算子的语义最清晰：**这里有个东西，而且它不是空的。**
    """
    field = str(expected.get("field", "value"))
    if field not in observed:
        return _missing(f"not_empty 需要 observed[{field!r}]")

    v = observed[field]
    empty = (v is None or v == "" or v == [] or v == {})
    return AssertionOutcome(
        passed=not empty,
        observed={field: (f"<{type(v).__name__} 长度 {len(v)}>"
                          if hasattr(v, "__len__") else v)},
        detail=("" if not empty else f"{field} 为空"),
    )


# ---------------------------------------------------------------------------
# 盲读类（C3 的 L3）
# ---------------------------------------------------------------------------

#: 盲读裁定的封闭取值集。
#:
#: ⚠️ **必须是封闭集** —— 开放取值会让"未知的裁定"被当成"通过"。
#: 具体说：若模型返回了一个没见过的词（"looks fine"），
#: 宽松的判定会当成"没问题"，而那正是**最危险的失败方向**。
BLIND_VERDICTS: frozenset[str] = frozenset(
    {"PASS", "REVISE", "ESCALATE", "REJECT"})


@register("blind_read_verdict", version="1")
def blind_read_verdict(expected: Mapping[str, Any],
                       observed: Mapping[str, Any]) -> AssertionOutcome:
    """跨模型族盲读的裁定必须是「通过」。

    这是 C3（确定性验收门禁）的 **L3 层**：
    前两层（L1 断言算子 / L2 一致性嵌入）都是**确定性**的，
    它们能查的东西是有限的（分辨率、时长、配色漂移…）。
    剩下那部分**语义质量**——"这个人的手是不是画坏了"——
    只能由视觉模型去看。

    期望参数：
      · `allow`（可选）：允许的裁定集合，默认 `["PASS"]`。
        把它放宽（如 `["PASS","REVISE"]`）是**有意的降级**，
        须在约束的 `description` 里说明理由。

    观测参数（由 `agent` 层注入，见下方"为什么是注入"）：
      · `blindVerdict`（必填）：`PASS` / `REVISE` / `ESCALATE` / `REJECT`
      · `blindReviewerModel`：审核方模型 id
      · `blindGeneratorModel`：生成方模型 id
      · `blindInvalidReason`：判定作废的原因（若有）
      · `blindBlocking`：阻断级问题的条数（可选，进证据）

    为什么是**注入观测**而不是门禁去调模型
    --------------------------------------
    `review` 层**不能 import `agent`**（架构守卫强制，见
    `test_architecture.py` 的层序表）—— 审核方在下、决策方在上，
    反了就成了环。

    所以正确做法是**上层把结论递下来**：
    `EpisodeRunner` 调 `agent` 层的盲读，把结果放进观测值，
    门禁只用**纯函数**比对。三个好处：

      ① 门禁保持纯净（无 IO、无模型、可穷尽测试）
      ② **缺观测时照常走"能力预检"** —— 没有 VLM key 就如实报"跳过"，
         而不是崩掉或静默放行（复用已有机制，不新增分支）
      ③ 盲读**失败**（超时、作废）与盲读**说不合格**能区分开 ——
         前者是缺观测，后者是真的不合格

    ⚠️ **不可信 ≠ 不合格。**
    这是门禁三态铁律之一。审核方与生成方同族时，判定**作废** ——
    那不是"产物有问题"，而是"这次判定不算数"。
    两者必须映射到**不同的结果**：前者 `ESCALATE`（升级人工），
    后者 `REVISE`（返工）。把作废当成不合格，会让返工指令**指错方向**。
    """
    if "blindVerdict" not in observed:
        return _missing("blind_read_verdict 需要 observed['blindVerdict']"
                        "（由 agent 层的跨模型族盲读注入）")

    raw = str(observed.get("blindVerdict") or "").strip().upper()
    allow = {str(x).strip().upper()
             for x in (expected.get("allow") or ("PASS",))}

    reviewer = str(observed.get("blindReviewerModel") or "")
    generator = str(observed.get("blindGeneratorModel") or "")
    invalid = str(observed.get("blindInvalidReason") or "")
    blocking = observed.get("blindBlocking")
    evidence: dict[str, Any] = {
        "reviewerModel": reviewer,
        "generatorModel": generator,
        "allow": sorted(allow),
    }
    if blocking is not None:
        evidence["blockingIssues"] = blocking

    # ---- ① 判定作废：**不是**产物不合格 ----
    #
    # 顺序很关键：先判"这次判定算不算数"，再判"产物好不好"。
    # 反过来的话，一份作废的判定会走进"不合格 -> 返工"，
    # 而返工指令是基于**不可信的观察**写的 —— 那比不返工更糟。
    if invalid:
        return AssertionOutcome(
            passed=False,
            # ⚠️ **`inconclusive=True`** —— 这让门禁走 `ESCALATE` 而不是
            # `RETRY`，并且**不给返工指令**（见 `AssertionOutcome` 的说明）。
            inconclusive=True,
            observed={"blindVerdict": raw or "(空)", "invalidated": True,
                      "reason": invalid, **evidence},
            detail=f"盲读判定**作废**（{invalid}）—— "
                   f"这不是「产物不合格」，而是「这次判定不算数」。"
                   f"需人工复核或换一个模型族重审",
        )

    # ---- ② 未知裁定：**不通过**，且同样不可信 ----
    #
    # 未知裁定归 `inconclusive` 而不是"不合格"，理由：模型返回了一个
    # 我们不认识的词，**说明这次交互本身出了问题**（提示词没约束好、
    # 模型换了版本、解析错了）。此时"产物到底好不好"是**未知**的，
    # 不是"已知不好"。
    if raw not in BLIND_VERDICTS:
        return AssertionOutcome(
            passed=False,
            inconclusive=True,
            observed={"blindVerdict": raw or "(空)", "unknown": True,
                      **evidence},
            detail=f"盲读返回了未知裁定 {raw!r}（合法取值："
                   f"{sorted(BLIND_VERDICTS)}）—— "
                   f"未知裁定**不能当成通过**：那会把'模型没见过这个词'"
                   f"读成'没问题'，而那是错误的方向。"
                   f"但它也**不是产物不合格** —— 是这次交互出了问题",
        )

    # ---- ③ 正常判定 ----
    passed = raw in allow
    return AssertionOutcome(
        passed=passed,
        observed={"blindVerdict": raw, **evidence},
        detail=("" if passed else
                f"盲读裁定 {raw}（允许 {'/'.join(sorted(allow))}）—— "
                f"审核方 {reviewer or '?'} 对生成方 {generator or '?'}"),
    )


# ---------------------------------------------------------------------------
# 注册触发
# ---------------------------------------------------------------------------

def ensure_registered() -> None:
    """确保内置算子已注册。

    本模块在 import 时就执行了所有 `@register` 装饰器，
    所以这个函数只是给"显式表达依赖"用的 ——
    调用它比依赖"import 顺序恰好对"更稳。
    """
    from little_crew.review.constraint import REGISTRY
    missing = [n for n in ("identity_similarity", "token_diff", "duration_within",
                           "resolution_matches", "count_at_least")
               if n not in REGISTRY.names()]
    if missing:
        raise AssertionError_(f"内置算子未注册：{missing}")


# ---------------------------------------------------------------------------
# 语义自洽类（文本 vs 结构）
# ---------------------------------------------------------------------------

#: 「画面上只有一个人」的中文标记。
#:
#: ⚠️ **刻意不收录 `一个人`** —— 它在 `一个人影`、`像一个人那样站着`
#: 这类句子里并不表示"独自"。误报会让人不再信这条断言，
#: 而**一条不被信的断言比没有更糟**（它制造"已检查"的假象）。
_SOLO_MARKERS: tuple[str, ...] = (
    "独自", "孤身", "只身", "单独一人", "孤零零", "形单影只",
    "alone", "by herself", "by himself",
)

#: 显式人数 → 数字。用于「描述写三人、角色表只有两人」这类矛盾。
_COUNT_MARKERS: tuple[tuple[str, int], ...] = (
    ("两个人", 2), ("二人", 2), ("两人", 2),
    ("三个人", 3), ("三人", 3),
    ("四个人", 4), ("四人", 4),
    ("五个人", 5), ("五人", 5),
)


def _cast_of(observed: Mapping[str, Any]) -> list[str] | None:
    """从观测里取角色表。取不到返回 None（交给 `_missing`）。"""
    raw = observed.get("characters", None)
    if raw is None:
        return None
    if isinstance(raw, str):
        return [x.strip() for x in raw.replace(",", "，").split("，") if x.strip()]
    try:
        return [str(x) for x in raw]
    except TypeError:
        return None


@register("cast_consistent_with_text", version="1")
def cast_consistent_with_text(expected: Mapping[str, Any],
                              observed: Mapping[str, Any]) -> AssertionOutcome:
    """**角色表与镜头描述是否自洽。**

    ## 为什么需要它（真实缺陷）

    2026-10-17 跑整集时发现：某镜 `characters = ["少女"]`，
    描述写的是"少女 **独自** 走在山坡上采花" —— 而另一镜
    `characters = ["旅人", "少女"]`。桩路径更直接：

        角色 ['少女', '旅人']   而描述写"少女 独自 走在山坡上采花"

    外观块按**角色表**注入两个角色的外貌描述，模型自然画出两个人。
    **画面错了，而所有确定性检查（分辨率/时长/哈希）全部通过。**

    这类矛盾**是机器能查出来的** —— 不需要看图，只要比对
    "文字说了几个人"与"结构声明了几个人"。

    ## 判据（两向都查）

    | 方向 | 例子 | 处置 |
    |---|---|---|
    | 描述说"独自"，角色表 > 1 | `独自` + `["少女","旅人"]` | **不通过** |
    | 描述给显式人数，与角色表不符 | `两个人` + `["少女"]` | **不通过** |

    ## 刻意**不**查的方向（可作为 `checkUnlisted` 开启）

    「描述里提到了某个角色，但他不在这镜的角色表里」——
    看似该查，其实**经常是合法的**：`她想起母亲` 里的母亲并不在画面里。
    把它默认打开会产生大量误报，而**误报会让断言失去信任**。

    ## 期望参数

    | 参数 | 默认 | 说明 |
    |---|---|---|
    | `soloMarkers` | 内置 | 覆盖"独自"类标记 |
    | `countMarkers` | 内置 | 覆盖人数标记 |
    | `checkUnlisted` | `false` | 开启"提到但未列"检查（需要 `castNames` 观测） |

    ## 观测参数

    | 字段 | 必需 | 说明 |
    |---|---|---|
    | `characters` | ✅ | 本镜角色表（列表或逗号分隔字符串） |
    | `description` | ✅ | 镜头描述文本 |
    | `castNames` | 仅 `checkUnlisted` | 全集角色名册 |
    """
    chars = _cast_of(observed)
    desc = observed.get("description", None)
    if chars is None:
        return _missing("cast_consistent_with_text 需要 observed['characters']")
    if not isinstance(desc, str):
        return _missing("cast_consistent_with_text 需要 observed['description']")

    solo = tuple(expected.get("soloMarkers") or _SOLO_MARKERS)
    counts = expected.get("countMarkers") or _COUNT_MARKERS
    n = len(chars)

    # ---- ① 描述说"独自"，结构却是多人 ----
    hits = [m for m in solo if m in desc]
    if hits and n > 1:
        return AssertionOutcome(
            passed=False,
            observed={"characters": n, "names": chars,
                      "soloMarkers": hits, "direction": "solo-vs-many"},
            detail=(f"描述写「{hits[0]}」（暗示画面只有 1 人），"
                    f"但角色表有 {n} 人：{chars}。"
                    f"外观会按角色表注入 {n} 份描述，模型会画出 {n} 个人 —— "
                    f"**画面与意图不符，而分辨率/时长/哈希全都查不出来**"),
        )

    # ---- ② 描述给了显式人数，与角色表不符 ----
    for token, want in counts:
        if token in desc and want != n:
            return AssertionOutcome(
                passed=False,
                observed={"characters": n, "names": chars,
                          "explicitCount": want, "token": token,
                          "direction": "explicit-count"},
                detail=(f"描述写「{token}」（{want} 人），"
                        f"但角色表是 {n} 人：{chars}"),
            )

    # ---- ③ 可选：提到但未列 ----
    if expected.get("checkUnlisted"):
        roster = [str(x) for x in (observed.get("castNames") or ())]
        if not roster:
            return _missing(
                "checkUnlisted 已开启，但缺少 observed['castNames'] —— "
                "没有名册就无法判断「提到了谁、该不该在画面里」")
        unlisted = [nm for nm in roster if nm in desc and nm not in chars]
        if unlisted:
            return AssertionOutcome(
                passed=False,
                observed={"characters": n, "names": chars,
                          "mentionedNotListed": unlisted},
                detail=(f"描述提到了未列入角色表的人：{unlisted}。"
                        f"若他们确实不该在画面里，描述该改写；"
                        f"若该在，角色表缺了他们（外观不会注入）"),
            )

    return AssertionOutcome(
        passed=True,
        observed={"characters": n, "names": chars,
                  "soloMarkers": hits, "direction": "consistent"},
        detail=f"{n} 人，描述无矛盾标记",
    )

