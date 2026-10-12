"""把门禁接进 `EpisodeRunner` —— 渲染后判定，判定结果进交付证据。

设计决策
--------
**为什么门禁在 `runtime` 里被调用，而不违反"runtime 不做判定"**：

| | 谁做 |
|---|---|
| **判定规则** | `review/ops.py`（算子）与 `review/gate.py`（裁定） |
| **调用与编排** | `runtime/episode.py` —— 它只是**在正确的时机把数据递过去** |

`runtime` 没有判定逻辑，它甚至连"合格"长什么样都不知道 ——
那完全由 IR 的约束与 `review` 的算子决定。
这与"runtime 不做判定"并不矛盾：**不判定 ≠ 不调用判定**。

（架构守卫据此把 `(runtime, review)` 加入同层白名单，见
`tests/test_architecture.py` 的白名单注释。）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from little_crew.review.constraint import REGISTRY
from little_crew.review.gate import Gate, GateResult, GateVerdict, Observation
from little_crew.review.observe import (
    ObservationPlan,
    measure_artifact,
    plan_observations,
)

# ⚠️ **本模块刻意不 import `runtime` 的任何东西** —— 连产物类型都不 import。
#
# 层次布局是 `review`(下标小) -> `runtime`(下标大) 的**调用方向**，
# 但那是 `runtime` 调 `review`。`review` 自己**对 runtime 零 import 依赖**，
# 这才让"生成方与审核方隔离"在**代码层面**而不只是文档层面成立：
#
#   · 审核方拿不到通道、提交不了作业、读不了产物目录 —— 它根本没那些类型
#   · 产物以**鸭子类型**被递进来（只要 `.width` / `.bytes` / `.extra` 等属性在）
#
# 代价是这里少了一点静态类型检查，收益是**隔离不靠自觉**。
# 见 `observe.extract_from_artifact`：它全程用 `getattr`，正是为此。

__all__ = ["ShotGateReport", "GateRunner"]


@dataclass(frozen=True)
class ShotGateReport:
    """一集的整体门禁报告。"""

    results: tuple[GateResult, ...] = ()
    #: 因本机能力不足而**未执行**的约束（显式记录，不静默）
    skipped: tuple[str, ...] = ()
    #: 本机实际具备的观测能力
    available: tuple[str, ...] = ()

    @property
    def kept(self) -> int:
        return sum(1 for r in self.results if r.verdict is GateVerdict.KEEP)

    @property
    def retry(self) -> int:
        return sum(1 for r in self.results if r.verdict is GateVerdict.RETRY)

    @property
    def escalate(self) -> int:
        return sum(1 for r in self.results
                   if r.verdict is GateVerdict.ESCALATE)

    @property
    def blocking(self) -> tuple[GateResult, ...]:
        return tuple(r for r in self.results if r.verdict is not GateVerdict.KEEP)

    @property
    def all_kept(self) -> bool:
        """是否**没有**任何门禁失败。

        ⚠️ **没有可执行的约束时返回 `True`** —— 这与"约束全过"是两件不同的事，
        但**都不构成失败**。

        早期版本写成 `bool(self.results) and not self.blocking`，
        于是"IR 里没有约束"被判成"门禁未过" —— 实测表现为
        **一集跑通了却被报 `ok=False`，理由还是"0 镜需返工 / 0 镜需人工"**
        （理由里的数字自相矛盾，是这类缺陷的典型特征）。

        真正的判据是**有没有失败**，不是**有没有检查**：
          · 0 条可执行约束 -> 没有失败 -> True
            （"本次没检查任何东西"由 `results` 为空如实体现）
          · N 条全过 -> True
          · 有 RETRY 或 ESCALATE -> False
        """
        return all(r.verdict is GateVerdict.KEEP for r in self.results)

    def all_repairs(self) -> tuple[str, ...]:
        out: list[str] = []
        for r in self.results:
            out.extend(r.repairs)
        return tuple(out)

    def all_escalations(self) -> tuple[str, ...]:
        out: list[str] = []
        for r in self.results:
            out.extend(r.escalations)
        return tuple(out)

    def as_dict(self) -> dict[str, Any]:
        return {
            "results": [r.as_dict() for r in self.results],
            "skippedConstraints": list(self.skipped),
            "availableObservations": list(self.available),
            "counts": {
                "shots": len(self.results),
                "kept": self.kept,
                "retry": self.retry,
                "escalate": self.escalate,
                "skipped": len(self.skipped),
            },
        }

    # ---- 返工闭环要用 ----------------------------------------------------

    def actions_for(self, shot_id: str) -> tuple[Any, ...]:
        """取某个镜头的返工动作（**给返工循环执行**）。

        返回空元组有三种情况，且必须能区分：
          · 该镜通过了（没有动作要执行）
          · 该镜没进入裁定（约束全被跳过）
          · 该镜 RETRY 但动作全不可应用（**循环不该重跑**）
        后两者的区分靠 `results` 里有没有这个 shot —— 见 `EpisodeRunner`。
        """
        for r in self.results:
            if r.artifact_id == shot_id:
                return tuple(r.repair_actions)
        return ()

    def verdict_of(self, shot_id: str) -> str:
        for r in self.results:
            if r.artifact_id == shot_id:
                return r.verdict.value
        return ""

    def repairable_shots(self) -> tuple[str, ...]:
        """哪些镜头**值得重跑**（有可应用的动作）。

        **这是防烧 GPU 的第一道闸**：动作全不可应用的镜头
        重跑也只会拿到同一张图（甚至命中缓存）—— 空转的循环比不循环更糟。
        """
        return tuple(r.artifact_id for r in self.results
                     if r.verdict is GateVerdict.RETRY and r.repairable)


def _units_of(refs: Any) -> list[Any]:
    """把产物的各种形态统一成「可测单元」列表。

    | 输入形态 | 展开为 |
    |---|---|
    | `{名字: bytes}` | `[(名字, bytes), ...]` |
    | `[名字, ...]` / `[ArtifactRef, ...]` | 原样 |

    **为什么值得单独一个函数**：产物形态随渲染路径变化 ——
    `CachingRenderer` 给的是 `{名字: bytes}`，
    而通道层给的是 `ArtifactRef`。门禁的判定逻辑不该关心这些差异，
    它只想要「能测的东西」。

    实测依据：`CacheOutcome.artifacts` 就是 `{文件名: bytes}`，
    而早期版本按「名字字符串列表」处理，于是 `getattr(bytes, 'width')`
    拿到默认值 —— **门禁以为观测缺失，把所有分辨率约束都跳过了**，
    而且不报任何错。
    """
    if isinstance(refs, Mapping):
        out: list[Any] = []
        for k, v in refs.items():
            out.append((k, v) if isinstance(v, (bytes, bytearray)) else v)
        return out
    if isinstance(refs, (list, tuple)):
        return list(refs)
    return [refs] if refs else []


class GateRunner:
    """按镜头跑门禁。

    **无状态**：同一实例可反复使用，结果只取决于入参。
    """

    def __init__(self, gate: Gate | None = None) -> None:
        self.gate = gate or Gate()

    def run(
        self,
        *,
        constraints_by_shot: Mapping[str, Sequence[Mapping[str, Any]]],
        artifacts_by_shot: Mapping[str, Sequence[Any]],
        resolve: Any = None,
        context_by_shot: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> ShotGateReport:
        """对每个镜头求值。

        `constraints_by_shot` 来自各镜的 IR（`ir["constraints"]`）；
        `artifacts_by_shot` 是渲染产出的产物引用。

        `resolve` 是**产物名 -> 真实路径**的回调（`EpisodeRunner.resolve_artifact`）。

        `context_by_shot` 是**来自镜头计划**的观测（如 `characterCount`）。
        它们不从产物文件量 —— 但不传的话，依赖它们的约束
        （如 `count_at_most(field="characterCount")`）会被预检判为
        "缺观测"而跳过，**表现为约束默默失效**。

        ⚠️ **不给 `resolve` 会显著削弱门禁**：产物引用上的
        `width`/`height`/`bytes` 字段实测为空（见 `observe.py` 的 docstring），
        所以分辨率/大小类的约束会因为"观测缺失"被**全部跳过** ——
        表现是"门禁接了但什么都不检查"。
        给了解析器就能从文件**量**出这些值。
        """
        results: list[GateResult] = []
        skipped: list[str] = []
        available: set[str] = set()

        # ---- L2 一致性子：先算出**全体**的，再逐镜用 ----
        #
        # ⚠️ **为什么必须先批量算**：`similarity` 是**两两之间**的量，
        # 而 `measure_artifact` 是**单产物**函数 ——
        # 在逐镜循环里根本拿不到"另一镜"。
        #
        # ⚠️ **按需才算**：算一张 832x480 的一致性子要 ~300ms。
        # 只有当某条约束真的需要 `similarity` 时才启动 ——
        # 无条件算的话，"跑一集"会在门禁这步白花 6 秒。
        sigs = self._consistency_signatures(
            constraints_by_shot, artifacts_by_shot, resolve)
        wants_similarity = bool(sigs)

        for shot_id, refs in artifacts_by_shot.items():
            cons = list(constraints_by_shot.get(shot_id) or ())
            if not cons:
                continue

            # 先放**来自镜头计划**的观测（如 characterCount）——
            # 它们不从产物文件量，但同样是可执行的断言依据。
            values: dict[str, Any] = dict(
                (context_by_shot or {}).get(shot_id) or {})
            # ⚠️ `refs` 可能是三种形态：
            #   ① `{名字: bytes}`（`CachingRenderer` 实测就是这个）
            #   ② 名字字符串列表
            #   ③ 产物引用对象（`ArtifactRef`）
            # 统一展开成"可测的单元"，交给 `measure_artifact`。
            for unit in _units_of(refs):
                for k, v in measure_artifact(
                        unit, resolve,
                        want_signature=wants_similarity).items():
                    values.setdefault(k, v)

            # ---- 注入跨镜相似度 ----
            values.update(self._similarity_values(shot_id, sigs, cons))
            available |= set(values)

            plan: ObservationPlan = plan_observations(
                cons, values, known_ops=REGISTRY.names())
            skipped.extend(
                f"{shot_id}/{line}" for line in plan.audit_lines())

            if not plan.executable:
                # 全部约束都算不出 -> **不产出结果**，但审计里会说清为什么。
                # 不能产出一个"空通过"的结果，那会被读成"这一镜查过了"。
                continue

            results.append(self.gate.evaluate(
                plan.executable,
                Observation(artifact_id=shot_id, values=values,
                            source="artifact-ref")))

        return ShotGateReport(results=tuple(results),
                              skipped=tuple(skipped),
                              available=tuple(sorted(available)))

    # ---- L2：跨镜一致性 ------------------------------------------------

    @staticmethod
    def _consistency_signatures(
        constraints_by_shot: Mapping[str, Sequence[Mapping[str, Any]]],
        artifacts_by_shot: Mapping[str, Sequence[Any]],
        resolve: Any,
    ) -> dict[str, dict[str, Any]]:
        """**只有确实需要时**才算全体一致性子。

        需要 = 任一镜的约束里出现 `similarity` 类算子。
        不需要时返回空字典 —— 调用方据此跳过全部描述子计算。
        """
        needed = False
        for cons in constraints_by_shot.values():
            for c in cons:
                op = (c.get("assertion") or {}).get("op") \
                    if isinstance(c.get("assertion"), Mapping) else None
                if not op and isinstance(c.get("op"), str):
                    op = c["op"]
                if op in ("identity_similarity", "pairwise_similarity"):
                    needed = True
                    break
            if needed:
                break
        if not needed:
            return {}

        sigs: dict[str, dict[str, Any]] = {}
        for shot_id, refs in artifacts_by_shot.items():
            for unit in _units_of(refs):
                vals = measure_artifact(unit, resolve, want_signature=True)
                if "consistencySignature" in vals:
                    sigs[shot_id] = vals["consistencySignature"]
                    break                       # 每镜取第一个可描述产物
        return sigs

    @staticmethod
    def _similarity_values(
        shot_id: str,
        sigs: Mapping[str, Mapping[str, Any]],
        constraints: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """给某镜算它与**其他镜**的相似度。

        ⚠️ **取"最不像的那一镜"作为 `similarity`。**
        一致性是**短板效应** —— 一集里只要有一镜跟别人都不一样，
        那就已经崩了。取均值会让崩掉的那一镜被其他镜"拉平"而漏检。

        同时产出 `pairwise`（逐镜明细）与 `weakestDimension`，
        让返工指令能说清"跟谁不像、哪一维不像"。
        """
        mine = sigs.get(shot_id)
        if mine is None or len(sigs) < 2:
            # 只有一镜、或这一镜没有可描述产物 -> **不编造相似度**
            return {}

        ops = set()
        for c in constraints:
            a = c.get("assertion")
            if isinstance(a, Mapping) and isinstance(a.get("op"), str):
                ops.add(a["op"])
        if not ops & {"identity_similarity", "pairwise_similarity"}:
            return {}

        from little_crew.review.embed import METHOD, compare_signatures

        pairs: list[dict[str, Any]] = []
        for other, sig in sigs.items():
            if other == shot_id:
                continue
            r = compare_signatures(mine, sig)
            pairs.append({"otherShotId": other, **r})

        if not pairs:
            return {}
        worst = min(pairs, key=lambda p: p["similarity"])
        # ⚠️ 键名是 `method` —— 与 `ops.py` 里算子的读法一致。
        # 早期这里写 `similarityMethod`，于是算子读不到、证据里恒为
        # `"unspecified"` —— 而"用了哪个算法"恰恰是追溯相似度变化的前提。
        return {
            "similarity": worst["similarity"],
            "method": METHOD,
            "weakestDimension": worst["weakest"],
            "otherShotId": worst["otherShotId"],
            "pairwise": [
                {"otherShotId": p["otherShotId"],
                 "similarity": p["similarity"],
                 "weakest": p["weakest"]}
                for p in sorted(pairs, key=lambda p: p["similarity"])
            ],
        }
