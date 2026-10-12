"""跨模型族盲读 —— C3 的 L3 层适配器。

它解决什么问题
--------------
C3（确定性验收门禁）有三层：

| 层 | 手段 | 能查什么 |
|---|---|---|
| **L1** | 断言算子（`review/ops.py`） | 分辨率、时长、字节数、角色数… |
| **L2** | 一致性描述子（`review/embed.py`） | 配色/光照/构图漂移 |
| **L3** | **本模块** —— 视觉模型盲读 | **语义质量**："这只手是不是画坏了" |

L1/L2 是确定性的，但它们**查不了语义**。剩下那部分只能由模型去看。

而"让模型去看"有个陷阱：**模型审自己的产出等于自我签收**。
所以 L3 的硬要求是**跨模型族** —— 生成方与审核方必须是不同族，
且审核方不得是产出者本人（这条由 `orchestrator.check_no_self_signoff`
强制，本模块**不重复实现**）。

层面归属
--------
本模块在 `agent` 层，**不是** `review` 层。理由：

    review 不能 import agent（架构守卫强制）——
    审核方在下、决策方在上，反了就成了环。

所以流程是**上层把结论递下去**：

    EpisodeRunner（runtime）
        -> BlindReader（agent） 调协作者、拿 ReviewFeedback
        -> 转成观测值
        -> GateRunner（review）用**纯函数**比对

门禁因此保持纯净：无 IO、无模型、可穷尽测试。

铁律：**不可信 ≠ 不合格**
-------------------------
| 情况 | 含义 | 映射到 |
|---|---|---|
| 审核方与生成方同族 | 判定**不算数** | `blindInvalidReason` -> 门禁 `ESCALATE` |
| 审核方超时/未产出 | 判定**缺失** | 不放观测 -> 门禁**跳过**该约束 |
| 模型说画面有问题 | 产物**真不合格** | `blindVerdict=REVISE` -> 门禁 `RETRY` |

**把三种混在一起是最危险的**：拿一份作废的判定去生成返工指令，
等于**基于不可信的观察指挥返工** —— 比不返工更糟。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

__all__ = ["BlindReadOutcome", "BlindReader"]


@dataclass
class BlindReadOutcome:
    """一次盲读的结果，已转成**门禁能吃的观测值**。"""

    #: 能不能产出观测值。`False` -> 该约束会被判"缺观测"而跳过。
    available: bool = False
    #: 门禁观测值（键名与 `ops.blind_read_verdict` 对齐）
    values: dict[str, Any] = field(default_factory=dict)
    #: 为什么不可用（进审计，便于回答"为什么这条约束没执行"）
    unavailable_reason: str = ""

    @property
    def invalidated(self) -> bool:
        """判定是否**作废**（与"产物不合格"是两回事）。"""
        return bool(self.values.get("blindInvalidReason"))


class BlindReader:
    """把编排器的审查能力包成"给门禁用的观测值生产器"。

    `review_fn` 可注入 —— 这样**自检不需要真的调模型**，
    而且"审核方超时""同族作废""真的不合格"三种路径都能被穷尽测试。
    默认实现走 `CrewOrchestrator.review_artifact`。
    """

    #: 门禁用的产物键（`orchestrator._REVIEWERS_BY_ARTIFACT` 里的那一套）
    ARTIFACT_KEY = "shot.image"

    def __init__(self, orchestrator: Any = None, *,
                 review_fn: Callable[..., Any] | None = None,
                 generator_id: str = "shot_renderer",
                 payload: Mapping[str, Any] | None = None) -> None:
        self.orchestrator = orchestrator
        self._review_fn = review_fn
        self.generator_id = generator_id
        self.payload = dict(payload or {})

    # ---- 主入口 --------------------------------------------------------

    def read(self, shot_id: str,
             artifacts: Sequence[Any] | None = None) -> BlindReadOutcome:
        """对某镜的产物做一次盲读。

        **任何异常都变成 `available=False`**，而不是往上抛 ——
        理由：盲读是**增量能力**，它挂了不该让整集跑不动。
        但**必须留下原因**，否则"盲读没生效"会变成一个查不出来的静默失效
        （这个项目已经在这类问题上踩过多次）。
        """
        fn = self._review_fn
        if fn is None:
            if self.orchestrator is None:
                return BlindReadOutcome(
                    unavailable_reason="未提供编排器，也没有注入 review_fn")
            fn = self.orchestrator.review_artifact

        payload = dict(self.payload)
        payload.setdefault("shotId", shot_id)
        if artifacts:
            payload.setdefault("artifacts", list(artifacts))

        try:
            reviews, invalid = fn(self.ARTIFACT_KEY, self.generator_id,
                                  payload=payload)
        except Exception as e:                            # noqa: BLE001
            return BlindReadOutcome(
                unavailable_reason=f"盲读调用失败：{type(e).__name__}: {e}")

        if invalid and not reviews:
            # 作废原因**照样要传下去** —— 它是"判定不算数"，
            # 不是"没有判定"。两者在门禁里的结局不同：
            # 前者 ESCALATE（升级人工），后者跳过。
            return BlindReadOutcome(
                available=True,
                values={"blindInvalidReason": invalid,
                        "blindVerdict": ""},
                unavailable_reason=invalid)

        if not reviews:
            return BlindReadOutcome(
                unavailable_reason="审核者未产出任何审查意见")

        return BlindReadOutcome(available=True,
                                values=self._to_values(reviews, invalid))

    # ---- 转换 ----------------------------------------------------------

    @staticmethod
    def _to_values(reviews: Sequence[Any],
                   invalid: str = "") -> dict[str, Any]:
        """把多份审查意见收敛成**一份**观测值。

        **取最严的那一份**（与 `merge_reviews` 的裁定规则一致）：
        审核意见里只要有一份说有问题，就不该被其他"没问题"盖过去。
        """
        rank = {"PASS": 0, "REVISE": 1, "REJECT": 2, "ESCALATE": 3}
        worst = None
        worst_rank = -1
        blocking = 0

        for r in reviews:
            v = r.effective_verdict() if hasattr(r, "effective_verdict") \
                else getattr(r, "verdict", None)
            name = getattr(v, "value", str(v)).upper()
            blocking += len(getattr(r, "blocking_issues", ()) or ())
            if rank.get(name, 0) > worst_rank:
                worst_rank, worst = rank.get(name, 0), r

        if worst is None:
            return {"blindVerdict": "", "blindInvalidReason": invalid}

        v = worst.effective_verdict() if hasattr(worst, "effective_verdict") \
            else getattr(worst, "verdict", None)
        name = getattr(v, "value", str(v)).upper()

        # ⚠️ 审核方自身判定为 ESCALATE 时，那是"**它认为**需要人看"，
        # 不是"判定作废"。作废只在 `invalid` 非空时成立。
        # 两者都导致升级人工，但**原因文案必须不同** ——
        # 否则事后查不出到底是"模型没把握"还是"这次审核程序上无效"。
        return {
            "blindVerdict": name,
            "blindReviewerModel": str(getattr(worst, "reviewer_model", "")),
            "blindGeneratorModel": str(getattr(worst, "generator_model", "")),
            "blindBlocking": blocking,
            "blindInvalidReason": invalid,
        }

    # ---- 供门禁用的注入 ------------------------------------------------

    def observations_for(self, shot_id: str,
                         artifacts: Sequence[Any] | None = None
                         ) -> dict[str, Any]:
        """给 `GateRunner` 用的 `context_by_shot[shot_id]` 片段。

        返回空字典表示"这次没得注" —— 门禁会据此**跳过**盲读约束
        （并留审计记录），而不是判它通过。
        """
        out = self.read(shot_id, artifacts)
        return dict(out.values) if out.available else {}
