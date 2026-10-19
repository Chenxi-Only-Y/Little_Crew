"""模型授权门禁。

对应《模型调研与适配方案》§8
---------------------------
背景：调研中逐一核验授权后发现三类真实风险，全靠人工记忆必然出错：

  1. **同名模型授权相反** —— FLUX.2 Klein 4B 是 Apache-2.0（可商用），
     9B 是 FLUX Non-Commercial（禁商用）。名字几乎一样，后果很重。
  2. **有条件授权带地区排除** —— MiniMax H3 排除 US/EU/UK/KR（中国大陆在范围内）；
     HunyuanVideo 1.5 排除欧盟/英国/韩国。
  3. **营收门槛** —— LTX Community 要求年营收 < 1000 万美元；
     Stable Audio 3 要求 < 100 万美元。
  4. **信源冲突** —— MiniMax Music 3 的授权有两个互相矛盾的说法。

因此本模块把授权变成**代码强制的硬约束**，而不是文档里的一句话。

三条强制行为
------------
  A. **未核实 = 拒绝**。``verified=False`` 的模型不得进入自动路由。
     授权信源冲突时，"我不知道"的正确处理是拒绝，不是随便挑一个。
  B. **降级必须重新过授权**。降级链的每一跳都要重跑检查 ——
     否则会出现"为了避免 OOM 降级到了一个禁商用模型"。
  C. **归属标注自动注入**。MiniMax H3 要求界面显著标注，
     靠人记得必然会漏；应由平台在成片尾帧/关于页自动加上。

合规姿态
--------
本模块**不是法律意见**。它做的是把已核实的授权事实编成可执行检查，
并在"未核实/信源冲突"时**保守拒绝**。任何真实商用场景都应以官方页面为准。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "LicenseGate",
    "LicenseInfo",
    "DeploymentContext",
    "LicenseVerdict",
    "LICENSE_CATALOG",
    "ReviewedLicense",
    "UNVERIFIED",
    "NONCOMMERCIAL",
    "APACHE_2_0",
    "CC_BY_NC_4_0",
]


# ==========================================================================
# 授权标识常量
# ==========================================================================

APACHE_2_0 = "Apache-2.0"
MIT = "MIT"
CC_BY_NC_4_0 = "CC-BY-NC-4.0"
FLUX_NONCOMMERCIAL = "FLUX-Non-Commercial"
CREATIVEML_RAIL = "CreativeML-Open-RAIL++-M"
MINIMAX_COMMUNITY = "MiniMax-Community"
TENCENT_COMMUNITY = "Tencent-Community"
LTX_COMMUNITY = "LTX-Community"
STABILITY_COMMUNITY = "Stability-Community"
UNVERIFIED = "UNVERIFIED"
NONCOMMERCIAL = "NONCOMMERCIAL"


class Verdict(str, Enum):
    """授权判定结果。"""

    ALLOW = "ALLOW"                    # 允许使用
    DENY = "DENY"                      # 禁止使用（硬性）
    NEEDS_REVIEW = "NEEDS_REVIEW"      # 需人工确认（未核实 / 信源冲突）

    @property
    def allowed(self) -> bool:
        return self is Verdict.ALLOW


# ==========================================================================
# 数据结构
# ==========================================================================

@dataclass(frozen=True)
class LicenseInfo:
    """一个模型的授权事实。所有字段都必须来自可核验来源。"""

    license_id: str
    commercial_use: bool
    #: 免费商用的年营收上限（美元）。None = 无门槛。
    revenue_threshold_usd: int | None = None
    #: 授权排除的部署地区（ISO 3166-1 alpha-2 国家码或区域码）。
    excluded_regions: tuple[str, ...] = ()
    #: 是否要求在产品界面显著标注模型名称。
    attribution_required: bool = False
    attribution_text: str = ""
    #: 是否包含可接受使用政策（AUP）的额外禁止用途条款。
    has_aup: bool = False
    #: 授权是否已核实。**False 时一律拒绝自动启用。**
    verified: bool = False
    #: 核实备注：来源、日期、或冲突说明。
    note: str = ""
    #: 是否存在信源冲突（如 MiniMax Music 3）。
    conflicting_sources: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "licenseId": self.license_id,
            "commercialUse": self.commercial_use,
            "revenueThresholdUsd": self.revenue_threshold_usd,
            "excludedRegions": list(self.excluded_regions),
            "attributionRequired": self.attribution_required,
            "attributionText": self.attribution_text,
            "hasAup": self.has_aup,
            "verified": self.verified,
            "note": self.note,
            "conflictingSources": self.conflicting_sources,
        }


@dataclass(frozen=True)
class DeploymentContext:
    """部署方的实际情形。决定授权是否适用。"""

    region: str = "CN"                      # 部署地区（ISO alpha-2）
    commercial: bool = False                # 是否商用
    annual_revenue_usd: int = 0             # 年营收（美元）
    #: 是否愿意接受 AUP 条款（如 SDXL 的禁止用途）
    accepts_aup: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "region": self.region,
            "commercial": self.commercial,
            "annualRevenueUsd": self.annual_revenue_usd,
            "acceptsAup": self.accepts_aup,
        }


@dataclass(frozen=True)
class LicenseVerdict:
    """判定结果，携带可读理由 —— 理由必须能直接展示给用户。"""

    verdict: Verdict
    model_id: str
    reason: str = ""
    #: 需要注入到产品界面的归属标注（若有）
    attribution_text: str = ""
    #: 触发判定的具体条款，便于溯源
    triggered_rule: str = ""

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "modelId": self.model_id,
            "reason": self.reason,
            "attributionText": self.attribution_text,
            "triggeredRule": self.triggered_rule,
        }


# ==========================================================================
# 授权事实目录
# ==========================================================================

@dataclass(frozen=True)
class ReviewedLicense:
    """模型 -> 授权事实的登记项。"""

    model_id: str
    info: LicenseInfo


#: 调研中逐一核验的授权事实。
#: 命名统一使用项目仓库标识，便于与 ModelSpec 对应。
LICENSE_CATALOG: Mapping[str, LicenseInfo] = {
    # ---------- Apache 2.0：可商用、无门槛 ----------
    "qwen-image-2512": LicenseInfo(
        APACHE_2_0, commercial_use=True, verified=True,
        note="已核实"),
    "qwen-image-2.1": LicenseInfo(
        APACHE_2_0, commercial_use=True, verified=True, note="已核实"),
    "ernie-image": LicenseInfo(
        APACHE_2_0, commercial_use=True, verified=True, note="已核实"),
    "wan2.2-5b": LicenseInfo(
        APACHE_2_0, commercial_use=True, verified=True,
        note="已核实。注意：Wan 开源线到 2.2 为止，2.5/2.6/2.7 均为 API 专属"),
    "wan2.2-14b": LicenseInfo(
        APACHE_2_0, commercial_use=True, verified=True, note="已核实"),
    "flux2-klein-4b": LicenseInfo(
        APACHE_2_0, commercial_use=True, verified=True,
        note="已核实。BFL 首个完全 Apache-2.0 的 FLUX 系模型"),
    "ace-step-1.5": LicenseInfo(
        APACHE_2_0, commercial_use=True, verified=True,
        note="两个信源对 Apache-2.0 / MIT 有分歧，但都无营收门槛"),

    # ---------- 禁商用 ----------
    "flux2-klein-9b": LicenseInfo(
        FLUX_NONCOMMERCIAL, commercial_use=False, verified=True,
        note="⚠️ 与 4B 授权相反。名字几乎一样，后果很重"),
    "flux1-dev": LicenseInfo(
        FLUX_NONCOMMERCIAL, commercial_use=False, verified=True,
        note="仅用于基准对照"),
    "yue2-3b": LicenseInfo(
        CC_BY_NC_4_0, commercial_use=False, verified=True,
        note="权重能下载 ≠ 产出能用"),

    # ---------- 有条件：地区排除 ----------
    "minimax-h3": LicenseInfo(
        MINIMAX_COMMUNITY, commercial_use=True,
        revenue_threshold_usd=20_000_000,
        excluded_regions=("US", "EU", "GB", "UK", "KR"),
        attribution_required=True,
        attribution_text="本作品由 MiniMax H3 生成",
        verified=True,
        note="排除地区 US/EU/UK/KR；中国大陆在授权范围内。"
             "须在产品界面显著标注「MiniMax H3」"),
    "hunyuanvideo-1.5": LicenseInfo(
        TENCENT_COMMUNITY, commercial_use=True,
        excluded_regions=("EU", "GB", "UK", "KR"),
        verified=True,
        note="排除欧盟/英国/韩国"),
    "hunyuan3d-2.1": LicenseInfo(
        TENCENT_COMMUNITY, commercial_use=True,
        excluded_regions=("EU", "GB", "UK", "KR"),
        verified=True,
        note="另有四条硬约束，见调研报告 §2"),

    # ---------- 有条件：营收门槛 ----------
    "ltx-video-2b": LicenseInfo(
        LTX_COMMUNITY, commercial_use=True,
        revenue_threshold_usd=10_000_000,
        verified=True,
        note="年营收 < 1000 万美元可免费商用。网上常被误写成 Apache 2.0"),
    "ltx-2.3": LicenseInfo(
        LTX_COMMUNITY, commercial_use=True,
        revenue_threshold_usd=10_000_000,
        verified=True,
        note="同 LTX-Video 2B"),
    "stable-audio-3-medium": LicenseInfo(
        STABILITY_COMMUNITY, commercial_use=True,
        revenue_threshold_usd=1_000_000,
        verified=True,
        note="仅生成器乐，不生成人声/歌词"),

    # ---------- 附 AUP 条款 ----------
    "sdxl-base-1.0": LicenseInfo(
        CREATIVEML_RAIL, commercial_use=True, has_aup=True, verified=True,
        note="CreativeML Open RAIL++-M，带可接受使用政策（AUP）禁止用途条款"),

    # ---------- 未核实 / 信源冲突：一律拒绝自动启用 ----------
    "minimax-music-3": LicenseInfo(
        UNVERIFIED, commercial_use=False, verified=False,
        conflicting_sources=True,
        note="⚠️ 信源冲突：一说 CC-BY-NC-4.0（禁商用），"
             "一说 MiniMax-Music3 Community License（可商用+界面标注+2000万美元门槛）。"
             "用前必须直接读仓库 LICENSE 文件"),
    "lens": LicenseInfo(
        UNVERIFIED, commercial_use=False, verified=False,
        note="Comfy-Org/Lens 是 repackage，原始厂商与授权待查"),

    # ---------- API 服务类：授权事实来自「服务条款」而非「权重许可证」 ----------
    #
    # ⚠️ 这一类与本地权重模型有**本质区别**：
    #   本地权重 = 许可证允许你怎么用**下载到的文件**
    #   API 服务 = 服务条款允许你怎么用**它提供的接口**
    #
    # 二者后果不同：权重违规是版权问题，API 违规是**合同问题**（可能被停服）。
    # 因此这里**不写"已核实"** —— 我们没有逐条读过这些服务条款，
    # 写 verified=True 就是编造事实，会让门禁在最该拦住的地方放行。
    #
    # 正确做法：部署方在正式上线前逐条核实并在 config/ 中登记；
    # 在那之前门禁一律 NEEDS_REVIEW。
    "deepseek-flash": LicenseInfo(
        "Commercial API ToS", commercial_use=True, verified=False,
        note="⚠️ 服务条款未逐条核实。价格页与条款页需分别核实，"
             "尤其是「生成内容的商用权」与「训练数据使用」两条"),
    "qwen3-max": LicenseInfo(
        "Alibaba Cloud Model Studio ToS", commercial_use=True, verified=False,
        note="⚠️ 服务条款未逐条核实"),
    "qwen3-vl-max": LicenseInfo(
        "Alibaba Cloud Model Studio ToS", commercial_use=True, verified=False,
        note="⚠️ 服务条款未逐条核实。审核模型的数据留存条款需单独确认"),
    #: 小米 MiMo —— **跨模型族盲读的审核方**。
    #:
    #: ⚠️ 与 deepseek **同理不写 verified** —— 我们没逐条读过小米的服务条款。
    #: 写 verified=True 就是编造事实，会让门禁在最该拦住的地方放行。
    #:
    #: 它存在的另一个理由：平台要求审核方与生成方**不同族**
    #: （见 docs/00 §5.9），而它扮演的正是那个审核方。
    "mimo": LicenseInfo(
        "Xiaomi MiMo API ToS", commercial_use=True, verified=False,
        note="⚠️ 服务条款未逐条核实（小米 MiMo 开放平台）。"
             "注意它是**纯 API 服务、无开源权重** —— "
             "商用前需确认「生成内容的商用权」与「数据留存」两条"),
    #: 智谱 GLM —— **第三个模型族**（2026-10-19 加入）。
    #:
    #: 它存在的理由与 mimo 类似：平台要求审核方与生成方不同族，
    #: 而在此之前只有两个族，导致「双维审查」的两个职位只能用同一个模型
    #: （见 docs/00 §5.10.16 ①）。加入它之后：
    #: 生成方 DeepSeek / 剧评人 MiMo / 文风编辑 GLM —— **三个职位三个族**。
    #:
    #: ⚠️ 同 mimo：**不写 verified** —— 我们没逐条读过智谱的服务条款。
    "glm": LicenseInfo(
        "Zhipu AI Open Platform ToS", commercial_use=True, verified=False,
        note="⚠️ 服务条款未逐条核实（智谱开放平台）。"
             "注意它是**推理模型**：reasoning token 也计费，"
             "约为显式输出的 1.6 倍 —— 成本估算要按实际 total_tokens 算"),
    "local-llm": LicenseInfo(
        APACHE_2_0, commercial_use=True, verified=False,
        note="⚠️ 本地 LLM 的实际授权取决于**部署方选用哪个权重**"
             "（Qwen / Llama / Gemma 各不相同）。此条目仅为占位，"
             "部署方须按实际权重另行登记"),
    "tts-http": LicenseInfo(
        "Alibaba Cloud Model Studio ToS", commercial_use=True, verified=False,
        note="⚠️ 服务条款未逐条核实。**声音克隆的肖像权/声音权**需单独确认"),
    "z-image-turbo": LicenseInfo(
        APACHE_2_0, commercial_use=True, verified=False,
        note="⚠️ 待核实：Z-Image 权重许可证与所用 4bit 量化版的再分发条款"),
}


# ==========================================================================
# 门禁
# ==========================================================================

class LicenseGate:
    """授权门禁。三条强制行为的实现处。"""

    def __init__(
        self,
        catalog: Mapping[str, LicenseInfo] | None = None,
        context: DeploymentContext | None = None,
    ) -> None:
        self.catalog: dict[str, LicenseInfo] = dict(catalog or LICENSE_CATALOG)
        self.context = context or DeploymentContext()

    # ---- 目录管理 ------------------------------------------------------

    def register(self, model_id: str, info: LicenseInfo) -> None:
        """登记或覆盖一个模型的授权事实。新增模型只需调此方法。"""
        self.catalog[model_id] = info

    def info(self, model_id: str) -> LicenseInfo | None:
        return self.catalog.get(model_id)

    # ---- 判定 ----------------------------------------------------------

    def check(self, model_id: str, context: DeploymentContext | None = None) -> LicenseVerdict:
        """对单个模型判定。返回携带可读理由的结果。"""
        ctx = context or self.context
        info = self.catalog.get(model_id)

        # 0) 目录中无记录 -> 视为未核实
        if info is None:
            return LicenseVerdict(
                Verdict.NEEDS_REVIEW, model_id,
                reason=f"模型 {model_id!r} 无授权记录，需先核实并登记",
                triggered_rule="NO_RECORD",
            )

        # A) 未核实 / 信源冲突 -> 一律拒绝自动启用
        if info.conflicting_sources:
            return LicenseVerdict(
                Verdict.NEEDS_REVIEW, model_id,
                reason=f"授权信源冲突，必须人工核实：{info.note}",
                triggered_rule="CONFLICTING_SOURCES",
            )
        if not info.verified:
            return LicenseVerdict(
                Verdict.NEEDS_REVIEW, model_id,
                reason=f"授权未核实，拒绝自动启用：{info.note}",
                triggered_rule="UNVERIFIED",
            )

        # 1) 非商用 + 商用场景 -> 拒绝
        if ctx.commercial and not info.commercial_use:
            return LicenseVerdict(
                Verdict.DENY, model_id,
                reason=f"{info.license_id} 禁止商用，当前为商用部署",
                triggered_rule="NONCOMMERCIAL_IN_COMMERCIAL_CTX",
            )

        # 2) 地区排除
        if ctx.region and ctx.region.upper() in {r.upper() for r in info.excluded_regions}:
            return LicenseVerdict(
                Verdict.DENY, model_id,
                reason=f"授权排除部署地区 {ctx.region}（排除清单 "
                       f"{', '.join(info.excluded_regions)}）",
                triggered_rule="REGION_EXCLUDED",
            )

        # 3) 营收门槛（仅商用场景下有意义）
        if (ctx.commercial and info.revenue_threshold_usd is not None
                and ctx.annual_revenue_usd > info.revenue_threshold_usd):
            return LicenseVerdict(
                Verdict.DENY, model_id,
                reason=f"年营收 {ctx.annual_revenue_usd:,} 美元超过免费商用门槛 "
                       f"{info.revenue_threshold_usd:,} 美元，需另行申请商业授权",
                triggered_rule="REVENUE_THRESHOLD_EXCEEDED",
            )

        # 4) AUP 条款
        if info.has_aup and not ctx.accepts_aup:
            return LicenseVerdict(
                Verdict.DENY, model_id,
                reason=f"{info.license_id} 含可接受使用政策（AUP），部署方未接受",
                triggered_rule="AUP_NOT_ACCEPTED",
            )

        # C) 通过。若要求归属标注，把文本一并返回，由上层注入产品界面
        return LicenseVerdict(
            Verdict.ALLOW, model_id,
            reason=f"{info.license_id} 在当前部署情形下允许使用",
            attribution_text=info.attribution_text if info.attribution_required else "",
            triggered_rule="OK",
        )

    # ---- 批量与路由集成 ------------------------------------------------

    def filter_allowed(
        self, model_ids: Iterable[str], context: DeploymentContext | None = None,
    ) -> tuple[list[str], list[LicenseVerdict]]:
        """从候选集中筛出允许的模型。返回 (允许列表, 全部判定)。

        这是路由算法的硬约束入口：**被拒绝的模型根本不进入打分环节**。
        """
        allowed: list[str] = []
        verdicts: list[LicenseVerdict] = []
        for mid in model_ids:
            v = self.check(mid, context)
            verdicts.append(v)
            if v.allowed:
                allowed.append(mid)
        return allowed, verdicts

    def check_degradation_chain(
        self, chain: Sequence[str], context: DeploymentContext | None = None,
    ) -> tuple[list[str], list[LicenseVerdict]]:
        """B) 降级链的每一跳都要重新过授权。

        典型事故：主模型因 OOM 降级 -> 落到一个禁商用模型 -> 静默产出禁商用内容。
        本方法把降级链整体过一遍授权，把不合规的跳直接剔除。
        """
        return self.filter_allowed(chain, context)

    def collect_attributions(self, model_ids: Iterable[str]) -> list[str]:
        """C) 汇总需要展示的归属标注，供成片尾帧/关于页自动注入。"""
        out: list[str] = []
        for mid in model_ids:
            info = self.catalog.get(mid)
            if info and info.attribution_required and info.attribution_text:
                if info.attribution_text not in out:
                    out.append(info.attribution_text)
        return out

    # ---- 报告 ----------------------------------------------------------

    def audit(self, model_ids: Iterable[str] | None = None) -> dict[str, Any]:
        """生成授权审计报告。可直接放进答辩材料或商用尽调清单。"""
        ids = list(model_ids) if model_ids is not None else sorted(self.catalog)
        rows: list[dict[str, Any]] = []
        counts = {v.value: 0 for v in Verdict}
        for mid in ids:
            v = self.check(mid)
            counts[v.verdict.value] += 1
            rows.append({**v.as_dict(), "license": (self.info(mid).as_dict()
                                                    if self.info(mid) else None)})
        return {
            "context": self.context.as_dict(),
            "counts": counts,
            "models": rows,
            "attributionsRequired": self.collect_attributions(ids),
        }
