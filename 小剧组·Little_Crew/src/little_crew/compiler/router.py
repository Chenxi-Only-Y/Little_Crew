"""模型路由 —— 在「模型声明」与「环境事实」的交汇处求解。

为什么单独成模块
----------------
``models`` 只声明"想用哪些模型"，``capability`` 只说"环境有哪些 class_type"，
``governance`` 只判"这个模型能不能用"。**三者都不该知道彼此** ——
把它们的判断混进任何一个，都会让"换模型"或"换环境"需要改多处。

本模块是唯一同时看见三者的地方，因此也是**唯一做决策的地方**。

求解顺序（硬约束在前，软偏好在后）
----------------------------------
    1. task 匹配
    2. 通道形态（local / api）—— 用户显式选择优先
    3. 授权门禁    —— **不可用即出局**（见 governance）
    4. 能力必满足  —— `TaskProfile.must` 逐条比对
    5. 成本上限（仅 API 通道）
    6. 软偏好加权排序

任何一步淘汰都会进入 ``RouteDecision.rejected``，携带可读理由 ——
**"为什么没选它"和"为什么选它"同样重要**，否则用户无法理解平台的决策。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from little_crew.governance.license import (
    DeploymentContext,
    LicenseGate,
    LicenseVerdict,
)
from little_crew.models.spec import (
    Backend,
    BackendKind,
    ModelRegistry,
    ModelSpec,
    TaskKind,
    TaskProfile,
)

__all__ = ["RouteDecision", "Rejection", "ModelRouter"]


@dataclass(frozen=True)
class Rejection:
    """一条淘汰记录。**必须携带可读理由** —— 用户需要知道为什么。"""

    model_id: str
    stage: str          # task | channel | license | capability | cost
    reason: str
    detail: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {"modelId": self.model_id, "stage": self.stage,
                "reason": self.reason, "detail": self.detail}


@dataclass
class RouteDecision:
    """路由结果。既给出选择，也给出**未被选择的理由**。"""

    chosen: ModelSpec | None = None
    backend: Backend | None = None
    #: 按偏好排序的备选（用于失败重试与"多引擎竞速"）
    alternatives: list[ModelSpec] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)
    #: 需要展示的归属标注（授权要求，如 "本作品由 MiniMax H3 生成"）
    attributions: list[str] = field(default_factory=list)
    #: 判定明细，供审计
    verdicts: list[LicenseVerdict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.chosen is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "chosen": self.chosen.model_id if self.chosen else None,
            "backendKind": self.backend.kind.value if self.backend else None,
            "endpoint": self.backend.endpoint if self.backend else None,
            "alternatives": [m.model_id for m in self.alternatives],
            "rejected": [r.as_dict() for r in self.rejected],
            "attributions": list(self.attributions),
        }


class ModelRouter:
    """把 ``TaskProfile`` 解析成 ``(ModelSpec, Backend)``。"""

    def __init__(
        self,
        registry: ModelRegistry,
        gate: LicenseGate,
        *,
        default_endpoints: Mapping[BackendKind, str] | None = None,
    ) -> None:
        self.registry = registry
        self.gate = gate
        #: 通道类型 -> 默认端点。模型声明里 endpoint 留空时用它。
        #: 来源：``config/.env``（COMFYUI_ENDPOINT / LOCAL_GEN_ENDPOINT）
        self.default_endpoints = dict(default_endpoints or {})

    # ---- 主入口 --------------------------------------------------------

    def route(
        self,
        profile: TaskProfile,
        *,
        ctx: DeploymentContext | None = None,
        only_models: Sequence[str] | None = None,
    ) -> RouteDecision:
        """求解。返回带淘汰理由的决策。"""
        decision = RouteDecision()
        allow = set(only_models) if only_models else None

        # 1) task 匹配
        candidates: list[ModelSpec] = []
        for spec in self.registry.all():
            if allow is not None and spec.model_id not in allow:
                continue
            if spec.task is not profile.task:
                continue
            candidates.append(spec)

        if not candidates:
            decision.rejected.append(Rejection(
                "*", "task", f"没有任何模型声明支持任务 {profile.task.value!r}"))
            return decision

        # 2) 通道形态
        for spec in list(candidates):
            if profile.channel == "local" and not spec.supports_local:
                candidates.remove(spec)
                decision.rejected.append(Rejection(
                    spec.model_id, "channel", "该模型无本地执行通道（用户要求本地）"))
            elif profile.channel == "api" and not spec.supports_api:
                candidates.remove(spec)
                decision.rejected.append(Rejection(
                    spec.model_id, "channel", "该模型无远端 API 通道（用户要求 API）"))

        # 3) 授权门禁（硬约束）
        for spec in list(candidates):
            v = self.gate.check(spec.license_id, ctx) if spec.license_id else None
            if v is not None:
                decision.verdicts.append(v)
                if not v.allowed:
                    candidates.remove(spec)
                    decision.rejected.append(Rejection(
                        spec.model_id, "license", v.reason,
                        {"verdict": v.verdict.value, "rule": v.triggered_rule}))
                    continue
            if v is not None and v.attribution_text:
                if v.attribution_text not in decision.attributions:
                    decision.attributions.append(v.attribution_text)

        # 4) 能力必满足
        for spec in list(candidates):
            unmet = self._unmet_capabilities(spec, profile)
            if unmet:
                candidates.remove(spec)
                decision.rejected.append(Rejection(
                    spec.model_id, "capability",
                    "不满足硬性能力要求: " + "; ".join(unmet)))

        # 5) 成本上限
        if profile.budget_cny > 0:
            for spec in list(candidates):
                est = self._estimate_cny(spec, profile)
                if est > profile.budget_cny:
                    candidates.remove(spec)
                    decision.rejected.append(Rejection(
                        spec.model_id, "cost",
                        f"预估成本 ¥{est:.3f} 超出预算 ¥{profile.budget_cny:.3f}",
                        {"estimatedCny": est}))

        if not candidates:
            return decision

        # 6) 软偏好排序
        ranked = sorted(candidates, key=lambda s: self._score(s, profile), reverse=True)
        decision.chosen = ranked[0]
        decision.alternatives = ranked[1:]
        decision.backend = self._pick_backend(decision.chosen, profile)
        return decision

    # ---- 内部 ----------------------------------------------------------

    def _unmet_capabilities(self, spec: ModelSpec, profile: TaskProfile) -> list[str]:
        """逐条比对 ``must``，返回未满足项的可读描述。"""
        cap = spec.capability
        unmet: list[str] = []
        for field_name, want in profile.must.items():
            have = getattr(cap, field_name, None)
            if have is None:
                unmet.append(f"能力画像无字段 {field_name!r}")
                continue
            if isinstance(want, bool):
                if bool(have) is not want:
                    unmet.append(f"{field_name} 需 {want}，实为 {have}")
            elif isinstance(want, (int, float)):
                # 数值型 `must` 语义：**要求实际值 >= 要求值**（越大越好）
                if float(have) < float(want):
                    unmet.append(f"{field_name} 需 >= {want}，实为 {have}")
            elif isinstance(want, (list, tuple)) and len(want) == 2:
                # 二元组表示 (宽, 高) 的分辨率要求
                hw = tuple(have) if isinstance(have, (list, tuple)) else None
                if hw is None or hw[0] < want[0] or hw[1] < want[1]:
                    unmet.append(f"{field_name} 需 >= {list(want)}，实为 {have}")
            else:
                if have != want:
                    unmet.append(f"{field_name} 需 {want!r}，实为 {have!r}")
        return unmet

    def _estimate_cny(self, spec: ModelSpec, profile: TaskProfile) -> float:
        c = spec.cost
        if c.kind == "local":
            return 0.0
        return c.cny_per_call

    def _score(self, spec: ModelSpec, profile: TaskProfile) -> float:
        """软偏好加权得分。越大越好。

        无 ``prefer`` 时按成熟度给基线分 —— 保证排序稳定且可解释。
        """
        base = {
            "production": 3.0, "beta": 2.0,
            "experimental": 1.0, "deprecated": 0.0,
        }[spec.maturity.value]

        total = base
        for field_name, (weight, direction) in profile.prefer.items():
            # 性能类字段（越小越好）从 performance 里取
            if field_name == "estimatedMs":
                vals = list(spec.performance.measured_ms.values())
                v = min(vals) if vals else 0
            elif field_name == "costCny":
                v = self._estimate_cny(spec, profile)
            else:
                v = getattr(spec.capability, field_name, None)
            if v is None:
                continue
            if direction == "max":
                total += weight * float(v)
            else:
                # "min"：取倒数形式，避免量纲差异主导
                total += weight * (1.0 / (1.0 + float(v)))
        return total

    def _pick_backend(self, spec: ModelSpec, profile: TaskProfile) -> Backend | None:
        """在模型的多条通道里选一条。用户指定形态优先，否则本地优先。"""
        if profile.channel == "local":
            order = (BackendKind.COMFYUI, BackendKind.PROCESS, BackendKind.HTTP)
        elif profile.channel == "api":
            order = (BackendKind.HTTP,)
        else:
            # 不限时本地优先：成本为 0，符合"先本地后云端"的默认取向
            order = (BackendKind.COMFYUI, BackendKind.PROCESS, BackendKind.HTTP)

        for kind in order:
            for b in spec.backends:
                if b.kind is not kind:
                    continue
                if profile.channel is None and kind is BackendKind.HTTP \
                        and not _is_local_endpoint(b, self.default_endpoints):
                    # 不限形态时，远端 API 排在本地之后
                    continue
                return b
        return spec.backends[0] if spec.backends else None


def _is_local_endpoint(b: Backend, defaults: Mapping[BackendKind, str]) -> bool:
    ep = b.endpoint or defaults.get(b.kind, "")
    return any(h in ep.lower() for h in ("127.0.0.1", "localhost", "0.0.0.0",
                                         "::1", "host.docker.internal"))
