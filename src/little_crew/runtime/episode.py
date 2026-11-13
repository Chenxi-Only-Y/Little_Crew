"""一集流水线 —— 从剧本素材到「成片清单 + 交付证据」。

链路位置
--------
```
剧本素材
  ↓ 编剧 → 剧本顾问 ‖ 文学策划 → 返工 → 定稿
script.final
  ↓ 分镜师
shot.list
  ↓ ir_bridge
每镜一张 IR
  ↓ CachingRenderer（缓存 + 检查点）
每镜一张图（**只重跑受影响的、崩溃可续**）
  ↓ 合成剪辑
episode.master（成片清单/母版）+ delivery.evidence（交付证据）
```

**为什么"成片清单"也算交付**：视频阶段依赖 H3 的 10 秒硬上限与 39 GB 权重，
是另一条独立的链。而"这一集由哪些镜、多长、按什么顺序、产物在哪个文件"
是**图像阶段就已经完整可用**的交付物 —— 它能被审片、能被验收、能被追溯。
把它做出来，M3 的"协作产出完整一集"就有了实际内容，而不是等视频阶段。

⚠️ 已实录名的产出名与规划文档有差异
----------------------------------
名册里 ``episode.master`` / ``delivery.evidence`` 由**代码**产出（成片阶段），
其 ``requires`` 含 ``shot.video`` —— 那是**含视频**的完整形态。
本模块产出的是**图像阶段的降级版**：同样的产出名，内容里 ``shots`` 指向图像。
这不是偷偷降级：清单里显式带 ``stage="image"`` 与 ``pendingStages=["video","audio"]``，
让审查方能看见"还缺什么"。
"""

from __future__ import annotations

import pathlib

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = [
    "EpisodeManifest",
    "DeliveryEvidence",
    "EpisodeResult",
    "EpisodeRunner",
]


# ==========================================================================
# 交付物
# ==========================================================================

class _ResumedCycle:
    """续跑用的"空闭环"：`final` 直接给出上次定稿，`rounds` 为空。

    ⚠️ 它不是"假装剧本通过了"：报告里 `scriptRounds=0` 且 `resumed=True`，
    读者能看出这一集**没有再跑剧本返工**。
    """

    def __init__(self, draft):
        self.final = draft
        self.rounds = ()
        self.round_count = 0
        self.resumed = True
        # 续跑**不产生裁定** —— 这里如实写"续跑"，而不是伪造 PASS：
        # 报告里能看出这一集没有重新走剧本门禁。
        self.accepted = True
        self.failure_kind = "resumed"
        self.reason = "剧本阶段从 checkpoint 续跑（未重新生成、未重走门禁）"


@dataclass
class EpisodeManifest:
    """成片清单（母版的"目录"）。

    刻意做成**可读的清单**而不是视频文件：
    视频合成需要 FFmpeg（外部依赖），而清单**不依赖任何外部工具**就能产出与校验。
    两者不冲突 —— 清单是"这一集是什么"，视频是它的一种呈现。
    """

    episode_id: str
    title: str = ""
    #: :attr:`stage` 表明本清单覆盖到哪一步
    stage: str = "image"           # image | video | audio
    #: 还差哪些阶段（让审查方看见缺口，而不是以为已完成）
    pending_stages: tuple[str, ...] = ("video", "audio")
    #: 按顺序排列的镜头条目
    shots: tuple[Mapping[str, Any], ...] = ()
    total_seconds: float = 0.0
    created_at: float = field(default_factory=time.time)

    @property
    def shot_count(self) -> int:
        return len(self.shots)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "episode.master",
            "episodeId": self.episode_id,
            "title": self.title,
            "stage": self.stage,
            "pendingStages": list(self.pending_stages),
            "shotCount": self.shot_count,
            "totalSeconds": round(self.total_seconds, 2),
            "shots": [dict(s) for s in self.shots],
            "createdAt": self.created_at,
        }


@dataclass
class DeliveryEvidence:
    """交付证据链 —— 证明"这一集是怎么来的"。

    这是审查与验收的依据：没有它，产出只是一个文件，无法追溯它由谁、
    用什么参数、在什么环境下生成。
    """

    episode_id: str
    #: 每个镜头的产物文件名（可直接去产物目录取）
    artifacts: tuple[str, ...] = ()
    #: 内容哈希链：镜头 id -> IR 哈希（改一镜即可看出变了哪个）
    ir_hashes: Mapping[str, str] = field(default_factory=dict)
    #: 产物**文件内容**哈希（文件名 -> sha256）。
    #:
    #: 与 `ir_hashes` 不是一回事：那个是**施工单**的哈希，回答"当时要做什么"；
    #: 这个是**产物字节**的哈希，回答"交付的东西有没有被改过"。
    #: 没有它，`tools/verify_evidence.py` 无法独立核验——只能信证据自述。
    artifact_hashes: Mapping[str, str] = field(default_factory=dict)
    #: 缓存命中统计（成本证据）
    cache_hits: int = 0
    rendered: int = 0
    seconds_saved: float = 0.0
    seconds_spent: float = 0.0
    #: 硬规则校验结果
    checks: Mapping[str, Any] = field(default_factory=dict)
    #: **验收门禁的完整裁定**（C3）。
    #:
    #: 它是 `DeliveryEvidence` 的一部分而不是"另一个文件"，
    #: 因为**交付证据链必须自证合格** —— 只说"我产出了 20 个文件"不够，
    #: 还要能说"这 20 个文件按哪些约束检查过、结论是什么"。
    #: 放在这里，审查方拿到证据链就能复核，不必再去别处找报告。
    gate: Mapping[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    @property
    def all_checks_passed(self) -> bool:
        return all(bool(v) for v in self.checks.values()) if self.checks else False

    @property
    def gate_passed(self) -> bool:
        """门禁是否全部通过。

        **未执行门禁时返回 `True`** —— 因为"没跑门禁"不是"门禁没通过"。
        这两件事必须区分：把它们混为一谈，会让"没接门禁"看起来像"门禁失败"，
        也会让"接了但全过"无法与"根本没接"区分。
        是否有门禁结果，看 `gate` 是否为空。
        """
        if not self.gate:
            return True
        counts = self.gate.get("counts") or {}
        return int(counts.get("retry", 0)) == 0 and int(counts.get("escalate", 0)) == 0

    def providers_summary(self) -> dict[str, Any]:
        """这次交付用了哪些模型（含**服务端实际模型名**）。

        契约：**永不抛异常** —— 证据里缺这一节比整份证据写不出来好。
        """
        try:
            from little_crew.governance.provider_fingerprint import summary

            s = summary()
            return {"total": s["total"], "providers": s["providers"],
                    "missingServedModel": s["missingServedModel"]}
        except Exception:                                   # noqa: BLE001
            return {"total": 0, "providers": [], "missingServedModel": 0,
                    "error": "unavailable"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "delivery.evidence",
            "episodeId": self.episode_id,
            "artifactCount": len(self.artifacts),
            "artifacts": list(self.artifacts),
            "irHashes": dict(self.ir_hashes),
            "artifactHashes": dict(self.artifact_hashes),
            "cache": {
                "hits": self.cache_hits, "rendered": self.rendered,
                "secondsSaved": round(self.seconds_saved, 2),
                "secondsSpent": round(self.seconds_spent, 2),
            },
            "checks": dict(self.checks),
            "allChecksPassed": self.all_checks_passed,
            "gate": dict(self.gate),
            "gatePassed": self.gate_passed,
            # provider 指纹：接云 API 之后，"这一版到底用了哪些模型"必须可查
            "providers": self.providers_summary(),
            "createdAt": self.created_at,
        }


@dataclass
class EpisodeResult:
    """一集跑完的结果。**部分成功是常态** —— 不因缺几镜就丢掉整集的记录。"""

    episode_id: str
    ok: bool = False
    script_rounds: int = 0
    script_verdict: str = ""
    #: **失败分类**（2026-10-17）—— 见 `CycleResult.failure_kind` 的说明。
    #:
    #: 存在的理由：`ok=False` 太粗。它把三种完全不同的情况混成一个"没成"：
    #:   · `review-failed` —— 评审**调用失败**（重派就好，与产物无关）
    #:   · `review-invalid` —— 判定作废（换族重审）
    #:   · `below-threshold` / `blocked` —— 产物**真的不够好**（返工）
    #:
    #: 实测里 C 组 5 次有 2 次是第一种，而报告只写"未通过、0 分" ——
    #: 读者会误以为"这个题材质量差"。
    failure_kind: str = ""

    #: **顾问意见**（2026-10-18）：`[{position, verdict, issues, total}]`。
    #:
    #: 与门禁审核者的区别是**它们不进裁定**。为什么：
    #:
    #: 往门禁里塞 3 个未标定的审核者，会让"通过与否"取决于
    #: 哪个提示词更爱挑刺 —— 那不是严谨，是**随机**。
    #: 所以先让它们以顾问身份**产出真实意见**，量出命中率，
    #: 再决定要不要把某一条升成门禁（与 `PASS_THRESHOLD` 同一套纪律）。
    #:
    #: **但它们是真跑的** —— 不是占位。意见会进报告，人看得见。
    #: **真片段拼的成片路径**（`video_mode` 时才有）。
    #:
    #: 与 `video_path` 的区别：
    #:   · `video_path` = 静帧 + Ken Burns（**便宜、随时能出**）
    #:   · `clip_video` = H3 真片段拼的（**331s/镜，贵**）
    #: 两者都在时，**`clip_video` 才是"视频"**，另一个是占位呈现。
    #: **美术设定**（2026-10-19）：`{"ok": bool, "bible": {...}}` 或空。
    art: dict[str, Any] = field(default_factory=dict)

    #: **白模预演**（2026-10-19）：`{"ok", "shots", "failed"}` 或空。
    #: 失败不阻断 —— 它只影响"构图是否受约束"，不影响能否出图。
    previs: dict[str, Any] = field(default_factory=dict)

    #: **排期**（2026-10-19）：`{"ok": bool, "schedule": {...}}` 或空。
    #: 不影响交付判定 —— 它是前置预估，不是门禁。
    schedule: dict[str, Any] = field(default_factory=dict)

    clip_video: str = ""

    #: **人工 Gate**（2026-11-08）：`{"decision", "reviewed", "approved", "assisted", ...}`。
    #:
    #: 为什么必须有：机器判"有没有/崩没崩"，判不了"能不能给人看"。
    #: `reviewed=False` 表示**没问人** —— 它与"人看过且认可"是两件事，
    #: 混在一起，答辩数据就会把"跳过"算成"通过"。
    human_gate: dict[str, Any] = field(default_factory=dict)

    #: **片段音轨检查结果**（V3，2026-10-18）：`{"ok", "clips", "passed", "failed", "reason"}`。
    #:
    #: 为什么单独一个字段：`clip_video` 只说"有没有拼出成片"，
    #: 而 M3 的要求是"**含音轨**"。两件事，必须分开看得见。
    clip_audio: dict[str, Any] = field(default_factory=dict)

    advisory: tuple[dict[str, Any], ...] = ()

    #: **导演方案**（2026-10-18）：`{"ok": bool, "plan": {...}}` 或空。
    #:
    #: 空 = 没启用导演（不是"导演失败了"）。失败时 `ok=False` 且有 `reason`。
    director: dict[str, Any] = field(default_factory=dict)

    #: **灯光方案**（2026-10-18）：`{"ok": bool, "plan": {...}}` 或空。
    gaffer: dict[str, Any] = field(default_factory=dict)
    shot_count: int = 0
    planned_shots: int = 0
    rendered: int = 0
    cache_hits: int = 0
    failed: int = 0
    total_seconds: float = 0.0
    wall_seconds: float = 0.0
    manifest: EpisodeManifest | None = None
    evidence: DeliveryEvidence | None = None
    reason: str = ""
    failures: tuple[Mapping[str, Any], ...] = ()
    #: 视频合成结果（未启用或失败时为 None）
    video: Any = None
    #: 成片视频路径（合成成功时）
    video_path: Path | None = None
    #: 角色定妆表（文字半）—— 交付物的一部分
    characters: Any = None

    @property
    def coverage(self) -> float:
        """已出图镜头 / 计划镜头。**这是"跑通一集"最该看的单一数字。**"""
        return (self.rendered + self.cache_hits) / self.planned_shots \
            if self.planned_shots else 0.0

    @property
    def playable(self) -> bool:
        """**能否连着看** —— "一集"最朴素的含义。

        在此之前"成片"只是一份 JSON 清单：能被审片、能被验收，
        但**不能被播放**。这个属性把那个区别显式化。
        """
        return bool(self.video_path and self.video_path.is_file())

    def as_dict(self) -> dict[str, Any]:
        return {
            "episodeId": self.episode_id,
            "ok": self.ok,
            "scriptRounds": self.script_rounds,
            "scriptVerdict": self.script_verdict,
            "failureKind": self.failure_kind,
            "clipVideo": self.clip_video,
            "art": dict(self.art),
            "schedule": dict(self.schedule),
            "previs": dict(self.previs),
            "clipAudio": dict(self.clip_audio),
            "advisory": [dict(a) for a in self.advisory],
            # 人工 Gate 必须进报告：否则"人没看过"和"人看过并认可"分不出来
            "humanGate": dict(self.human_gate or {}),
            "director": dict(self.director),
            "gaffer": dict(self.gaffer),
            "plannedShots": self.planned_shots,
            "rendered": self.rendered,
            "cacheHits": self.cache_hits,
            "failed": self.failed,
            "coverage": round(self.coverage, 4),
            "totalSeconds": round(self.total_seconds, 2),
            "wallSeconds": round(self.wall_seconds, 2),
            "playable": self.playable,
            # 场景越权否决是**交付级**结论，必须进报告（2026-11-10）：
            # 之前只看 report.ok，会把"被否决的集"统计成"全绿"
            # （EF17/EF18 就是 ok=True 但 CLI 否决）。
            "sceneVeto": bool(getattr(self, "scene_veto", False)),
            #: 视觉闸是否**一镜都没判成**（0 可判 != 没崩图）
            "visionNotJudged": bool(getattr(self, "vision_not_judged", False)),
            "video": (self.video.as_dict() if hasattr(self.video, "as_dict")
                      else None),
            "videoPath": str(self.video_path) if self.video_path else "",
            "characters": (self.characters.as_dict()
                           if hasattr(self.characters, "as_dict") else None),
            "reason": self.reason,
            "failures": [dict(f) for f in self.failures],
            # ---- 交付证据链 ----
            #
            # ⚠️ **必须导出，否则证据链的价值等于零。**
            # `evidence` 与 `manifest` 早就在 `EpisodeResult` 上，
            # 但 `as_dict()` 从不导出它们 —— 于是报告文件里
            # **看不到门禁裁定**，而"门禁跑没跑、结论是什么"
            # 恰恰是审查方最需要复核的东西。
            #
            # 一份写了不给人看的证据，与没写没有区别。
            "manifest": (self.manifest.as_dict()
                         if hasattr(self.manifest, "as_dict") else None),
            "evidence": (self.evidence.as_dict()
                         if hasattr(self.evidence, "as_dict") else None),
        }


# ==========================================================================
# 交付落盘 / 工作目录守卫 / 产物解析（入口脚本共用的三件小事）
# ==========================================================================
#
# ## 为什么单独抽出来（2026-10-23 审计 M-28 / H-4 / H-21）
# 这三件事原来各自被抄在两个入口里（`episode_demo` 与 `cli.commands`），
# 而**抄写必然漂移**：
#   · 报告页读 `<workdir>/delivery/run.report.json`，而产品 CLI 写的是
#     `<workdir>/run.report.json` —— 于是用 CLI 跑完一集，报告页是**空的**
#     （M-28）；顺带 CLI 也从不写 `episode.master.json` / `delivery.evidence.json`。
#   · 防串集守卫只有 demo 有，CLI 没有；而 CLI 的 `episode_id` 还写死成 "CLI"。
#   · `resolve_artifact` 只有 demo 设，CLI 从不设 —— 于是 `--no-video` 形同虚设
#     （H-21）：`assemble_video=True` 但解析器为 None，视频永远合不出来。
#
# 结论与项目一贯的做法一致：**不要复制，要抽出**。

def write_delivery(
    workdir: str | "Path",
    *,
    result: Any = None,
    manifest: Any = None,
    evidence: Any = None,
) -> "Path":
    """把一集的交付物**统一**落到 ``<workdir>/delivery/``。

    三份文件、一个目录 —— 这是报告页（``report_html.build_report``）约定的位置。
    任何入口（demo / CLI / API）都该走这里，否则报告页读不到东西。

    参数都可以省略：能拿到什么就写什么（例如只有 `result` 时才写 run.report.json）。
    """
    import json as _json
    from pathlib import Path as _Path

    out = _Path(workdir) / "delivery"
    out.mkdir(parents=True, exist_ok=True)

    def _dump(name: str, obj: Any) -> None:
        data = obj.as_dict() if hasattr(obj, "as_dict") else obj
        if data is not None:
            (out / name).write_text(
                _json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8")

    if manifest is not None:
        _dump("episode.master.json", manifest)
    if evidence is not None:
        _dump("delivery.evidence.json", evidence)
    # 指纹单独一份：包外的人想核"用了哪些模型"不用翻整个证据
    try:
        from little_crew.governance.provider_fingerprint import summary as _pf

        _dump("provider_fingerprint.json", _pf())
    except Exception:                                       # noqa: BLE001
        pass
    if result is not None:
        _dump("run.report.json", result)
    # ---- 视觉闸 / 重渲 结论进证据（2026-11-08）----
    # 缺席也要写：**"这集没审"和"这集审过且通过"必须能区分**。
    try:
        for _vn in ("vision_review.json", "vision_rework.json",
                    "storyboard_attempts.json"):
            _vf = out / _vn
            if not _vf.is_file():
                (out / _vn.replace(".json", ".absent.json")).write_text(
                    _json.dumps({"kind": _vn, "present": False,
                                 "why": "本次未产生（未开启或缺依赖）"},
                                ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:                                       # noqa: BLE001
        pass
    return out


def check_workdir_episode(workdir: str | "Path", episode_id: str
                          ) -> tuple[str, str]:
    """看看这个工作目录里**已经跑过哪一集**。

    返回 ``(已有集号, 冲突说明)``；没有冲突时两者都是空串。

    **为什么必须有**：最常见的出错方式不是"写错代码"，是**把新的一集写进老目录** ——
    两集的 shotId 会重名（`SH-E100-S01-001`），缓存会串（同 irHash 直接命中上一集的图），
    而报告看起来一切正常。那正是本项目最忌讳的形态：**看起来成功**。
    """
    import json as _json
    from pathlib import Path as _Path

    prev = _Path(workdir) / "delivery" / "run.report.json"
    if not prev.is_file():
        return "", ""
    try:
        old = _json.loads(prev.read_text(encoding="utf-8"))
    except Exception:                                     # noqa: BLE001
        return "", ""
    old_id = str((old or {}).get("episodeId") or "")
    if not old_id or not episode_id or old_id == episode_id:
        return old_id, ""
    return old_id, (f"这个目录里已经跑过 {old_id}，而你要跑的是 {episode_id}"
                    f"（镜头号会重名、缓存会串）")


def artifact_resolver(workdir: str | "Path"):
    """按**文件名**在工作目录里找产物，返回 ``name -> Path | None`` 的回调。

    给 `EpisodeRunner.resolve_artifact` 用：视频合成要从产物名找到真实文件。
    用 `rglob` + 一次索引缓存：产物散落在 `outputs/<镜头>/...` 各处，
    按名字全盘找一遍是最省事也最不容易漏的做法。
    """
    from pathlib import Path as _Path

    root = _Path(workdir)
    index: dict[str, "Path"] = {}

    def _resolve(name: str):
        if name in index:
            return index[name]
        if not name:
            return None
        direct = root / name
        if direct.is_file():
            index[name] = direct
            return direct
        for q in root.rglob(name):
            index[name] = q
            return q
        return None

    return _resolve


# ==========================================================================
# 合成剪辑（规则桩）
# ==========================================================================

class AssemblyEditorRules:
    """合成剪辑（规则桩）。把镜头产物汇成成片清单 + 交付证据链。

    **它不做视频合成** —— 那需要 FFmpeg，是另一个阶段的职责。
    它做的是**这一集是什么**：哪些镜、多长、按什么顺序、产物在哪。
    """

    def assemble(self, *, episode_id: str, title: str,
                 shots: Sequence[Mapping[str, Any]],
                 artifacts: Mapping[str, Sequence[str]],
                 ir_hashes: Mapping[str, str],
                 cache_stats: Mapping[str, Any],
                 gate: Mapping[str, Any] | None = None,
                 prompts: Mapping[str, str] | None = None,
                 ) -> tuple[EpisodeManifest, DeliveryEvidence]:
        entries: list[dict[str, Any]] = []
        flat: list[str] = []
        for s in shots:
            sid = str(s.get("shotId", ""))
            files = list(artifacts.get(sid, ()))
            flat.extend(files)
            entries.append({
                "shotId": sid,
                "sceneId": str(s.get("sceneId", "")),
                "shotSize": str(s.get("shotSize", "")),
                "cameraMovement": str(s.get("cameraMovement", "")),
                "durationSec": float(s.get("durationSec", 0.0)),
                "characters": list(s.get("characters", ())),
                "artifacts": files,
                #: **我们当初要求的是什么**（外观并入后的镜头描述）。
                #:
                #: 只存哈希是不够的：哈希能验"这张图没被换过"，
                #: 但验不了"图上画的是不是我要的"。两者缺一不可。
                "prompt": (prompts or {}).get(sid, ""),
                #: 有产物才算"已出图" —— 清单要能反映真实缺口
                "ready": bool(files),
            })

        total = sum(e["durationSec"] for e in entries)
        manifest = EpisodeManifest(
            episode_id=episode_id, title=title, stage="image",
            pending_stages=("video", "audio"),
            shots=tuple(entries), total_seconds=total)

        ready = sum(1 for e in entries if e["ready"])
        checks = {
            #: 每个镜头都有产物 —— 缺一镜就不算"跑通一集"
            "allShotsHaveArtifacts": ready == len(entries) and bool(entries),
            #: 每镜都在硬上限内
            "allShotsWithinDurationLimit": all(
                e["durationSec"] <= 10.0 for e in entries),
            #: 清单非空
            "manifestNonEmpty": bool(entries),
            #: 产物文件名唯一（重名会互相覆盖）
            "artifactsUnique": len(set(flat)) == len(flat),
            #: 有内容哈希链（否则无法追溯"改了什么"）
            "hasContentHashChain": all(
                ir_hashes.get(str(s.get("shotId", ""))) for s in shots),
        }

        evidence = DeliveryEvidence(
            episode_id=episode_id, artifacts=tuple(flat),
            ir_hashes=dict(ir_hashes),
            cache_hits=int(cache_stats.get("hits", 0)),
            rendered=int(cache_stats.get("rendered", 0)),
            seconds_saved=float(cache_stats.get("secondsSaved", 0.0)),
            seconds_spent=float(cache_stats.get("secondsSpent", 0.0)),
            checks=checks,
            gate=dict(gate or {}))
        return manifest, evidence


# ==========================================================================
# 一集流水线
# ==========================================================================

class StopRequested(Exception):
    """主动停下（`--stop-after`）。**不是错误** —— 是"按要求停在这里"。"""


def _cover_enabled() -> bool:
    """封面设计师是否启用（环境变量开关；默认关）。"""
    import os as _os

    return str(_os.environ.get("LITTLE_CREW_COVER") or "").strip() in (
        "1", "true", "yes", "on")


def _cs_enabled() -> bool:
    """角色图集是否启用（环境变量开关；默认关 —— 按张计费）。"""
    from little_crew.runtime.character_sheets import enabled_by_env

    return enabled_by_env()


class EpisodeRunner:
    """把一集从头跑到尾。

    **它不做决策** —— 决策由各职位做，它只负责"按顺序把它们串起来，
    并把产出记录下来"。这与 :class:`CrewOrchestrator` 的分工一致：
    编排器管协作，本类管**交付**。
    """

    def __init__(self, orchestrator: Any, renderer: Any, *,
                 episode_id: str = "E001", title: str = "",
                 checkpoint: Any = None) -> None:
        # **每实例**一份，避免不同 episode 之间串控制图。
        self.previs_first: dict[str, Path] = {}
        #: 进度落盘器（首次 `_mark` 时按 workdir 建）
        self.progress: Any = None
        #: 结构化日志（首次 `_mark` 时按 workdir 建）
        self.runlog: Any = None
        #: 场景主体冲突（分镜越权引入了剧本没有的场景主体）
        self.scene_conflicts: list[dict] = []
        #: 每镜用到了哪些角色锚点图（`shotId -> [角色名]`）。
        #: ⚠️ **必须在 `__init__` 里初始化**：2026-11-05 第一次接线时忘了，
        #: 于是 `_bridge` 里赋值抛 AttributeError，被外层 except 吞掉，
        #: 结果是"接了但一个锚点都没用上"—— 又是一次静默失败。
        self._anchors_used: dict[str, list[str]] = {}
        #: 锚点图发现结果缓存（名字 -> 路径）
        self._anchor_table: dict[str, str] | None = None
        #: 场景越权是否构成**否决**（severity=error 时置 True）。
        #: 与 scene_conflicts 分开：后者是记录，这个是「能不能交付」的结论。
        self.scene_veto: bool = False
        self.orch = orchestrator
        self.renderer = renderer
        self.episode_id = episode_id
        self.title = title
        self.checkpoint = checkpoint
        self.assembler = AssemblyEditorRules()
        #: 本集的角色定妆表（文字半）。跑完 run() 后可用。
        self.characters: Any = None
        #: 当前阶段（短键：script/storyboard/…）。半成品报告与实时页面用它。
        self.stage: str = "init"
        #: 每阶段的相对时间与小信息。
        self.trace: dict[str, Any] = {}
        #: 跑到这个阶段后停下（"" = 不停）。给 `--stop-after` 用。
        self.stop_after: str = ""
        self._t0: float = time.time()

    # ---- 主流程 --------------------------------------------------------

    def _mark(self, stage: str, **info: Any) -> None:
        """记下"到了哪一步"。**纯副作用，不改变任何行为。**

        它存在的唯一理由：中途停下时，得有东西说明"停在哪、之前做了什么"。
        """
        self.stage = stage
        self.trace[stage] = {"t": round(time.time() - self._t0, 1), **info}
        # 结构化日志（2026-11-12）：与 state.json 同一处落盘，机器可读、可按阶段筛
        try:
            if getattr(self, "runlog", None) is None:
                from little_crew.runtime.runlog import RunLog

                _wd_l = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
                self.runlog = RunLog(_wd_l or ".", episode_id=self.episode_id)
            self.runlog.write("stage", stage=stage,
                              **{k: v for k, v in info.items()
                                 if isinstance(v, (str, int, float, bool))})
        except Exception:                                     # noqa: BLE001
            pass
        # 取消信号（2026-11-12）：工作目录出现 CANCEL 文件 -> 在当前阶段边界停下。
        # 只**优雅停止**（写半成品报告），不杀进程 —— 杀进程会留下半写的产物。
        try:
            from little_crew.runtime.progress import cancel_requested

            _wd_c = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
            if _wd_c and cancel_requested(_wd_c):
                raise StopRequested("收到取消标记（CANCEL）：在当前阶段停下")
        except StopRequested:
            raise
        except Exception:                                     # noqa: BLE001
            pass
        # 落盘一份可被外部读取的进度（2026-11-12）：长跑不再只能盯 stdout
        try:
            if getattr(self, "progress", None) is None:
                from little_crew.runtime.progress import Progress

                _wd = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
                self.progress = Progress(_wd or ".", episode_id=self.episode_id)
            self.progress.mark(stage, **{k: v for k, v in info.items()
                                         if isinstance(v, (str, int, float, bool))})
        except Exception:                                     # noqa: BLE001
            pass
        if self.stop_after and stage == self.stop_after:
            raise StopRequested(f"停在「{stage}」（--stop-after）")

    def _run_human_gate(self, ok: bool, reason: str, *, manifest, failed) -> tuple:
        """人工 Gate：要不要问人、问了怎么改判。返回 `(ok, reason, hg)`。

        为什么抽出来：这段 25 行、且**只碰"判决"不碰产物**。
        判据很硬（只有 reject 能改判；approve 不能把失败变成功；skip 什么都不做），
        留在 `run()` 里时很容易被当成"打印装饰"。
        """
        _hg = None
        _ok, _reason = ok, reason
        if getattr(self, "human_gate_enabled", False):
            try:
                import json as _jh

                from little_crew.runtime import human_gate as _hgm

                _verrs = sum(
                    1 for f in ((getattr(self, "_vision_review", None) or {})
                                .get("findings") or ())
                    if f.get("severity") == "error")
                _sum = _hgm.summarize(
                    manifest.shot_count, manifest.total_seconds,
                    pending=len(failed), vision_errors=_verrs, gate_ok=bool(_ok))
                _res_hg = _hgm.decide(
                    auto=str(getattr(self, "gate_auto", "") or ""), summary=_sum)
                _hg = _hgm.as_record(_res_hg, shots=manifest.shot_count,
                                     seconds=manifest.total_seconds)
                print(f"  人工确认：{_hg['decision']}"
                      f"（{'人已看过' if _hg['reviewed'] else '未问人'}）")
                # 改判规则抽成纯函数（human_gate.apply），可单测：
                # **只有 reject 改判**；approve 不能把失败变成功、skip 不做任何事
                _ok, _reason = _hgm.apply(_ok, _reason, _hg)
            except Exception as _e_hg:                       # noqa: BLE001
                print(f"  ⚠️ 人工 Gate 未运行：{type(_e_hg).__name__}: {_e_hg}")
        else:
            # 没开也要能回答"这集有没有人看过"
            _hg = {"kind": "human.gate", "decision": "skip", "reviewed": False,
                   "approved": False, "assisted": False, "by": "disabled",
                   "reason": "未开启人工 Gate（--human-gate 可开）",
                   "interactive": False, "shots": manifest.shot_count,
                   "seconds": round(manifest.total_seconds, 1)}
        return _ok, _reason, _hg

    def _dump_script_rounds(self, cycle) -> None:
        """把剧本闭环逐轮落盘（`delivery/` + `research/script-rounds/`）。

        为什么单独成方法：这段 62 行只做"记录"，块外一个变量都不用；
        写坏了也不该影响出片（所以整段 try/except 吞异常并打印）。
        """
        try:
            import json as _json

            _rounds = []
            for _r in (getattr(cycle, "rounds", None) or ()):
                _item = {"verdict": str(getattr(_r, "verdict", ""))}
                for _k in ("total", "score", "blocker", "advisory",
                           "scenesWithIssues", "round"):
                    _v = getattr(_r, _k, None)
                    if _v is not None and not callable(_v):
                        _item[_k] = _v if isinstance(_v, (int, float, str)) else str(_v)
                # 退回原因：把可能承载文本的属性都试一遍
                for _k in ("issues", "notes", "reasons", "advice", "detail", "feedback"):
                    _v = getattr(_r, _k, None)
                    if _v:
                        _item[_k] = _json.loads(_json.dumps(_v, ensure_ascii=False, default=str))
                if hasattr(_r, "as_dict"):
                    try:
                        _item["asDict"] = _r.as_dict()
                    except Exception:                        # noqa: BLE001
                        pass
                _rounds.append(_item)
            _wd = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
            if _wd and _rounds:
                _out = Path(str(_wd)) / "delivery"
                _out.mkdir(parents=True, exist_ok=True)
                (_out / "script.rounds.json").write_text(
                    _json.dumps({"episodeId": self.episode_id,
                                 "rounds": _rounds,
                                 "accepted": bool(getattr(cycle, "accepted", False)),
                                 "reason": str(getattr(cycle, "reason", ""))[:400]},
                                ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
                # provider 指纹（2026-11-06）：**这个点每集都会经过** ——
                # 剧本阶段的模型调用已经发生，先记下来。
                # （scene_quota 那个点只在分镜成功后才到，会被 0 镜挡掉。）
                try:
                    from little_crew.governance.provider_fingerprint import (
                        summary as _pfs0)

                    (_out / "provider_fingerprint.json").write_text(
                        _json.dumps(_pfs0(), ensure_ascii=False, indent=2),
                        encoding="utf-8")
                except Exception:                       # noqa: BLE001
                    pass

                # ⚠️ **必须双写**（2026-11-03）：`outputs/` 是会定期清理的目录，
                # 而实验记录不能跟着被删 —— 上一次清理就把 E3 的 7 对样本删掉了，
                # 导致汇总结果从 +2.81 翻成 -2.08，差点当成「结论翻转」。
                try:
                    # 测试与自检不该往仓库里写实验记录（2026-11-12）：
                    # 每次跑 `tests/run_all.py` 都会把 E100.json 改一遍（CLI 自检的集号），
                    # 于是工作区永远"脏一个文件"。`LITTLE_CREW_RESEARCH_DUMP=0` 可关。
                    import os as _os_rd

                    if str(_os_rd.environ.get("LITTLE_CREW_RESEARCH_DUMP", "1")) == "0":
                        raise RuntimeError("research 落盘已关闭（LITTLE_CREW_RESEARCH_DUMP=0）")
                    _repo = Path(__file__).resolve().parents[3]
                    _dst = _repo / "research" / "script-rounds"
                    _dst.mkdir(parents=True, exist_ok=True)
                    (_dst / f"{self.episode_id}.json").write_text(
                        (_out / "script.rounds.json").read_text(encoding="utf-8"),
                        encoding="utf-8")
                except Exception as _e2:                     # noqa: BLE001
                    if "已关闭" not in str(_e2):
                        print(f"  ⚠️ 实验记录未备份到 research/：{type(_e2).__name__}: {_e2}")
                print(f"  剧本轮次已落盘：{len(_rounds)} 轮"
                      f" -> delivery/ 与 research/script-rounds/")
        except Exception as _e:                              # noqa: BLE001
            print(f"  ⚠️ 剧本轮次未落盘：{type(_e).__name__}: {_e}")


    def _audit_scene_quota(self) -> None:
        """镜头配额对账 + 再分配处置（**附加产物，不参与门禁**）。

        抽出来的理由：这段 80 行只读黑板、只写 `delivery/*.json`，
        块内变量在块外一个都不用；留在 `run()` 里会把"分镜检查"和"越权检查"隔开。
        异常一律吞掉 —— 对账失败不该把一集跑挂。
        """
        try:
            from little_crew.governance.plan_alloc import audit as _qa
            from little_crew.governance.plan_alloc import quotas_for as _qf

            _plan0 = None
            for _k0 in ("episode.plan", "director.plan"):
                try:
                    _c0 = self.orch.blackboard.value(_k0)
                except Exception:                          # noqa: BLE001
                    _c0 = None
                if isinstance(_c0, dict):
                    _in0 = _c0.get("plan") if isinstance(_c0.get("plan"), dict) else _c0
                    if isinstance(_in0, dict) and _in0.get("scenes"):
                        _plan0 = _in0
                        break
            _q = _qf(_plan0, getattr(script_draft, "scenes", ()) or ())
            _q["audit"] = _qa(_q, shots)
            _wdq = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
            if _wdq:
                import json as _jq

                (_outq := Path(str(_wdq)) / "delivery").mkdir(
                    parents=True, exist_ok=True)
                (_outq / "scene_quota.json").write_text(
                    _jq.dumps(_q, ensure_ascii=False, indent=2), encoding="utf-8")
            _au = _q["audit"]
            print(f"  配额遵守：{_au['compliantScenes']}/{_au['sceneCount']} 场"
                  f"（{_au['complianceRate']*100:.0f}%）· 越界 {len(_au['findings'])} 条")

            # ---- 处置闭环（P3，2026-11-06）----
            #
            # 审计只说"哪场超了"；**处置说"怎么改"**。
            # 有了它，定向返工才不是"再抽一次"，而是"带着改法重切"。
            try:
                from little_crew.governance import disposition as _dp

                _disp = _dp.plan(_au["findings"], _q)
                _q["disposition"] = _disp
                if _wdq:
                    (_outq / "scene_disposition.json").write_text(
                        _jq.dumps(_disp, ensure_ascii=False, indent=2),
                        encoding="utf-8")
                if _disp["sceneCount"]:
                    print(f"  处置：{_disp['sceneCount']} 场需要改"
                          f"（{', '.join(x['sceneId'] for x in _disp['scenes'])}）")

                # ---- 一轮时长再分配（P4，2026-11-06）----
                # 把有余量的场借给超时的场。**只允许一轮** —— 反复借会震荡。
                try:
                    from little_crew.governance.plan_alloc import (
                        redistribute as _rd)

                    _rr = _rd(_q, _q["audit"], round_no=1, max_rounds=1)
                    _q["redistribution"] = _rr
                    if _rr["applied"]:
                        _mv = ", ".join(m["from"] + "→" + m["to"]
                                        for m in _rr["moves"][:3])
                        print(f"  再分配：借出 {_rr['totalMoved']:.1f}s（{_mv}）")
                    if _wdq:
                        (_outq / "scene_quota.json").write_text(
                            _jq.dumps(_q, ensure_ascii=False, indent=2),
                            encoding="utf-8")

                    # provider 指纹（2026-11-06）：此刻剧本/导演/分镜的模型调用
                    # 都已发生 —— 先把已发生的记下来，避免"只在完整跑才有"。
                    try:
                        from little_crew.governance.provider_fingerprint import (
                            summary as _pfs)

                        (_outq / "provider_fingerprint.json").write_text(
                            _jq.dumps(_pfs(), ensure_ascii=False, indent=2),
                            encoding="utf-8")
                    except Exception:                       # noqa: BLE001
                        pass
                except Exception as _e_rd:                 # noqa: BLE001
                    print(f"  ⚠️ 再分配未运行：{type(_e_rd).__name__}: {_e_rd}")
            except Exception as _e_dp:                      # noqa: BLE001
                print(f"  ⚠️ 处置未生成：{type(_e_dp).__name__}: {_e_dp}")
        except Exception as _e_q:                          # noqa: BLE001
            print(f"  ⚠️ 配额对账未运行：{type(_e_q).__name__}: {_e_q}")


    def _check_interface_defects(self, picked) -> None:
        """接口缺陷检查：落 `delivery/interface_defects.json`（**附加产物，不参与门禁**）。

        为什么抽出来：这段 191 行、只做"检查 + 落盘"，块内变量在块外一个都不用；
        留在 `run()` 里像一道墙，把"渲染"和"合成"隔开，读的人会以为它俩是一段。
        异常一律吞掉 —— 它是审计附加物，不该把一集跑挂。
        """
        try:
            from little_crew.review import interface_defects as _idef

            _plan = None
            try:
                # ⚠️ 真实键是 `episode.plan`（2026-11-05 查出来的）。
                # 原来读 `director.plan` —— 那个键没人写，于是预算偏差**永不触发**，
                # 而检查器看上去"有这一项"。保留旧键作兼容回退。
                _plan = None
                _plan_key = ""
                for _k in ("episode.plan", "director.plan", "episode.direct"):
                    try:
                        _cand = self.orch.blackboard.value(_k)
                    except Exception:                          # noqa: BLE001
                        _cand = None
                    # 黑板里可能是**包装过的**：`{"ok": bool, "plan": {...}}`
                    # （见本文件里 episode.plan 的说明）。两种都要认。
                    if isinstance(_cand, dict):
                        _inner = _cand.get("plan") if isinstance(_cand.get("plan"), dict) else _cand
                        if isinstance(_inner, dict) and _inner.get("scenes"):
                            _plan = _inner
                            _plan_key = _k
                            break
            except Exception:                                  # noqa: BLE001
                _plan = None
            if not isinstance(_plan, dict):
                _plan = None
            # ---- 成片整体审（`review_board`）—— 2026-11-08 首次接上 ----
            #
            # 为什么显式传 reviewers：`_REVIEWERS_BY_ARTIFACT["shot.list"]` 配的是
            # `dramaturge`（它只审 `script.draft`），走默认映射**必然失败**；
            # 而 `review_board` 的 CRITIC_CHECKS 声明的产物正是 `shot.list`。
            # 也就是说：**这个审查职位一直存在，却从来没有被调用过。**
            #
            # 纪律：**没审 != 通过** —— 没绑定/失败都要写进产物。
            _rb = {"kind": "shotlist.review", "reviewed": False,
                   "reviewers": [], "blockers": 0, "why": "", "defects": []}
            try:
                _impls = getattr(self.orch, "_impls", {}) or {}
                if "review_board" in _impls:
                    try:
                        _sl = self.orch.blackboard.value("shot.list")
                    except Exception:                          # noqa: BLE001
                        _sl = None
                    _revs, _why = self.orch.review_artifact(
                        "shot.list", generator_id="storyboard_artist",
                        payload={"shot.list": _sl if _sl is not None else list(picked)},
                        reviewers=("review_board",))
                    _rb["reviewers"] = ["review_board"]
                    if _why:
                        _rb["why"] = str(_why)[:200]
                    else:
                        _rb["reviewed"] = True
                        _txt = " ".join(
                            str(x) for _f in (_revs or ())
                            for x in (getattr(_f, "issues", ()) or ()))
                        import re as _re_rb

                        for _m in _re_rb.finditer(r"S\d{2}", _txt):
                            _rb["defects"].append({
                                "sceneId": _m.group(0), "kind": "review.blocker",
                                "severity": "error", "reason": _txt[:160]})
                        _rb["blockers"] = len(_rb["defects"])
                    # 措辞必须区分三态：有意见 / 通过 / **未审**
                    _state = ("有意见" if _rb["blockers"]
                              else ("通过" if _rb["reviewed"] else "未审"))
                    print(f"  成片整体审：{_state}（{_rb['blockers']} 条）"
                          + (f" · {_rb['why']}" if _rb["why"] else ""))
                else:
                    _rb["why"] = "review_board 未绑定（本次未审）"
            except Exception as _e_rb:                          # noqa: BLE001
                _rb["why"] = f"{type(_e_rb).__name__}: {_e_rb}"[:200]
            _wd_rb = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
            if _wd_rb:
                try:
                    (_o_rb := Path(str(_wd_rb)) / "delivery").mkdir(
                        parents=True, exist_ok=True)
                    import json as _j_rb

                    (_o_rb / "shotlist_review.json").write_text(
                        _j_rb.dumps(_rb, ensure_ascii=False, indent=2),
                        encoding="utf-8")
                except Exception:                              # noqa: BLE001
                    pass

            _payload = _idef.collect(
                scene_conflicts=self.scene_conflicts,
                shots=picked,
                limit_s=8.0,
                plan=_plan,
                episode_id=self.episode_id,
                budget_shots=shots,
            )
            _payload.setdefault("inputs", {})["planKeyHit"] = _plan_key
            if _rb.get("defects"):        # 整体审的意见并进缺陷清单，让定向返工接住
                _payload.setdefault("defects", []).extend(_rb["defects"])
            _wd = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
            if _wd:
                # ---- 定向返工（§3.10.2 第二层执行，2026-11-04）----
                #
                # 有 error 级缺陷时，只重跑受影响的那几场（默认最多 3 场），
                # 再整场替换回镜头表 —— 不做整篇重抽。
                # ⚠️ 关了开关就退回"只记录不重做"：`LITTLE_CREW_REWORK=0`
                try:
                    import os as _os

                    from little_crew.runtime import rework as _rw2
                    from little_crew.runtime import rework_pipeline as _rwp

                    _on = (_os.environ.get("LITTLE_CREW_REWORK") or "1") != "0"
                    _want = bool(_on and _rw2.should_rework(_payload.get("defects")))
                    if not _want:
                        # **无论有没有重做，都写一条结论** ——
                        # 否则 `reworkRun` 缺失会被误读成"没接上"（真跑时踩过）。
                        _payload["reworkRun"] = {
                            "ok": True, "skipped": True,
                            "reason": ("开关关闭" if not _on else "无需重做"),
                        }
                    if _want:
                        _res = _rwp.rework_targets(
                            run_stage=self.orch.run_stage,
                            draft=script_draft,
                            defects=_payload.get("defects") or [],
                            shots=picked,
                            max_scenes=3,
                            get_shots=lambda: self.orch.blackboard.value("shot.list"),
                            put_back=lambda sl: self.orch.blackboard.put(
                                "shot.list", sl, owner="storyboard_artist"),
                        )
                        _payload["reworkRun"] = {
                            "ok": bool(_res.get("ok")),
                            "reworked": _res.get("reworked") or [],
                            "failed": _res.get("failed") or [],
                            "addedShots": _res.get("addedShots", 0),
                            "reason": _res.get("reason", ""),
                        }
                        if _res.get("ok"):
                            picked = tuple(_res["shots"])
                            shots = picked
                            print(f"  ↻ {_res.get('reason')}")
                            # ---- 重做后**必须复验**（2026-11-05 真跑发现）----
                            #
                            # 原来重做完不复验：`self.scene_conflicts` 还是重做前那份，
                            # 于是打印「场景越权否决交付」—— **重做了却按旧结论判**。
                            # 这里用同一个判据重扫一遍合并后的镜头：
                            #   干净了 -> 清掉 error；还在 -> 保留（并说明是复验结果）
                            try:
                                from little_crew.review.scene_subject import (
                                    check_scene_subject as _csc,
                                    subjects_in as _s_in,
                                )

                                # 就地取一份，别依赖别的**方法**的局部量：
                                # `_script_text` 原来只存在于 `_check_scene_subjects` 里，
                                # 抽出方法后 run() 没有它 -> NameError ->
                                # 复验整段被 except 吞掉（EF24 只能靠修复块复算兜住）。
                                _script_text = str(script_draft or "")
                                _allowed = set(_s_in(_script_text))
                                _fresh = []
                                for _sh in picked:
                                    _r2 = _csc(_script_text, _sh.description,
                                               allowed=_allowed)
                                    _fresh.append({"shotId": _sh.shot_id, **_r2})
                                self.scene_conflicts = _fresh
                                _err2 = [c for c in _fresh
                                         if c.get("severity") == "error"]
                                # ⚠️ 复验也必须更新 `scene_veto`：
                                # 它是 CLI 门禁唯一读的标志，2.8 时置过 True，
                                # 只清 conflicts 不清它 → 重做完照样否决（EF20）。
                                self.scene_veto = bool(_err2)
                                print(f"  复验：越权 {len(_err2)} 镜"
                                      + ("（已修好）" if not _err2 else "（仍在）"))
                            except Exception as _e2:               # noqa: BLE001
                                print(f"  ⚠️ 重做后未能复验："
                                      f"{type(_e2).__name__}: {_e2}")
                        elif _res.get("reason"):
                            print(f"  ⚠️ 定向返工未生效：{_res.get('reason')}")
                except Exception as _e:                        # noqa: BLE001
                    print(f"  ⚠️ 定向返工未运行：{type(_e).__name__}: {_e}")

                # §3.10.2 第二层：把缺陷换成「只重做哪几处」的计划
                try:
                    from little_crew.runtime import rework as _rw

                    _payload["reworkPlan"] = _rw.plan(_payload.get("defects") or [])
                except Exception:                          # noqa: BLE001
                    pass
                _dst = _idef.write(_wd, _payload)
                print(f"  接口缺陷 {_payload['total']} 条 -> {_dst.name}")
        except Exception as _e:                                # noqa: BLE001
            print(f"  ⚠️ 接口缺陷清单未生成：{type(_e).__name__}: {_e}")


    def _compare_ir_hashes(self, picked, irs) -> None:
        """IR 指纹：存每镜内容哈希，并在续跑时对比打印"哪一镜变了"。

        为什么单独成方法：这段有 57 行、且**纯诊断**（不影响产物）。
        留在 `run()` 里时，它把"渲染主流程"切成了两段，读的人容易跳过；
        抽出来以后 `run()` 只剩"取 IR -> 渲染"两步。
        异常一律吞掉 —— 诊断失败不该影响出片。
        """
        try:
            from little_crew.ir.canonical import content_hash as _ch

            _irh = {str(s.shot_id): _ch(ir) for s, ir in zip(picked, irs)}
            self._ir_hashes_now = _irh
            _old = (getattr(self, "resume_data", None) or {}).get("irHashes") or {}
            _old_json = (getattr(self, "resume_data", None) or {}).get("irJson") or {}
            _self_json = {}
            try:
                import json as _j_ir

                _wd_ir2 = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
                if _wd_ir2:
                    (Path(str(_wd_ir2)) / "_ir_current.json").write_text(
                        _j_ir.dumps({str(s.shot_id): ir
                                     for s, ir in zip(picked, irs)},
                                    ensure_ascii=False, indent=2),
                        encoding="utf-8")
            except Exception:                            # noqa: BLE001
                pass
            try:
                from little_crew.runtime import stage_checkpoint as _sc_ir

                _wd_ir = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
                if _wd_ir:
                    _self_json = {str(s.shot_id): ir for s, ir in zip(picked, irs)}
            except Exception:                            # noqa: BLE001
                pass
            if _old:
                _same = sum(1 for k, v in _irh.items() if _old.get(k) == v)
                print(f"  IR 对比：{_same}/{len(_irh)} 镜哈希相同"
                      + ("（应命中缓存）" if _same == len(_irh) else "（**不同 → 必重渲**）"))
                _diff = [k for k, v in _irh.items() if _old.get(k) and _old.get(k) != v]
                if _diff:
                    print(f"    首个不同: {_diff[0]}")
                    # 字段级 diff：只存哈希时说不出"哪不同"
                    try:
                        _old_ir = (_old_json.get(_diff[0]) or {})
                        _new_ir = (_self_json.get(_diff[0]) or {})
                        _paths = []

                        def _walk(a, b, path=""):
                            if len(_paths) >= 6:
                                return
                            if isinstance(a, dict) and isinstance(b, dict):
                                for k in set(a) | set(b):
                                    _walk(a.get(k), b.get(k), f"{path}.{k}" if path else k)
                            elif a != b:
                                _paths.append((path, str(a)[:40], str(b)[:40]))

                        _walk(_old_ir, _new_ir)
                        for _p, _a, _b in _paths:
                            print(f"      {_p}: {_a} -> {_b}")
                    except Exception:                        # noqa: BLE001
                        pass
        except Exception:                                    # noqa: BLE001
            pass
    def run(self, source: str, *, limit: int | None = None,
            on_progress: Any = None) -> EpisodeResult:
        t0 = time.perf_counter()

        # ---- LLM 探活（2026-11-11）----
        #
        # EF31 现场：剧本跑到一半，云端点开始 `10061 拒绝连接`，
        # 于是一场 11 分钟的剧本白跑、分镜 3 次重试全废、退出码 1。
        # 开跑前先问一句「你还在吗」：失败重试 3 次（间隔 5 秒），
        # 仍失败就明确报警 —— 别让人等 10 分钟才看到一个网络错误。
        try:
            from little_crew.agent.llm import LlmClient as _LC_probe

            _ok_llm = False
            for _try_p in range(1, 4):
                try:
                    _r_p = _LC_probe(
                        getattr(self, "script_model", "") or "deepseek-flash",
                        timeout_s=20).chat("回复 OK")
                    _ok_llm = bool(getattr(_r_p, "text", _r_p))
                except Exception as _e_p:                     # noqa: BLE001
                    _ok_llm = False
                    print(f"  ⏳ LLM 探活失败（第 {_try_p} 次）：{type(_e_p).__name__}")
                if _ok_llm:
                    print(f"  ✅ LLM 探活通过（第 {_try_p} 次）")
                    break
                import time as _t_p

                _t_p.sleep(5)
            if not _ok_llm:
                print("  ⚠️ LLM 端点不可用 —— 后面的剧本/分镜很可能整段失败")
        except Exception:                                    # noqa: BLE001
            pass

        # ---- 1) 剧本闭环 ----
        #: `--limit` 的值。白模与渲染都要用它，否则两者看的镜头不一致。
        self._limit = int(limit or 0)
        self._mark("script")
        # ---- 续跑：剧本阶段已有产物就直接用（2026-11-08）----
        #
        # 为什么不"跳过一段代码"：`run()` 各阶段共享十几个局部变量，
        # 跳过等于改主流程。这里只替换**取值的来源**，后面代码一行不动。
        _resume = getattr(self, "resume_data", None) or {}
        _resume_draft = _resume.get("draft")
        if _resume_draft:
            from little_crew.agent.crew_llm import _as_draft as _asd

            script_draft = _asd(_resume_draft)
            cycle = _ResumedCycle(script_draft)
            print("  ↩ 剧本：从 checkpoint 续（未重新生成）")
        else:
            # 空剧本**先重试**再判失败（2026-11-11）
            #
            # EF16 / EF31 都栽在这里：编剧调用返回 `[no-content]`（空响应），
            # 一次就把整集判死 —— 而空响应是**瞬时**的（同一题重跑往往就好）。
            # 最多 3 次尝试，每次带 3 秒退避；3 次都空才判失败。
            _scr_tries = max(1, int(getattr(self, "script_empty_retries", 3)))
            cycle = None
            for _i_s in range(1, _scr_tries + 1):
                cycle = self.orch.run_script_cycle(
                    {"source.novel": source, "episodeId": self.episode_id,
                     "title": self.title})
                _draft_i = getattr(cycle, "final", None)
                if _draft_i is None and getattr(cycle, "rounds", None):
                    _draft_i = getattr(cycle.rounds[-1], "draft", None)
                _n_sc = len(getattr(_draft_i, "scenes", ()) or ())
                if _n_sc:
                    if _i_s > 1:
                        print(f"  ↻ 剧本重试成功（第 {_i_s} 次，{_n_sc} 场）")
                    break
                if _i_s < _scr_tries:
                    print(f"  ↻ 剧本为空（第 {_i_s} 次），3 秒后重试…")
                    import time as _t_s

                    _t_s.sleep(3)
                else:
                    print(f"  ⚠️ 剧本连续 {_scr_tries} 次为空")
                    self._script_empty_attempts = _scr_tries

        # ---- 剧本闭环逐轮落盘（2026-11-01）----
        #
        # 为什么：`research/retry-cost` 的 10 次记录里只有分数，
        # **没有一句"为什么被退回"** —— 于是只能回答"变没变"，
        # 回答不了"是返工没用，还是退回指令写得太差"。
        #
        # 这里把每轮的 verdict / 分数 / 可用的 issues 文本尽量 dump 出来。
        # **用 getattr 防御式取**：不同 critic 的字段名不一定一致，
        # 取不到就留空，**绝不因为"记录"而让主流程失败**。
        self._dump_script_rounds(cycle)
        if not cycle.accepted and not self.force_accept_script:
            return EpisodeResult(
                episode_id=self.episode_id, ok=False,
                script_rounds=cycle.round_count,
                script_verdict=(cycle.rounds[-1].verdict.value
                                if cycle.rounds else ""),
                    failure_kind=cycle.failure_kind,
                reason=f"剧本阶段未通过：{cycle.reason}",
                wall_seconds=time.perf_counter() - t0)

        # 强行接受时 `cycle.final` 是 None —— 退回**最后一轮的稿子**。
        # 注意这不是"当作通过"：`ok` 仍会如实标记，
        # 证据里也会保留 `scriptAccepted=False`（见下）。
        script_draft = cycle.final
        if script_draft is None and self.force_accept_script and cycle.rounds:
            script_draft = cycle.rounds[-1].draft

        # ---- 阶段 checkpoint：剧本（2026-11-08）----
        try:
            from little_crew.runtime import stage_checkpoint as _sc

            _wd_ck = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
            if _wd_ck and script_draft is not None and not _resume_draft:
                _sc.save(_wd_ck, "script",
                         {"draft": (script_draft.as_dict()
                                    if hasattr(script_draft, "as_dict")
                                    else script_draft)},
                         meta={"episodeId": self.episode_id,
                               "sourceLen": len(str(source or "")),
                               "sourceHead": str(source or "")[:80]})
        except Exception:                                    # noqa: BLE001
            pass

        # ---- 1.5) 导演：定预算，交给分镜 ----
        self._mark("director")
        #
        # ⚠️ **顺序是这个职位有没有意义的全部**：导演的产物是
        # "每场几个镜头"，**晚于分镜就毫无价值**。
        # （第一版插在桥接前 —— 那时分镜早就跑完了，
        #   于是预算一条都没生效。是 `D8` 那条结构断言抓到的。）
        director_info = self._run_director(script_draft)
        # 美术指导：定空间与材质（在灯光之前 —— 先有"东西"，再谈"光怎么打"）
        art_info = self._run_designer(script_draft)
        # 角色设计师·图像半（2026-11-13）：把文字角色表画成锚点图集。
        # **默认关闭**（按张计费）；失败不阻断整集，只留痕。
        self._run_character_sheets(script_draft)
        self._mark("art")
        # 灯光师在导演之后（参考基调）、分镜之前（光要进画面描述）
        lighting_info = self._run_gaffer(script_draft)
        self._mark("lighting")

        # ---- 2) 分镜 ----
        self._mark("storyboard")
        # （制片主任在分镜产出后跑 —— 它消费镜头表）
        # ---- 0 镜自动重试（2026-11-08，P0）----
        #
        # 实测：分镜偶发返回 0 镜（模型输出被截断/结构不对），
        # 之前**一失败就整集结束**，每集都要人手工重跑。
        # 现在自动重试（默认 2 次），并把每次的结果记进报告 ——
        # 重试不是"掩盖失败"：重试几次、每次拿到几镜，都要能看见。
        # ---- 空剧本守卫（2026-11-10）----
        #
        # 真跑 EF16：剧本调用返回 `[no-content]`（空响应），`--accept-script` 又把它放行，
        # 于是分镜在**空输入**上重试 3 轮、每轮都 0 镜 —— 白等 4 分钟才报错。
        # 空剧本是**上游失败**，不该由下游重试来"扛"。
        if not (getattr(script_draft, "scenes", ()) or ()):
            return EpisodeResult(
                episode_id=self.episode_id, ok=False,
                reason=(f"剧本为空：编剧连续 "
                        f"{int(getattr(self, 'script_empty_retries', 3))} 次"
                        f"没有产出任何场次，分镜无从谈起"),
                failure_kind="script-failed")

        _sb_attempts: list[dict] = []
        _sb_retries = int(getattr(self, "storyboard_retries", 2))
        out = None
        # 续跑：分镜已有产物 -> 塞回黑板，不调模型
        if (_resume_shots := (getattr(self, "resume_data", None) or {}).get("shotList")):
            from little_crew.runtime.replay_agent import _restore as _rs

            _obj = _rs(_resume_shots, "shot.list")
            if _obj and _obj.get("shot.list") is not None:
                self.orch.blackboard.put("shot.list", _obj["shot.list"],
                                         owner="storyboard_artist")
                print("  ↩ 分镜：从 checkpoint 续（未重新生成）")
                class _OK:                       # 轻量结果对象，字段与 StageOutcome 齐
                    ok = True
                    error = ""
                out = _OK()
                _sb_attempts.append({"attempt": 0, "ok": True,
                                     "shots": len(_obj["shot.list"].shots),
                                     "error": "", "resumed": True})
                _sb_retries = -1
        for _i in range(1 + max(0, _sb_retries)):
            # ---- 逐场分镜（2026-11-09）----
            #
            # `storyboard_per_scene` 早就写好（"结构上消灭输出被截断"），
            # 但**从来没有被调用过** —— episode 一直是一次要 7 场 × 4 镜，
            # 于是整集真跑经常 `分镜：0 个镜头`（今天连试 3 次都是 0）。
            # 这里改成逐场：每次只要一场的输出，短到不可能被截断；
            # 任何一场失败则整体失败（与设计一致）。
            from little_crew.runtime import storyboard_stage as _sbs

            # 场景主体白名单：剧本里出现的场景名词（runtime 可以 import review）
            try:
                from little_crew.review.scene_subject import subjects_in as _subj_ck

                _allowed_ck: list[str] = []
                for _sc_ck in (getattr(script_draft, "scenes", ()) or ()):
                    for _s_ck in _subj_ck(_sc_ck):
                        if _s_ck and _s_ck not in _allowed_ck:
                            _allowed_ck.append(_s_ck)
            except Exception:                                # noqa: BLE001
                _allowed_ck = []

            def _sb_call(sub):
                """`run_stage` 返回 `StageOutcome`，而 `storyboard_per_scene`
                需要 `CallResult`（读 `.output["shot.list"]`）—— 这里做转换。
                （2026-11-09 真跑踩到：`'StageOutcome' object has no attribute 'output'`）
                """
                from little_crew.agent.protocol import CallResult

                # 第 2 次起不带白名单：万一是"只许出现这些主体"把模型逼成 0 镜，
                # 也该在重试里被排除掉（2026-11-10 加）。
                _subs_try = _allowed_ck if _sb_attempts == [] else []
                if _sb_attempts and _allowed_ck:
                    pass  # 打印在下面统一给出，避免每场刷屏
                o = self.orch.run_stage("storyboard_artist", "shot.list",
                                        {"script.final": sub,
                                         "scene.subjects": _subs_try})
                if not getattr(o, "ok", False):
                    return CallResult.fail("F-STAGE",
                                           str(getattr(o, "error", "") or "分镜失败"))
                sl = self.orch.blackboard.value("shot.list")
                return CallResult.success({"shot.list": sl})

            # 只在**真实 LLM 分镜师**上逐场：桩实现是按整份稿返回的，
            # 逐场拆给它会破坏"每场至少 2 镜"等既有契约（episode_check A3/H5 抓到）。
            _impl = (getattr(self.orch, "_impls", {}) or {}).get("storyboard_artist")
            _is_llm = type(_impl).__name__ == "StoryboardArtistLlm"
            if _is_llm:
                # 并发**默认关闭**（2026-11-11 实测）：
                #   串行 8 场 = 83.5s · 并发 4 线程 = 95.5s —— 并发反而更慢。
                # 原因是单场只要 ~10s，线程开销 + 服务端限流吃掉了收益。
                # 保留开关（`LITTLE_CREW_SB_PARALLEL=1`）：端点变慢/长提示时再试。
                import os as _os_sb

                _par = (_os_sb.environ.get("LITTLE_CREW_SB_PARALLEL") or "0") == "1"
                _res = _sbs.per_scene(self.orch, script_draft,
                                      parallel=_par, subjects=tuple(_allowed_ck),
                                      progress=lambda sid, n: print(
                                          f"    分镜 {sid}: {n} 镜"))
                if not _res["ok"]:
                    print(f"  ⚠️ 逐场分镜失败：{_res['reason']}")
                    out = self.orch.run_stage(
                        "storyboard_artist", "shot.list",
                        {"script.final": script_draft})
                else:
                    out = type("_O", (), {"ok": True, "error": ""})()
            else:
                out = _sb_call(script_draft)
            _got = 0
            try:
                _sl0 = self.orch.blackboard.value("shot.list")
                _got = len(getattr(_sl0, "shots", ()) or ())
            except Exception:                                  # noqa: BLE001
                _got = 0
            _sb_attempts.append({"attempt": _i + 1, "ok": bool(out.ok),
                                 "shots": _got,
                                 "error": str(getattr(out, "error", "") or "")[:120]})
            if out.ok and _got > 0:
                break
            if _i < _sb_retries:
                print(f"  ↻ 分镜第 {_i + 1} 次没出镜头（{_got} 镜），重试…")
        self._storyboard_attempts = _sb_attempts
        if len(_sb_attempts) > 1:
            print(f"  分镜重试：共 {len(_sb_attempts)} 次（"
                  + " / ".join(f"{a['shots']}镜" for a in _sb_attempts) + "）")
        try:
            _wd_sb = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
            if _wd_sb:
                (_osb := Path(str(_wd_sb)) / "delivery").mkdir(
                    parents=True, exist_ok=True)
                import json as _jsb

                (_osb / "storyboard_attempts.json").write_text(
                    _jsb.dumps({"kind": "storyboard.attempts",
                                "attempts": _sb_attempts}, ensure_ascii=False,
                               indent=2), encoding="utf-8")
        except Exception:                                    # noqa: BLE001
            pass
        if not out.ok:
            return EpisodeResult(
                episode_id=self.episode_id, ok=False,
                script_rounds=cycle.round_count,
                    failure_kind="storyboard-failed",
                reason=("分镜阶段失败："
                        + str(getattr(out, "error", "")
                              or getattr(out, "error_message", "")
                              or "（无错误信息）")),
                wall_seconds=time.perf_counter() - t0)

        shot_list = self.orch.blackboard.value("shot.list")
        shots = tuple(getattr(shot_list, "shots", ()) or ())
        # ⚠️ **0 镜必须当场算失败**（2026-11-05 真跑 E51 发现）。
        #
        # 原来这里往下走：`picked = shots[:0]` 是空元组，
        # 后面渲染 0 镜、覆盖率 0%，而**过程一句错都不报** ——
        # 一整集就这样悄悄变成空的。
        # 这跟「缺观测 ≠ 通过」是同一条：**没有镜头不是「跑完了」，是「没跑出东西」。**
        if not shots:
            _last_verdict = (cycle.rounds[-1].verdict.value
                             if cycle.rounds else "")
            return EpisodeResult(
                episode_id=self.episode_id, ok=False,
                script_rounds=cycle.round_count,
                script_verdict=_last_verdict,
                failure_kind="storyboard-empty",
                reason=("分镜阶段返回 0 个镜头（不是跑完，是没跑出东西）；"
                        f"剧本 {cycle.round_count} 轮，最后一轮裁定 "
                        f"{_last_verdict or chr(31354)}"),
                wall_seconds=time.perf_counter() - t0)
        # ---- 阶段 checkpoint：分镜（2026-11-08）----
        try:
            from little_crew.runtime import stage_checkpoint as _sc2

            _wd_ck2 = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
            _sl_ck = self.orch.blackboard.value("shot.list")
            if _wd_ck2 and _sl_ck is not None and not _resume_shots:
                _sc2.save(_wd_ck2, "storyboard",
                          {"shotList": (_sl_ck.as_dict()
                                        if hasattr(_sl_ck, "as_dict") else _sl_ck)},
                          meta={"episodeId": self.episode_id,
                                "shots": len(getattr(_sl_ck, "shots", ()) or ())})
        except Exception:                                    # noqa: BLE001
            pass

        planned = len(shots) if limit is None else min(len(shots), int(limit))
        picked = shots[:planned]

        # ---- 场级配额与遵守情况（2026-11-06，P2）----
        #
        # 配额是**事前**的约束，这里是**事后**的对账：
        # 分镜到底有没有落在 [min,max]、有没有超场时长。
        # 落 delivery/scene_quota.json —— 既不参与门禁，也不假装通过。
        self._audit_scene_quota()

        self._check_scene_subjects(picked, script_draft)

        # ---- 接口缺陷清单（§3.10.2 第一层，2026-11-04）----
        #
        # 验证的输出不该只是 PASS/FAIL —— 那下游只能整篇重抽。
        # 这里把三类「单元之间的接口」信号格式化成可定位的缺陷，
        # 落 delivery/interface_defects.json，供定向返工与证据链使用。
        # **它是附加产物，不参与门禁判决。**
        self._check_interface_defects(picked)

        if self.scene_conflicts:
            # **按严重度分开报**：越权新增 = 协作冲突（该退）；丢场景 = 特写的常态（不该退）
            _err = [c for c in self.scene_conflicts if c.get("severity") == "error"]
            _warn = [c for c in self.scene_conflicts if c.get("severity") == "warn"]
            if _err:
                from little_crew.review.scene_subject import has_veto as _hv

                self.scene_veto = bool(_hv(_err))
                _f = _err[0]
                print(f"  ❌ 场景越权 {len(_err)} 镜（**否决交付**）"
                      f"（如 {_f.get('shotId')}：{_f.get('reason', '')[:56]}）")
            if _warn:
                print(f"  ℹ️ 镜头未提场景 {len(_warn)} 镜"
                      f"（特写常态，不算失败）")

        # ---- 2.5) 角色定妆（文字半）----
        #
        # 位置在分镜之后、桥接之前：外观要写进**每镜的提示词**，
        # 所以必须在桥接前拿到。
        #
        # **这是"角色一致"的前置**：没有它，每镜提示词里只有"少女"三个字，
        # 图像模型只能即兴发挥 —— 于是同一角色长得不一样。
        # 有了它，至少**文字层面**是一致的（图像层面还需参考图，见 §5.8）。
        # ---- 2.5) 顾问批评家（不进裁定）----
        advisory = self._run_advisory(script_draft, on_progress=on_progress)

        self.characters = self._design_characters(script_draft)

        picked, shots = self._repair_subjects(picked, shots, script_draft)

        # ---- 阶段 checkpoint：**最终**镜头表（2026-11-10）----
        #
        # ⚠️ 原来的 checkpoint 写在返工**之前** —— 返工换过镜头后它就成了旧的。
        # 续跑用旧镜头 -> IR 变 -> 缓存全 miss -> "续跑"等于重渲。
        # 真跑实测：checkpoint 里 6/5/6（17s），而成片是 21s。
        try:
            from little_crew.runtime import stage_checkpoint as _sc_f

            _wd_f = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
            if _wd_f:
                _picked_final = [{
                    "shotId": str(getattr(s, "shot_id", "")),
                    "sceneId": str(getattr(s, "scene_id", "")),
                    "shotSize": str(getattr(s, "shot_size", "")),
                    "cameraMovement": str(getattr(s, "camera_movement", "")),
                    "durationSec": float(getattr(s, "duration_sec", 0) or 0),
                    "characters": list(getattr(s, "characters", ()) or ()),
                    "action": str(getattr(s, "action", "") or ""),
                    "description": str(getattr(s, "description", "") or ""),
                } for s in picked]
                _sc_f.save(_wd_f, "storyboard",
                           {"shotList": {"kind": "shot.list",
                                         "episodeId": self.episode_id,
                                         "shots": _picked_final,
                                         "totalSeconds": round(sum(
                                             x["durationSec"] for x in _picked_final), 2)}},
                           meta={"episodeId": self.episode_id,
                                 "shots": len(_picked_final), "final": True})
        except Exception:                                    # noqa: BLE001
            pass


        # ---- 3) 桥接 ----
        # ⚠️ **提示词要进证据链**（2026-10-17 补）。
        #
        # 在此之前，清单每镜只记了 `artifacts`（文件名）+ `irHashes`（哈希）。
        # 也就是说：**能证明"这张图是我产的"，但不能证明"我当初要的是什么"**。
        # 对一个把"可审计"当核心的项目，这是缺半边 —— 哈希只能验真伪，
        # 验不了"要求与实际是否相符"。
        #
        # 只存**文字层**的提示词（外观并入后的镜头描述）：那是本项目控制得住
        # 的那一层；模板/风格后缀是编译期常量，由 `irHash` 覆盖。

        # ---- 3.8) 白模预演 + 制片主任排期（**必须在渲染之前**）----
        #
        # ⚠️ 2026-10-20 修正：这两步原来被放在「合成」之后，
        # 后果是**白模控制图永远来不及用** —— 渲染早就跑完了。
        # 顺序错的功能，看起来「跑了」，实际等于没跑。
        #
        # 依赖：白模消费 `shot.list`（分镜之后）、产出控制图（渲染之前要）；
        #       排期是前置预估，放在渲染之前才有意义（渲染后就是事后记账）。
        sched_info = self._run_line_producer()
        self._mark("line_producer")
        self._mark("previs")
        # ---- 预演：续跑优先复用 checkpoint（2026-11-12）----
        # 以前预演产物不进 checkpoint，续跑要把每镜白模重跑一遍（3~5 秒/镜）。
        # 现在：checkpoint 里有 `previs.firsts` 且文件都还在 -> 直接复用。
        _pv_ck = (getattr(self, "resume_data", None) or {}).get("previs") or {}
        _pv_firsts = dict(_pv_ck.get("firsts") or {})
        _pv_ok = {k: v for k, v in _pv_firsts.items()
                  if str(v) and __import__("pathlib").Path(str(v)).is_file()}
        if _pv_ok:
            from pathlib import Path as _P

            self.previs_first = {k: _P(str(v)) for k, v in _pv_ok.items()}
            previs_info = {"ok": True, "resumed": True,
                           "shots": len(self.previs_first),
                           "reason": "从 checkpoint 复用（未重跑白模）"}
            print(f"  ↩ 预演：从 checkpoint 复用 {len(self.previs_first)} 镜"
                  f"（未重跑 Blender）")
        else:
            previs_info = self._run_previs(picked)

        # 落盘预演产物（供下次续跑复用）
        try:
            from little_crew.runtime import stage_checkpoint as _sc_pv

            _wd_pv = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
            if _wd_pv and getattr(self, "previs_first", None):
                _sc_pv.save(_wd_pv, "previs",
                            {"firsts": {str(k): str(v)
                                        for k, v in self.previs_first.items()},
                             "resolution": list(self.previs_resolution)},
                            meta={"episodeId": self.episode_id,
                                  "shots": len(self.previs_first),
                                  "resumed": bool(previs_info.get("resumed"))})
        except Exception:                                     # noqa: BLE001
            pass
        self._mark("previs_done")

        # ⚠️ **桥接必须在白模预演之后**（2026-10-22 修）。
        #
        # 原来桥接（IR 编译）在预演之前，于是 `_control_frame_for()` 拿到的
        # 永远是 `None` —— **控制图再也不会被编进施工单**。
        # 这不是「后端不支持」，是「控制图还没生成，施工单就编好了」。
        # 表现是崩溃（`_stage_control_image(None)`），真因在顺序。
        self._prompts_seen: dict[str, str] = {}
        # ---- IR：续跑优先复用 checkpoint 里的施工单（2026-11-10）----
        #
        # 为什么不是"重新桥接一遍"：
        # 桥接结果取决于镜头字段（含 `duration_sec`），而续跑时上游任何一处漂移
        # （导演重排、返工换镜、修复改文本）都会让 IR 变 → 缓存全 miss。
        # 真跑实测：checkpoint 里 6.0s，续跑桥接出 4.0s → 0/3 命中。
        #
        # 既然 checkpoint 已经存了**整份 IR**（`render` 阶段的 `irJson`），
        # 续跑就直接用它 —— checkpoint 的语义本来就是"这一步的输入已经定了"。
        _irs_cached = (getattr(self, "resume_data", None) or {}).get("irJson") or {}
        _reused = 0
        # 逐镜复用，**不要求数量相等**（2026-11-11）：
        # 之前写成 `len(cached) == len(picked)`，于是"上次缺 6 镜"这种局面
        # 直接退化成全量桥接（EF27 续跑实测 0/41 命中、实算 41）——
        # 恰恰是最需要复用的场景被这条前置条件挡掉了。
        if _irs_cached:
            irs = []
            for _s_ir in picked:
                _cached = _irs_cached.get(str(getattr(_s_ir, "shot_id", "")))
                if isinstance(_cached, dict) and _cached:
                    irs.append(_cached)
                    _reused += 1
                else:
                    irs.append(self._bridge(_s_ir))
            if _reused:
                print(f"  IR：从 checkpoint 复用 {_reused}/{len(picked)} 镜"
                      f"（其余重新桥接）")
        else:
            irs: list[dict[str, Any]] = [self._bridge(s) for s in picked]

        # ---- IR 指纹：查"续跑为什么还重渲"（2026-11-10）----
        #
        # 缓存键 = IR 内容哈希。续跑若 IR 变了，缓存必 miss。
        # 这里把每镜哈希存进 checkpoint，并在续跑时**对比打印** ——
        # 否则只能看到"命中 0"，看不出是"哪个字段变了"。
        self._compare_ir_hashes(picked, irs)
        _prompts = self._prompts_seen
        if limit is not None:
            irs = irs[:planned]
        # ---- 4) 渲染 ----
        self._mark("render_start")
        # 渲染前健康检查（2026-11-11）：EF27 有 6 镜栽在
        # `channel_unavailable: 无法连接 127.0.0.1:8188`，而集成 20 分钟才发现。
        # 先探一次，探不到就**明确报错**，别让 6 镜在后面才丢。
        self._render_backend_healthcheck()
        rstats = self._render(irs, on_progress=on_progress)
        batch = rstats["batch"]

        # 单镜失败重试一次（只重试"通道不可用"这类瞬时错误）
        batch, _retried = self._retry_channel_failures(batch, irs, rstats)
        if _retried:
            print(f"  ↻ 渲染重试：{_retried} 镜成功（通道瞬时不可用）")
        # 产物落盘（云端 b64 产物在系统临时目录，不落盘下游全找不到）—— 见方法注释
        self._materialize_batch(batch, picked)

        # ---- 4.5) 验收门禁（C3）----
        #
        # **位置很关键：在渲染之后、合成之前。**
        # 放在合成之后的话，"成片清单"会先被判定为完整，
        # 而门禁发现的缺陷要等到更后面才暴露 —— 那时清单已经发出去了。
        gate_report = self._run_gate(batch=batch, picked=picked, irs=irs)

        # ---- 4.6) 返工闭环 ----
        #
        # ```text
        # 门禁判 RETRY -> 有可应用的动作 -> 改 IR -> 只重跑那几镜 -> 复检
        #                ^                                        |
        #                +---------------- 最多 max_repair_rounds --+
        # ```
        #
        # **为什么只重跑那几镜**：整集重跑会浪费已验证的镜头，
        # 而缓存能识别"只有改过的镜头的 IR 变了"（改了 IR 即改了哈希）。
        #
        # **为什么必须改 IR 而不是改图**：改 IR 才进入哈希链，
        # "这张图是怎么来的"才可追溯。而且**不改 IR 的重跑会命中缓存**，
        # 拿回同一张图 —— 那是空转。
        repair_log: list[str] = []
        applied_repairs: list[dict[str, Any]] = []
        if gate_report is not None and self.max_repair_rounds > 0:
            (gate_report, batch, repair_log,
             applied_repairs) = self._repair_loop(
                gate_report=gate_report, batch=batch, irs=irs, picked=picked)

        self._materialize_batch(batch, picked)
        gate_dict = gate_report.as_dict() if gate_report is not None else {}
        if repair_log:
            gate_dict["repairRounds"] = list(repair_log)
        if applied_repairs:
            # 证据里留一份"当初改了什么" —— 最终裁定是 KEEP 时它已经没有动作了
            gate_dict["appliedRepairs"] = list(applied_repairs)

        # ---- 4.7) 收集产物 ----
        #
        # ⚠️ **必须在返工循环之后**。
        # 放在循环之前的话，重跑过的镜头仍然记着**旧**产物名 ——
        # 而旧产物可能已被缓存层的新写入覆盖，或者（更糟）依然存在，
        # 于是清单指向一张**已经被判定不合格的图**。
        # 那类错误不报任何异常，只是"交付的图不是最终那张"。
        artifacts: dict[str, list[str]] = {}
        for o in self._outcomes_of(batch):
            # 产物名以缓存层为准（它保证唯一）
            artifacts[str(getattr(o, "graph_id", ""))] = sorted(
                getattr(o, "artifacts", {}) or {})

        # ---- 5) 合成 ----
        self._run_post_production(picked, engine=str(getattr(self, "engine_name", "") or ""))
        self._mark("assemble")
        ir_hashes: dict[str, str] = {}
        for (s, ir) in zip(picked, irs):
            try:
                from little_crew.ir.canonical import content_hash

                ir_hashes[s.shot_id] = content_hash(ir)
            except Exception:                                 # noqa: BLE001
                ir_hashes[s.shot_id] = ""

        # ---- 产物文件哈希（2026-11-04，§3.10.2 第三层）----
        #
        # 证据里原来只有 `irHashes`（施工单哈希），**没有产物文件哈希** ——
        # 于是"这一集说它渲染了 40 个镜头"只能**信它自述**。
        # 这里补上：逐个文件算 sha256，独立脚本才能重算比对。
        artifact_hashes: dict[str, str] = {}
        try:
            import hashlib as _hl

            _base = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
            if _base:
                _base = Path(str(_base))
                for _names in artifacts.values():
                    for _n in _names:
                        _f = _base / _n
                        if not _f.is_file():
                            _hits = list(_base.rglob(_n))
                            _f = _hits[0] if _hits else None
                        if _f is None or not _f.is_file():
                            continue
                        _h = _hl.sha256()
                        with _f.open("rb") as _fh:
                            for _c in iter(lambda: _fh.read(1 << 20), b""):
                                _h.update(_c)
                        artifact_hashes[_n] = "sha256:" + _h.hexdigest()
        except Exception as _e:                                # noqa: BLE001
            print(f"  ⚠️ 产物文件哈希未生成：{type(_e).__name__}: {_e}")

        per_shot_artifacts = {
            s.shot_id: artifacts.get(f"G-{self.episode_id}-{s.shot_id}", [])
            for s in picked
        }

        # ---- 阶段 checkpoint：渲染（2026-11-08）----
        try:
            from little_crew.runtime import stage_checkpoint as _sc3

            _wd_ck3 = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
            if _wd_ck3:
                _sc3.save(_wd_ck3, "render",
                          {"perShotArtifacts": dict(per_shot_artifacts),
                           "irHashes": dict(getattr(self, "_ir_hashes_now", {}) or {}),
                           #: 整份 IR —— 续跑时能**字段级**解释"为什么哈希不同"，
                           #: 只存哈希只能说"不同"，说不清"哪不同"（2026-11-10）
                           "irJson": {str(s.shot_id): ir
                                      for s, ir in zip(picked, irs)},
                           "artifacts": {k: list(v) for k, v in artifacts.items()}},
                          meta={"episodeId": self.episode_id,
                                "shots": len(per_shot_artifacts)})
        except Exception:                                    # noqa: BLE001
            pass

        self._stage_voice(picked, shots, script_draft, per_shot_artifacts)

        self._stage_vision(picked, irs, per_shot_artifacts, artifacts)
        # 混合渲染（方案 C）：关键镜换云端版；缺锚点就现生（画风一致）
        self._run_cloud_shots(picked, per_shot_artifacts, script_draft)
        # 封面设计师（2026-11-13）：选帧 或 生成（默认选帧）
        self._run_cover(picked, per_shot_artifacts)

        self._stage_final(picked, per_shot_artifacts)

        self._stage_image_review(picked, per_shot_artifacts, artifact_hashes)

        manifest, evidence = self.assembler.assemble(
            episode_id=self.episode_id, title=self.title,
            shots=[s.as_dict() for s in picked],
            artifacts=per_shot_artifacts, ir_hashes=ir_hashes,
            cache_stats={
                "hits": len(batch.cache_hits),
                "rendered": len(batch.rendered),
                "secondsSaved": batch.seconds_saved,
                "secondsSpent": batch.seconds_spent,
            },
            gate=gate_dict, prompts=_prompts)

        if artifact_hashes:
            evidence.artifact_hashes = dict(artifact_hashes)

        # 每镜用到哪些角色锚点图 —— 落成附加产物，便于核对"一致性有没有接线"
        try:
            _wd0 = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
            if _wd0 and getattr(self, "_anchors_used", None):
                import json as _j0

                _o0 = Path(str(_wd0)) / "delivery"
                _o0.mkdir(parents=True, exist_ok=True)
                (_o0 / "anchors_used.json").write_text(_j0.dumps(
                    {"kind": "anchors.used",
                     "table": sorted((getattr(self, "_anchor_table", {}) or {}).keys()),
                     "perShot": dict(sorted(self._anchors_used.items()))},
                    ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:                                    # noqa: BLE001
            pass

        failed = tuple({"graphId": o.graph_id, "failure": o.failure.value,
                        "reason": o.reason[:200]} for o in batch.failed)
        # `ok` 同时看**硬规则**与**门禁**。
        #
        # ⚠️ 门禁结论必须影响 `ok`，否则它只是一个"附在证据里的意见" ——
        # 而 C3 的整个意义就是**让判定能阻断交付**。
        # 只管记录不管拦截的门禁，与"自我签收"没有实质区别。
        gate_ok = gate_report is None or gate_report.all_kept
        ok = (bool(manifest.shot_count) and evidence.all_checks_passed
              and gate_ok)
        if ok:
            reason = "全部镜头就绪"
        elif not gate_ok:
            reason = (f"门禁未过：{gate_report.retry} 镜需返工 / "
                      f"{gate_report.escalate} 镜需人工")
        else:
            reason = f"{len(failed)} 镜失败 / 硬规则未全过"

        # ---- 6) 视频合成（可选）----
        #
        # 把"成片清单"变成"能播放的一集"。失败**不影响前面各步的结论** ——
        # 图已经出了，清单已经产出了，视频只是多一种呈现。
        video = None
        video_path = None
        if self.assemble_video and manifest.shot_count:
            video, video_path = self._assemble_video(
                manifest, per_shot_artifacts)

        # ---- 6.5) 视频模式第二段（真片段，不是静帧 + Ken Burns）----
        #
        # ⚠️ **顺序**：必须在静帧都落盘之后 —— H3 要拿静帧当首帧。
        # ⚠️ **成本**：331s/镜，24 镜 ≈ 2.3 小时。所以 `video_mode` 默认关。
        # ⚠️ **失败不覆盖前一步的结论**：片段没出来，静帧成片仍然有效。
        clip_video = ""
        # ⚠️ **两个都要有默认值**：`video_mode` 默认 False，
        # 不开视频时这两行是唯一的赋值点。缺了它们，
        # 构造 `EpisodeResult` 时传 `self._clip_audio` 会 `AttributeError`
        # （审计报告 H-19 的建议修法正好踩这个）。
        clip_audio: dict[str, Any] = {}
        self._clip_info: dict[str, Any] = {}
        self._clip_audio: dict[str, Any] = {}
        if self.video_mode and manifest.shot_count:
            clips = self._render_clip_stage(per_shot_artifacts,
                                            on_progress=on_progress)
            clip_video = str(clips.get("path") or "")
            # **记下来**：不记的话"片段没出来"与"没开视频模式"分不清。
            self._clip_info = dict(clips)
            # ⚠️ **缺音轨 -> 交付失败**（M3 的定义是"成片含音轨"）
            self._clip_audio = dict(clips.get("audio") or {})
            clip_audio = dict(self._clip_audio)
            if clips.get("path") and self._clip_audio.get("ok") is False:
                ok = False
                reason = str(self._clip_audio.get("reason") or "片段缺音轨")

        # ---- 人工 Gate（2026-11-08）----
        #
        # 机器只判"有没有/崩没崩"，判不了"能不能给人看"。
        # 这一屏是**给人看的**，不是给机器看的：
        # 人点认可 -> humanReviewed=True；驳回 -> 本集不算通过（assisted=True）；
        # 没问人（关掉/非交互）-> skip，**绝不等于通过**。
        ok, reason, _hg = self._run_human_gate(ok, reason, manifest=manifest, failed=failed)
        self._human_gate = _hg
        try:
            import json as _jh2

            _wd_hg = (getattr(self, "_workdir", None)
                      or getattr(self, "workdir", None))
            if _wd_hg:
                (_ohg := Path(str(_wd_hg)) / "delivery").mkdir(
                    parents=True, exist_ok=True)
                (_ohg / "human_gate.json").write_text(
                    _jh2.dumps(_hg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:                                    # noqa: BLE001
            pass

        return EpisodeResult(
            episode_id=self.episode_id, ok=ok,
            script_rounds=cycle.round_count,
            script_verdict=(cycle.rounds[-1].verdict.value
                            if cycle.rounds else ""),
                  # ⚠️ **出图失败优先于剧本分类**（2026-10-17 实测）：
                  # 24 镜全 500 的那次，报告写的是 `below-threshold`（剧本分数不够）
                  # —— 而真正卡住交付的是**渲染服务挂了**。两者该做的事完全不同。
                  #
                  # 判据：只要有镜头渲染失败，**下一个动作就是查出图**，
                  # 所以它压过剧本分类。剧本的裁定仍在 `script_verdict` 里，
                  # 信息没丢，只是 `failure_kind` 回答的是"下一步做什么"。
                  failure_kind=("render-failed" if failed
                                else cycle.failure_kind),
            shot_count=manifest.shot_count, planned_shots=planned,
            rendered=len(batch.rendered), cache_hits=len(batch.cache_hits),
            failed=len(failed), total_seconds=manifest.total_seconds,
            manifest=manifest, evidence=evidence, reason=reason,
            failures=failed, video=video, video_path=video_path,
            # ⚠️ **这两个也漏过一次**（2026-10-23 审计 H-19）：
            # H3 片段渲了 2.3 小时、音轨也查了，但构造 `EpisodeResult` 时没传，
            # 报告里 `clipVideo`/`clipAudio` 恒为空 ——
            # 与"没开视频模式"完全一样，而 `:687` 上方那条注释
            # 记的正是同一个坑（`previs`）。**同一个函数，第二次。**
            clip_video=clip_video,
            clip_audio=clip_audio,
            human_gate=_hg,
            characters=self.characters,
            advisory=advisory,
            director=director_info,
            gaffer=lighting_info,
            # ⚠️ 这三个**漏过一次**（2026-10-19）：白模渲了 40 张控制图，
            # 但报告里 `previs` 是空的 —— 因为构造 EpisodeResult 时没传。
            # 表现是「做了但看不见」，与「没做」在报告上完全一样。
            art=art_info,
            schedule=sched_info,
            previs=previs_info,
            wall_seconds=time.perf_counter() - t0)

    # ---- 视频合成 ------------------------------------------------------

    #: 是否在跑完一集后自动合成视频。**默认开启** —— 关掉它没有好处。
    assemble_video: bool = True

    #: 产物存放根目录（决定成片落在哪）。由调用方在构造后设置。
    workdir: Path | None = None

    #: 合成产物的子目录名
    video_subdir: str = "delivery"

    #: 成片文件名
    video_filename: str = "episode.master.mp4"

    def _repair_subjects(self, picked, shots, script_draft):
        """把分镜里「剧本未授权」的场景词换成本场授权词（2026-11-10 抽出）。

        返回 `(picked, shots)`；并在修复后复算越权，否则门禁仍按旧文本否决。
        抽出来的意义：这一段原来躺在 1500 行的 run() 里，改它要读全流程。
        """
        # ---- 场景主体修复：把"剧本没授权"的场景词换成本场授权词（2026-11-10）----
        #
        # 白名单提示词降低了概率但没根除（EF9 仍有 3 镜写"花田"→ 否决交付）。
        # 这里做**确定性修复**并逐条记录 —— 修不掉就留给门禁，不瞎猜。
        try:
            from little_crew.review.scene_subject import subjects_in as _subj_fix
            from little_crew.runtime import subject_repair as _srep

            _allow_by_scene: dict[str, list[str]] = {}
            for _sc_f in (getattr(script_draft, "scenes", ()) or ()):
                _sid_f = str(_sc_f.get("scene_id") or _sc_f.get("id") or "")
                _allow_by_scene[_sid_f] = sorted(_subj_fix(_sc_f))
            _allow_all = sorted({w for v in _allow_by_scene.values() for w in v})
            _plan_fix = _srep.plan(picked, _allow_all, by_scene=_allow_by_scene)
            # 无论有没有替换，都打印 + 复算 —— 否则"0 处"和"没跑"看起来一样
            # （EF17 就是没有任何输出，无法判断是没检出还是没执行）。
            if not any(x.get("applied") for x in _plan_fix):
                print("  场景主体修复：0 处（未检出越权词）")
            if any(x.get("applied") for x in _plan_fix):
                picked, _done_fix = _srep.apply(picked, _plan_fix)
                shots = tuple(picked)
                print(f"  场景主体修复：{len(_done_fix)} 处"
                      + (f"（如 {_done_fix[0]['old']}→{_done_fix[0]['new']}）" if _done_fix else ""))
                _wd_fix = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
                if _wd_fix:
                    try:
                        import json as _j_fix

                        _out_fix = Path(str(_wd_fix)) / "delivery"
                        _out_fix.mkdir(parents=True, exist_ok=True)
                        (_out_fix / "subject_repairs.json").write_text(
                            _j_fix.dumps({"kind": "subject.repairs",
                                          "count": len(_done_fix),
                                          "repairs": _done_fix},
                                         ensure_ascii=False, indent=2),
                            encoding="utf-8")
                    except Exception:                        # noqa: BLE001
                        pass
        except Exception as _e_fix:                          # noqa: BLE001
            print(f"  ⚠️ 场景主体修复未运行：{type(_e_fix).__name__}: {_e_fix}")

        # ---- 修复后**复算**越权（2026-11-10）----
        #
        # 越权判定在上面（2.8）已跑过，而**返工与主体修复都在它之后** ——
        # 不复算的话，门禁仍按旧文本否决整集：
        #   · EF10：修复已经改了词，门禁还按旧文本否
        #   · EF20：返工换掉了那镜（新文本里根本没有"花田"），
        #           `scene_veto` 仍停在 2.8 的 True -> 白否一集
        # 所以复算**无条件执行**（不依赖"有没有替换"）。
        try:
            from little_crew.review.scene_subject import (
                check_scene_subject as _csc2,
            )

            _text2 = str(script_draft or "")
            _conf2 = []
            for _s2 in picked:
                _r2 = _csc2(_text2, str(getattr(_s2, "description", "") or ""))
                if _r2.get("verdict") == "RETRY":
                    _conf2.append({
                        "shotId": str(getattr(_s2, "shot_id", "")),
                        "severity": "error",
                        "reason": str(_r2.get("reason") or "")[:120],
                    })
            self.scene_conflicts = _conf2
            # ⚠️ CLI 的门禁看的是 `scene_veto`（在 2.8 就置 True 了），
            # 只改 conflicts 不清这个标志 → 修好了照样否决（EF18 真跑踩到）。
            self.scene_veto = bool(_conf2)
            print(f"  修复后复算越权：{len(_conf2)} 镜")
        except Exception as _e_re:                   # noqa: BLE001
            print(f"  ⚠️ 修复后复算未运行：{type(_e_re).__name__}: {_e_re}")

        # ---- 计划镜数必须以"最终镜头"为准（2026-11-10）----
        #
        # `planned` 原来在**返工/主体修复之前**算：那会儿 35 镜，
        # 修完只剩 33 镜 -> 覆盖率 33/35=0.943 -> 门禁判"未完全跑通"。
        # 分母比分子大，不是"少交了 2 镜"，是**分母过期**。
        planned = len(picked)
        self.planned_shots = planned

        return picked, shots


    def _stage_music(self, picked, out_dir, ffmpeg):
        """决定用哪条音乐床（2026-11-10 抽出）。

        优先 composer 职位产物，失败退回内联；返回 `(bed, film_seconds)`。
        这段原来在 run() 里缩进 24 层，光对齐就能改错。
        """
        from little_crew.runtime import music_stage as _ms

        bed = _ms.resolve_bed(orch=self.orch, shots=picked, out_dir=out_dir,
                              ffmpeg=ffmpeg or "ffmpeg")
        film_s = float(bed.get("requestedSeconds") or _ms.film_seconds(picked))
        if bed.get("via") == "composer":
            print(f"  配乐（composer）："
                  f"{float(bed.get('seconds') or 0):.1f}s"
                  f" · {bed.get('source') or ''}")
        elif bed.get("note"):
            print(f"  ⚠️ 音乐床：{bed['note']}")
        return bed, film_s


    def _stage_final(self, picked, per_shot_artifacts):
        """成片：静帧 + 音轨 + 字幕 -> mp4（2026-11-10 抽出）。

        原块整体搬入（未改一行缩进）：它本来就在 `run()` 的 8 空格层级，
        正好是方法体层级 —— 这也是上次抽视觉闸失败的原因（我多此一举重排了缩进）。
        """
        if getattr(self, "final_enabled", False):
            try:
                import json as _jf

                from little_crew.runtime import finalize as _fz

                _wd2 = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
                _imgs, _durs = [], []
                for _s in picked:
                    _nm = (per_shot_artifacts.get(_s.shot_id) or [""])[0]
                    _hit = list(Path(str(_wd2)).rglob(_nm)) if (_wd2 and _nm) else []
                    if _hit:
                        _imgs.append(str(_hit[0]))
                    _durs.append(float(getattr(_s, "duration_sec", 0) or 0))
                _mixp = (Path(str(_wd2)) / "delivery" / "mix.wav") if _wd2 else None
                _srtp = (Path(str(_wd2)) / "delivery" / "subtitles.srt") if _wd2 else None
                _fr = _fz.build(
                    _imgs, picked,
                    str(Path(str(_wd2)) / "delivery" / "episode.mp4"),
                    audio=str(_mixp) if (_mixp and _mixp.is_file()) else "",
                    srt=str(_srtp) if (_srtp and _srtp.is_file()) else "",
                    ffmpeg=getattr(self, "ffmpeg", "") or "ffmpeg",
                    workdir=str(Path(str(_wd2)) / "build"))
                if _wd2:
                    (Path(str(_wd2)) / "delivery" / "final.json").write_text(
                        _jf.dumps(_fr, ensure_ascii=False, indent=2), encoding="utf-8")
                if _fr["ok"]:
                    print(f"  成片：{_fr['videoSeconds']:.1f}s · 字幕 {_fr['subtitles']}"
                          f" · {_fr['size']/1024/1024:.1f} MB"
                          + ("· 音轨补静音 %.1fs" % _fr["audioPadded"]
                             if _fr.get("audioPadded", 0) > 0.05 else ""))
                else:
                    print(f"  ⚠️ 成片失败：{_fr['error']}")
            except Exception as _e_f:                      # noqa: BLE001
                print(f"  ⚠️ 成片未运行：{type(_e_f).__name__}: {_e_f}")


    def _stage_voice(self, picked, shots, script_draft, per_shot_artifacts):
        """配音 + 配乐 + 混流（2026-11-10 抽出）。

        原块整体搬入（含：释放 ComfyUI 显存 → 音色 → 对白编辑 → 配音轨 →
        音乐床 → 混流 + 字幕），**未改一行缩进** —— 它本来就是 `run()` 的
        8 空格层级，正好是方法体层级。
        """
        # ⚠️ 工作目录必须在方法内自取（2026-11-10）：
        # 这段原来在 run() 里，`_wd` 是上游先定义的；抽出方法后它就不在作用域了 ——
        # 真跑报 `UnboundLocalError: cannot access local variable '_wd'`，
        # 而**音色只剩"造音色未运行"一行提示**，配音照样能跑（用已有音色），
        # 单测全绿也发现不了。同类坑今晚第二次（上次是 `_wdv`）。
        _wd = getattr(self, "_workdir", None) or getattr(self, "workdir", None)

        # 配音前探活（2026-11-11）：EF26/EF28 各有 1~2 句失败，
        # 事后**手动重调同一句就成功** —— 是 TTS 后端瞬时不可用。
        # 探一次，不可用就等 10 秒再试（仍失败就照常跑，失败会落进 rows）。
        if getattr(self, "voice_enabled", False):
            try:
                import time as _t_v
                import urllib.request as _ur_v
                from little_crew.runtime import tts_breeze as _tts_v

                _ep_v = _tts_v.server_endpoint().rstrip("/") + "/health"
                _ok_v = False
                for _try_v in range(1, 3):
                    try:
                        with _ur_v.urlopen(_ep_v, timeout=5) as _r_v:
                            _ok_v = getattr(_r_v, "status", 0) == 200
                    except Exception:                        # noqa: BLE001
                        _ok_v = False
                    if _ok_v:
                        break
                    print(f"  ⏳ TTS 探活失败（第 {_try_v} 次），等 5 秒…")
                    _t_v.sleep(5)
                if not _ok_v:
                    print("  ⚠️ TTS 后端不可用 —— 配音很可能出现失败句")
                else:
                    # **暖机一句**（2026-11-11）：EF26/EF28/EF30 失败的都是
                    # S01 的第一句，且重试 3 次都失败 —— 这是 TTS **冷启动**
                    # 把首次合成就吞了（模型加载）。先拿一句丢弃的试音把
                    # 冷启动吃掉，别让第一句真实台词承担它。
                    try:
                        import tempfile as _tf_v

                        _warm = _tf_v.NamedTemporaryFile(
                            suffix=".wav", delete=False).name
                        for _w_try in range(1, 4):
                            _wr = _tts_v.synth("试音。", _warm, voice="girl")
                            if _wr.get("ok") and pathlib.Path(_warm).is_file():
                                print(f"  🔥 TTS 暖机完成（第 {_w_try} 次）")
                                break
                            print(f"  ⏳ TTS 暖机失败（第 {_w_try} 次），等 5 秒…")
                            _t_v.sleep(5)
                        try:
                            pathlib.Path(_warm).unlink(missing_ok=True)
                        except Exception:                    # noqa: BLE001
                            pass
                    except Exception as _e_warm:             # noqa: BLE001
                        print(f"  ⚠️ TTS 暖机未运行：{type(_e_warm).__name__}: {_e_warm}")
            except Exception:                                # noqa: BLE001
                pass

        # ---- 配音轨（§3.11，2026-11-06）----
        #
        # 只在 `--voice` 打开时跑：一句台词约 3~4 秒（本机 breeze），
        # 默认关闭以免拖慢普通验证。
        # ---- 配音前释放 ComfyUI 显存（2026-11-09）----
        #
        # 实测：ComfyUI 渲完仍占 4~6GB；此时音频后端会回落/抖动 ——
        # TTS server 实测从 0.9s/句 退化到 65~90s/句。渲染已结束，显存该还。
        try:
            import os as _os_vr
            import urllib.request as _ur_vr

            if (_os_vr.environ.get("LITTLE_CREW_FREE_VRAM") or "1") != "0":
                _req = _ur_vr.Request(
                    "http://127.0.0.1:8188/free",
                    data=b'{"unload_models": true, "free_memory": true}',
                    headers={"Content-Type": "application/json"},
                    method="POST")
                with _ur_vr.urlopen(_req, timeout=8) as _r_vr:
                    if _r_vr.status == 200:
                        print("  已释放 ComfyUI 显存（给配音腾地方）")
        except Exception:                                     # noqa: BLE001
            pass                                              # 释放失败不影响配音

        if getattr(self, "voice_enabled", False):
            try:
                import json as _jv

                from little_crew.governance.voice_cast import cast as _cast
                from little_crew.runtime import tts_breeze as _tts
                from little_crew.runtime import voice_track as _vt

                _chars = sorted({str(c) for sc in (script_draft.scenes or ())
                                 for c in (sc.get("characters") or ()) if str(c)})
                _sheets = {}
                for _c in _chars:
                    _sp = Path("assets") / "characters" / _c / "sheet.json"
                    if _sp.is_file():
                        try:
                            _sheets[_c] = _jv.loads(_sp.read_text(encoding="utf-8"))
                        except Exception:                  # noqa: BLE001
                            _sheets[_c] = {}
                _have = _tts.list_voices()
                # ---- 缺音色就**自己造**（2026-11-08）----
                # 分工：设定来自角色表（LLM 可覆盖），物化交给 TTS 的试音。
                # 不再需要人给参考音。
                if getattr(self, "forge_voices", True):
                    try:
                        from little_crew.runtime import voice_forge as _vf
                        from little_crew.governance.voice_design import (
                            from_sheet as _vfs, slug as _vslug)

                        _need = [c for c in _chars
                                 if _vslug(c) not in set(_have)
                                 and c not in set(_have)]
                        if _need:
                            _specs = {c: _vfs(_sheets.get(c) or {}) for c in _need}
                            _fr = _vf.forge_all(
                                _specs, synth=_tts.synth,
                                workdir=str(Path(str(_wd)) / "delivery" / "voices")
                                if _wd else None)
                            if _fr["count"]:
                                print(f"  已造音色 {_fr['count']} 个"
                                      f"（{', '.join(x['name'] for x in _fr['forged'])}）")
                            if _fr["failed"]:
                                print(f"  ⚠️ 音色失败 {len(_fr['failed'])} 个")
                            _have = _tts.list_voices()
                    except Exception as _e_fg:                # noqa: BLE001
                        print(f"  ⚠️ 造音色未运行：{type(_e_fg).__name__}: {_e_fg}")
                _cast_map = _cast(_chars, _sheets, _have)
                # ---- 对白编辑（`dialogue_editor`）—— 2026-11-08 首次接上 ----
                #
                # 名册早就声明 `script.final -> shot.voice`，但从未被调用，
                # 配音是在这里**内联算**的：产物不可审、策略不可换。
                # 现在优先用职位的产物；没绑定时退回内联（行为不变）。
                _voice_plan = None
                try:
                    _impls_v = getattr(self.orch, "_impls", {}) or {}
                    if "dialogue_editor" in _impls_v:
                        _jv_out = self.orch.run_stage(
                            "dialogue_editor", "shot.voice",
                            {"script.final": script_draft})
                        if getattr(_jv_out, "ok", False):
                            _voice_plan = self.orch.blackboard.value("shot.voice")
                            if _voice_plan is not None:
                                print(f"  对白编辑：{_voice_plan.get('lineCount', 0)} 句 · "
                                      f"计划 {_voice_plan.get('totalSeconds', 0):.1f}s")
                        else:
                            print(f"  ⚠️ 对白编辑未产出：{_jv_out.error}")
                except Exception as _e_de:                     # noqa: BLE001
                    print(f"  ⚠️ 对白编辑未运行：{type(_e_de).__name__}: {_e_de}")

                # 台词来源**优先结构化字段** `dialogue`（2026-11-06 加），
                # 没有才退回正则抽正文 —— 并在每条上记 `source`，便于统计。
                _lines = []
                for _sc in (script_draft.scenes or ()):
                    _sid = str(_sc.get("scene_id") or _sc.get("id") or "")
                    _known = {str(c) for c in (_sc.get("characters") or ()) if str(c)}
                    _dia = _sc.get("dialogue") or []
                    if _dia:
                        for _d in _dia:
                            _lines.append({"character": str(_d.get("character") or ""),
                                           "text": str(_d.get("line") or ""),
                                           "sceneId": _sid, "source": "dialogue"})
                    else:
                        for _d in _vt.parse_dialogue(str(_sc.get("text") or ""),
                                                     known=_known or None):
                            _lines.append({**_d, "sceneId": _sid, "source": "regex"})
                if _voice_plan and _voice_plan.get("lines"):
                    # 用职位的产物替换内联结果：句序、情绪、估时都来自 `shot.voice`
                    _lines = [{"character": str(x.get("character") or ""),
                               "text": str(x.get("text") or ""),
                               "sceneId": str(x.get("sceneId") or ""),
                               "emotion": str(x.get("emotion") or ""),
                               "plannedSeconds": float(x.get("estimateSeconds") or 0),
                               "source": "dialogue_editor"}
                              for x in _voice_plan["lines"]]
                _wd = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
                _vdir = (Path(str(_wd)) / "delivery" / "voice") if _wd else None
                # 全部合成（0 = 不截断）：一集 30+ 句也就多花 ~2 分钟，
                # 但成片不会出现"大半是静音"（163s 里 145s 静音那次）。
                _trk = _vt.build(_lines, shots, synth=_tts.synth,
                                 cast_map=_cast_map, out_dir=_vdir,
                                 max_lines=0)
                if _wd:
                    _vo = Path(str(_wd)) / "delivery"
                    _vo.mkdir(parents=True, exist_ok=True)
                    (_vo / "voice_used.json").write_text(
                        _jv.dumps(_trk, ensure_ascii=False, indent=2),
                        encoding="utf-8")
                # 诊断：抽不到台词时，把"角色名单 + 正文样例"打出来 ——
                # 否则只能看到"0 句"，不知道为什么（2026-11-06 踩过）。
                if not _lines and getattr(self, "voice_debug", False):
                    print(f"  [配音诊断] 场数 {len(script_draft.scenes or ())}"
                          f" · 角色名单 {_chars}")
                    for _sc in (script_draft.scenes or ())[:2]:
                        print(f"    {_sc.get('scene_id')} chars={_sc.get('characters')}"
                              f" text={str(_sc.get('text') or '')[:80]!r}")
                # ---- 混流：字幕 + 环境音（BGM 无素材就明确跳过）----
                try:
                    from little_crew.runtime import mixdown as _mx

                    # **按镜头起点排台词**（2026-11-08）：不这样做的话 24 句会被
                    # 摞成一条连续旁白带 —— 画面在切镜、声音不跟着切，
                    # 表现就是「字幕对不上 + 镜头衔接怪 + 语气生硬」。
                    _offsets, _acc = {}, 0.0
                    for _s3 in picked:
                        _offsets[str(_s3.shot_id)] = round(_acc, 3)
                        _acc += float(getattr(_s3, "duration_sec", 0) or 0)
                    _rows = _mx.plan(_trk["utterances"], shot_offsets=_offsets)
                    if _wd and _rows:
                        _sub = Path(str(_wd)) / "delivery"
                        (_sub / "subtitles.srt").write_text(
                            _mx.srt(_rows), encoding="utf-8")
                        _bed, _film_s = self._stage_music(
                            picked, _sub,
                            getattr(self, "ffmpeg", "") or "ffmpeg")
                        # 响度归一（EBU R128）：默认开，`LITTLE_CREW_LOUDNORM=0` 可关。
                        # 混流后统一到 -16 LUFS —— 峰值只管"最响那一下"，
                        # 听感一致要靠积分响度。
                        import os as _os_ln

                        _ln_on = (_os_ln.environ.get("LITTLE_CREW_LOUDNORM") or "1") != "0"
                        _mix = _mx.build(
                            _rows, str(_sub / "mix.wav"),
                            ffmpeg=getattr(self, "ffmpeg", "") or "ffmpeg",
                            ambient=True,
                            bgm=str(_bed.get("path") or ""),
                            total_seconds=_film_s,
                            loudnorm=_ln_on)
                        _mix["bgmSource"] = str(_bed.get("source") or "")
                        (_sub / "mix.json").write_text(
                            _jv.dumps({k: v for k, v in _mix.items()
                                       if k != "command"},
                                      ensure_ascii=False, indent=2),
                            encoding="utf-8")
                        if _mix["ok"]:
                            print(f"  混流：{_mix['seconds']:.1f}s · "
                                  f"峰值 {_mix['peakDb']:.1f} dBFS · "
                                  f"字幕 {len(_rows)} 条"
                                  + (f" · 响度 {_mix.get('lufs', 0):.1f} LUFS"
                                     f"（目标 {_mix.get('targetLufs', 0):.0f}）"
                                     if _mix.get("loudnessNormalized") else "")
                                  + ("· ⚠️ 削波" if _mix.get("clipped") else ""))
                        else:
                            print(f"  ⚠️ 混流失败：{_mix['error']}")
                except Exception as _e_mx:                # noqa: BLE001
                    print(f"  ⚠️ 混流未运行：{type(_e_mx).__name__}: {_e_mx}")
                _rep = _trk["report"]
                print(f"  配音：{len(_trk['utterances'])} 句"
                      f"（台词抽出 {len(_lines)} · 丢弃 {len(_trk['dropped'])}）"
                      f" · 失败 {_rep['failed']} · 对齐均差 {_rep['align']['meanAbs']}s")
            except Exception as _e_v:                          # noqa: BLE001
                print(f"  ⚠️ 配音未运行：{type(_e_v).__name__}: {_e_v}")


    def _stage_vision(self, picked, irs, per_shot_artifacts, artifacts):
        """视觉闸 + 定向重渲（2026-11-10 抽出，**顺序已修正**）。

        原块整体搬入（8 空格层 → 方法体层，未改缩进）。
        `per_shot_artifacts` / `artifacts` 以**原地修改**方式回传（dict 可变），
        所以调用方不用重新赋值 —— 这样即使搬出去也不会漏掉重渲后的新图。
        """
        # ---- 视觉闸：真的看图判「崩没崩」（2026-11-08）----
        #
        # 为什么加这道：可数审片只看存在/尺寸/串镜，**抓不到"脸崩了"**。
        # 真跑标定（EV4，22 镜）：faces=broken **2 镜**、低一致度 8 镜 ——
        # 这 2 镜以前会直接进成片。
        #
        # 纪律：**未审 != 通过**（模型不给 JSON / 调用失败都记 reviewed=False）。
        if getattr(self, "vision_review_enabled", False):
            try:
                import json as _jv2

                # ⚠️ 工作目录必须在这里取：前面几段各自用过 `_wd`/`_wd_rb`，
                # 但它们**出了作用域就不存在** —— 真跑踩到 `NameError: _wdv`
                # （2026-11-08，视觉闸因此整段没跑，而图还是那张崩的）。
                _wdv = getattr(self, "_workdir", None) or getattr(self, "workdir", None)

                from little_crew.agent.llm import LlmClient as _LC
                from little_crew.review import vision_review as _vr

                _imgs = {}
                for _s4 in picked:
                    _nm = (per_shot_artifacts.get(_s4.shot_id) or [""])[0]
                    _hit = list(Path(str(_wdv)).rglob(_nm)) if (_wdv and _nm) else []
                    if _hit:
                        _imgs[_s4.shot_id] = str(_hit[0])
                _vrres = None
                # ---- 优先走**职位**（2026-11-12）----
                # 以前这里直接 `new LlmClient()` 调模型：24 次调用**不进预算账本、
                # 不进调用树、没有跨族检查**。现在先问编排器有没有 `qc_inspector`；
                # 绑了就按职位调（自动进账/进 span），没绑才退回直调。
                try:
                    if "qc_inspector" in (getattr(self.orch, "_impls", {}) or {}):
                        _qc_payload = {
                            "shot.image": dict(_imgs),
                            "descriptions": {str(getattr(_s9, "shot_id", "")):
                                             str(getattr(_s9, "description", ""))
                                             for _s9 in picked},
                        }
                        _qc_out = self.orch.run_stage("qc_inspector", "shot.qc",
                                                      _qc_payload)
                        if getattr(_qc_out, "ok", False):
                            _rep = self.orch.blackboard.value("qc.report")
                            if isinstance(_rep, dict):
                                _vrres = _rep
                            else:
                                _res_o = getattr(_qc_out, "result", None)
                                _vrres = ((getattr(_res_o, "output", None) or {})
                                          .get("qc.report"))
                        if _vrres is None:
                            print("  ⚠️ 质检员未产出 qc.report，退回直调视觉闸")
                except Exception as _e_qc:                        # noqa: BLE001
                    print(f"  ⚠️ 质检员调用失败，退回直调：{type(_e_qc).__name__}: {_e_qc}")
                    _vrres = None
                if _vrres is None:
                    _vrres = _vr.review(
                        picked, _imgs,
                        # 视觉调用单次 45s（默认 120s）：12 镜 × 2 次尝试最坏拖半小时
                        # （EF29 实测视觉闸单独占 13 分钟）。构造器传，chat_image 没有该参数。
                        client=_LC(getattr(self, "vision_model", "") or "deepseek-flash",
                                   timeout_s=45),
                        max_shots=int(getattr(self, "vision_max_shots", 12)))
                if _wdv:
                    (_outv := Path(str(_wdv)) / "delivery").mkdir(
                        parents=True, exist_ok=True)
                    (_outv / "vision_review.json").write_text(
                        _jv2.dumps(_vrres, ensure_ascii=False, indent=2),
                        encoding="utf-8")
                    # 质检员的产物也落盘（2026-11-12）：职位产物只进黑板的话，
                    # 跑完就没了 —— 报告/审计/复现都拿不到。
                    if isinstance(_vrres, dict) and _vrres.get("kind") == "qc.report":
                        (_outv / "qc.report.json").write_text(
                            _jv2.dumps(_vrres, ensure_ascii=False, indent=2),
                            encoding="utf-8")
                _bad = sum(1 for f in _vrres["findings"] if f["severity"] == "error")
                if not _vrres.get("reviewedCount"):
                    # ⚠️ 0 镜可判 != 没有崩镜（2026-11-11 EF27）：
                    # 全 47 镜的视觉调用都失败，终端却打印"严重 0 条" ——
                    # 读起来像"审过了、没问题"。必须显式说"等于没审"。
                    #
                    # ⚠️ 这段**本身不许抛**：EF29 就因为我用 `len(errors)` 处理
                    # 一个 int，把整段视觉闸炸成"未运行" —— 诚实性代码不能成为新的失败点。
                    self.vision_not_judged = True
                    try:
                        _n_err = int(_vrres.get("errors") or 0)
                    except Exception:                        # noqa: BLE001
                        _n_err = 0
                    print(f"  ⚠️ 视觉闸：0/{_vrres.get('total', len(picked))} 镜可判"
                          f"（{_n_err} 镜调用失败）"
                          f" —— **等于没审**，不能记作「没有崩图」")
                else:
                    print(f"  视觉闸：审 {_vrres['reviewedCount']}/{_vrres['total']} 镜"
                          f" · 严重 {_bad} 条 · 提示 {len(_vrres['findings']) - _bad} 条")
                self._vision_review = _vrres
            except Exception as _e_vr:                          # noqa: BLE001
                print(f"  ⚠️ 视觉闸未运行：{type(_e_vr).__name__}: {_e_vr}")

        # ---- 视觉闸 -> 定向重渲（2026-11-10 顺序修正）----
        #
        # 原来重渲写在视觉闸**之前**，findings 永远到不了它 ——
        # EF8 有一镜 faces-broken 判出却没重画。现在紧跟在视觉闸之后。
        #
        # 视觉闸（4.6x）能判「这一镜脸崩了」，但判完必须有人动 ——
        # 否则 finding 只是落盘，成片里还是那张崩的图。
        #
        # 机制不新造：编译器按 `provenance.seedDerivation` 派生种子，
        # 其中 `attempt` 进内容哈希 -> 把 attempt+1 就等于"同镜换一次尝试"，
        # 缓存必然 miss，图会被**真正重画**，旧图仍留在缓存里可追溯。
        self._vision_rework = {"kind": "vision.rework", "rerendered": [],
                               "failed": [], "skipped": [], "verified": []}
        try:
            _wd_vr = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
            _vres = getattr(self, "_vision_review", None) or {}
            _errs = [f for f in (_vres.get("findings") or [])
                     if f.get("severity") == "error"]
            if _errs and getattr(self, "vision_rework_enabled", True):
                from little_crew.runtime import vision_rework as _vrw

                _targets = _vrw.plan(_errs, max_shots=int(
                    getattr(self, "vision_rework_max_shots", 4)))
                _ir_by_shot = {}
                for _s5, _ir5 in zip(picked, irs):
                    _ir_by_shot[str(_s5.shot_id)] = _ir5
                _log = {"kind": "vision.rework", "rerendered": [], "failed": [],
                        "skipped": [], "verified": []}
                for _tg in _targets:
                    _sid = str(_tg["shotId"])
                    _ir = _ir_by_shot.get(_sid)
                    if _ir is None:
                        _log["skipped"].append({"shotId": _sid,
                                                "reason": "没有这镜的 IR"})
                        continue
                    _ir2, _att = _vrw.bump_attempt(_ir)
                    try:
                        _rb2 = self._render([_ir2]) or {}
                        _outs = getattr(_rb2.get("batch"), "outcomes", ()) or ()
                        _names = sorted(getattr(_outs[0], "artifacts", {}) or {}) \
                            if _outs else []
                    except Exception as _e_r2:                   # noqa: BLE001
                        _log["failed"].append({"shotId": _sid, "attempt": _att,
                                               "error": f"{type(_e_r2).__name__}: {_e_r2}"})
                        continue
                    if not _names:
                        _log["failed"].append({"shotId": _sid, "attempt": _att,
                                               "error": "重画没有产物"})
                        continue
                    # **替换**原产物引用，让成片/证据链都用新图
                    per_shot_artifacts[_sid] = _names
                    artifacts[str(_ir2.get("graphId", ""))] = _names
                    _log["rerendered"].append({"shotId": _sid, "attempt": _att,
                                               "artifacts": _names,
                                               "kind": _tg.get("kind", ""),
                                               "reason": _tg.get("reason", "")})
                    print(f"  ↻ 重画 {_sid}（attempt {_att}）")
                # ---- 重画后**复验**（2026-11-12）----
                # 缺这一步时 `verified` 永远是空：重画可能还是崩的，但没人知道。
                # 只复验被重画的那几镜（最多 4 镜，成本可控）；复验失败**不阻断**。
                try:
                    _re_shots = []
                    _re_imgs = {}
                    for _rr in _log["rerendered"]:
                        _sid_r = str(_rr.get("shotId") or "")
                        _names_r = _rr.get("artifacts") or []
                        if not _sid_r or not _names_r:
                            continue
                        _hit_r = list(Path(str(_wd_vr)).rglob(_names_r[0])) if _wd_vr else []
                        if not _hit_r:
                            continue
                        _re_imgs[_sid_r] = str(_hit_r[0])
                        _re_shots.append(type("_S", (), {
                            "shot_id": _sid_r,
                            "description": next((str(getattr(s, "description", "") or "")
                                                 for s in picked
                                                 if getattr(s, "shot_id", "") == _sid_r), "")})())
                    if _re_images := _re_imgs:
                        from little_crew.agent.llm import LlmClient as _LC2

                        _rv = _vr.review(_re_shots, _re_images,
                                         client=_LC2(getattr(self, "vision_model", "")
                                                     or "deepseek-flash", timeout_s=45),
                                         max_shots=len(_re_imgs))
                        _still_bad = {str(f.get("shotId")) for f in (_rv.get("findings") or ())
                                      if f.get("severity") == "error"}
                        for _rr in _log["rerendered"]:
                            _sid_r = str(_rr.get("shotId") or "")
                            _log["verified"].append({
                                "shotId": _sid_r,
                                "ok": bool(_rv.get("reviewedCount")) and _sid_r not in _still_bad,
                                "reviewed": int(_rv.get("reviewedCount") or 0),
                                "stillBad": _sid_r in _still_bad,
                            })
                        _ok_n = sum(1 for v in _log["verified"] if v["ok"])
                        print(f"  ✓ 重画复验：{_ok_n}/{len(_log['verified'])} 镜通过"
                              + ("" if _ok_n == len(_log["verified"])
                                 else "（仍崩的镜已记录，不再自动重画）"))
                except Exception as _e_rv:                          # noqa: BLE001
                    print(f"  ⚠️ 重画复验未运行：{type(_e_rv).__name__}: {_e_rv}")

                self._vision_rework = _log
                if _wd_vr and _log["rerendered"]:
                    try:
                        (_ovr := Path(str(_wd_vr)) / "delivery").mkdir(
                            parents=True, exist_ok=True)
                        (_ovr / "vision_rework.json").write_text(
                            __import__("json").dumps(_log, ensure_ascii=False, indent=2),
                            encoding="utf-8")
                    except Exception:                        # noqa: BLE001
                        pass
        except Exception as _e_vrw:                          # noqa: BLE001
            print(f"  ⚠️ 视觉重渲未运行：{type(_e_vrw).__name__}: {_e_vrw}")

    def _run_cover(self, picked, per_shot_artifacts):
        """封面设计师：从成片选帧，或用角色/美术设定生成新封面（2026-11-13）。

        **默认关**（选帧 1 次 VLM ≈ 20~30s；生成 1 次图生图 ≈ 30~50s）。
        失败不阻断整集：封面是交付物但不是成片依赖。
        """
        self._cover_report = {"enabled": False}
        if not (getattr(self, "cover_enabled", False) or _cover_enabled()):
            return
        self._cover_report["enabled"] = True
        wd = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
        if not wd:
            self._cover_report["error"] = "没有 workdir"
            return
        try:
            from little_crew.review import cover_designer as _cd

            out = Path(str(wd)) / "delivery"
            # 候选：每镜的第一张产物（路径解析与视觉闸同一套）
            imgs = {}
            for s in picked:
                nm = (per_shot_artifacts.get(getattr(s, "shot_id", "")) or [""])[0]
                hit = list(Path(str(wd)).rglob(nm)) if nm else []
                if hit:
                    imgs[str(getattr(s, "shot_id", ""))] = str(hit[0])
            mode = str(getattr(self, "cover_mode", "select") or "select").lower()
            refs = []
            if mode == "generate":
                # 参考图：优先精修版角色设定图
                adir = Path(str(wd)) / "assets" / "characters"
                if adir.is_dir():
                    for sub in sorted(adir.iterdir()):
                        for name in ("sheet_refined.png", "sheet.png"):
                            f = sub / name
                            if f.is_file():
                                refs.append(str(f))
                                break
            report = _cd.build_cover(refs, imgs, out, mode=mode,
                                     title=str(getattr(self, "cover_title", "")
                                                or getattr(self, "title", "") or ""),
                                     title_mode=str(getattr(self, "cover_title_mode",
                                                           "overlay") or "overlay"),
                                     character=str(getattr(self, "cover_character", "") or ""),
                                     story=str(getattr(self, "title", "") or ""),
                                     style=str(getattr(self, "character_sheet_style", "") or ""))
            self._cover_report = {"enabled": True, **report}
            print(f"  封面设计师（{mode}）：{_cd.summarize(report)}")
            (out / "cover.json").write_text(
                __import__("json").dumps(self._cover_report, ensure_ascii=False,
                                         indent=2, default=str), encoding="utf-8")
        except Exception as exc:                              # noqa: BLE001
            print(f"  ⚠️ 封面设计师未运行：{type(exc).__name__}: {exc}")
            self._cover_report["error"] = f"{type(exc).__name__}: {exc}"

    def _run_character_sheets(self, script_draft):
        """角色设计师（图像半）：文字角色表 -> 锚点图集（2026-11-13）。

        三段职责分得很清：
        - **文字半**（`CharacterDesignerRules`）定外观（封闭选项，可复现）；
        - **图像半**（`CharacterSheetPainter`）把它画出来（按张计费，默认关）；
        - **本方法**只负责"要不要跑、跑几个、结果写哪、失败怎么记"。

        纪律：
        - 默认 **关**（`--char-sheets` 或 `LITTLE_CREW_CHAR_SHEETS=1` 才开）；
        - 失败**不阻断**（图集是辅助资料，不是成片依赖）；
        - 0 个角色/0 张图都要**明确打印**，不能静默当成功。
        """
        self._character_sheets = {"enabled": False, "painted": 0}
        if not (getattr(self, "character_sheets_enabled", False)
                or _cs_enabled()):
            return
        self._character_sheets["enabled"] = True
        wd = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
        try:
            from little_crew.agent.character_sheet import CharacterDesignerRules
            from little_crew.runtime.character_sheets import CharacterSheetPainter

            rules = CharacterDesignerRules()
            lib = rules.design(script_draft, episode_id=self.episode_id)
            if not lib.sheets:
                print("  ⚠️ 角色设计师：剧本里没有识别出角色，未出图集")
                self._character_sheets["reason"] = "no-characters"
                return
            painter = CharacterSheetPainter(
                model_id=str(getattr(self, "character_sheet_model",
                                     "") or "") or None,
                limit=int(getattr(self, "character_sheets_limit", 6) or 6))
            out = (Path(str(wd)) / "assets" / "characters") if wd else Path("assets/characters")
            report = painter.paint(
                lib.sheets, out,
                style=str(getattr(self, "character_sheet_style", "") or ""),
                proportion=str(getattr(self, "character_sheet_proportion", "adult")
                               or "adult"),
                refine=bool(getattr(self, "character_sheet_refine", False)),
                qc=bool(getattr(self, "character_sheet_qc", False)),
                qc_attempts=2 if getattr(self, "character_sheet_qc_strict", False) else 1)
            self._character_sheets = {"enabled": True, **report}
            _n_ok = len(report.get("painted") or ())
            _n_bad = len(report.get("failed") or ())
            print(f"  角色设计师：画 {_n_ok}/{len(lib.sheets)} 个角色"
                  f"（{report.get('model')}）"
                  + (f" · ⚠️ 失败 {_n_bad}" if _n_bad else ""))
            for item in (report.get("failed") or ())[:3]:
                print(f"    ⚠️ {item.get('name') or item.get('id') or '?'}："
                      f"{str(item.get('reason'))[:100]}")
            if wd:
                (_d := Path(str(wd)) / "delivery").mkdir(parents=True, exist_ok=True)
                (_d / "character_sheets.json").write_text(
                    __import__("json").dumps(self._character_sheets,
                                             ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
        except Exception as exc:                              # noqa: BLE001
            # 不阻断：图集失败不影响成片；但必须留痕（否则就是静默失败）
            print(f"  ⚠️ 角色设计师未运行：{type(exc).__name__}: {exc}")
            self._character_sheets["error"] = f"{type(exc).__name__}: {exc}"

    def _check_scene_subjects(self, picked, script_draft):
        """场景主体对账：分镜有没有引入剧本没授权的场景（2026-11-10 抽出）。

        结果写进 `self.scene_conflicts`（`severity=error` 才是否决依据），
        并打印「它拿什么在比」—— 黑盒判定不可核对。
        """
        # ---- 场景主体冲突（2026-10-29 接入）----
        #
        # **先记录，不拦截。** 理由：这条判据刚上线，还不知道会命中多少镜头；
        # 直接当门禁会让一整集大面积 RETRY，而那种代价没量过。
        # （同一天的教训：`set` 规则被三次否定，就是因为它先被当成"该加的规则"。）
        #
        # 记录进 `self.scene_conflicts`，并打印一行摘要 ——
        # 先看命中率，再决定它是 WARN 还是 RETRY。
        self.scene_conflicts = []
        try:
            from little_crew.review.scene_subject import check_scene_subject

            _script_text = str(getattr(script_draft, "text", None)
                               or getattr(script_draft, "content", None)
                               or script_draft or "")
            # **先打印「它拿什么在比」** —— 黑盒判定不可核对（2026-10-29）。
            # 之前只输出结论，重跑就不复现；看不出是"输入变了"还是"判定变了"。
            from little_crew.review.scene_subject import subjects_in as _subj

            _s_set = _subj(_script_text)
            print(f"  剧本场景主体 {sorted(_s_set) or '（空：抽取失败或剧本无场景）'}")
            self.scene_trace = []
            for _s in picked:
                _desc = str(getattr(_s, "description", "") or "")
                _r = check_scene_subject(_script_text, _desc)
                _sid = getattr(_s, "shot_id", "")
                self.scene_trace.append(
                    {"shotId": _sid, "script": _r["script"], "prompt": _r["prompt"],
                     "severity": _r["severity"], "descHead": _desc[:60]})
                if not _r["ok"]:
                    self.scene_conflicts.append({"shotId": _sid, **_r})
            # ⚠️ **不许 except: pass**（2026-10-29 自己犯的）：
            # 这段第一次写的时候吞了异常，于是 `scene_check.json` 没生成而毫无提示 ——
            # **我为修"静默失败"加的代码，自己静默失败了。**
            try:
                import json as _json

                _wd = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
                if not _wd:
                    raise RuntimeError("runner 上没有 workdir，无法落盘")
                _out = Path(str(_wd)) / "delivery"
                _out.mkdir(parents=True, exist_ok=True)
                (_out / "scene_check.json").write_text(
                    _json.dumps({"scriptSubjects": sorted(_s_set),
                                 "scriptHead": _script_text[:200],
                                 "shots": self.scene_trace},
                                ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as _e:                          # noqa: BLE001
                print(f"  ⚠️ scene_check.json 未写出：{type(_e).__name__}: {_e}")
        except Exception as _e:                              # noqa: BLE001
            print(f"  ⚠️ 场景主体检查未运行：{type(_e).__name__}: {_e}")

    @staticmethod
    def _outcomes_of(batch: Any) -> list:
        """兼容两种批结果形态：`CachedBatch.outcomes`（串行）与 `list[CacheOutcome]`（并行）。

        ⚠️ 2026-11-13 云端并行首次真跑踩到：`_render` 在云端并发分支返回
        `{"batch": [...]}`（纯列表），而下游一律按 `batch.outcomes` 访问 ——
        `AttributeError: 'list' object has no attribute 'outcomes'`，整集在渲染后崩掉。
        """
        outs = getattr(batch, "outcomes", None)
        if outs is not None:
            return list(outs)
        if isinstance(batch, (list, tuple)):
            return list(batch)
        return []

    def _normalize_cloud_image(self, path: Path) -> bytes | None:
        """把**云端产物**统一缩放到引擎分辨率（cover + 中心裁切）。

        ## 为什么必须做（2026-11-13）

        云端生图的输出尺寸由服务商决定：请求 1536x1024，可能返回 1024x1536；
        而 IR 里的 `resolution_matches` 约束与可数审片都按 `engine_resolution`
        （本地 832x480）核对 —— 于是 20 镜全被判 `size-mismatch`、门禁全要返工。

        两种"解法"里选哪个：
        - ❌ 改约束放宽：那等于**删掉合同**，以后真出低分辨率也没人拦
        - ✅ **落盘时归一**：合同不变，输入先被拉齐（本函数）

        返回归一后的字节（供证据链用同一份数据）；不需要归一返回 None。
        """
        be = getattr(self, "backend", None)
        if be is not None and bool(getattr(be, "is_local_by_default", True)):
            return None
        res = getattr(self, "engine_resolution", None)
        if not isinstance(res, (tuple, list)) or len(res) != 2:
            return None
        from little_crew.review.image_review import png_size

        got = png_size(path)
        if got is None or (int(got[0]), int(got[1])) == (int(res[0]), int(res[1])):
            return None
        w, h = int(res[0]), int(res[1])
        tmp = path.with_name(path.stem + "_norm.png")
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
              f"crop={w}:{h},setsar=1")
        # ffmpeg 解析：**先看实例属性**（demo 会直接赋绝对路径），再字面量路径，
        # 再环境变量/PATH。2026-11-13：只走 `_ffmpeg()` 时它退化成 "ffmpeg"
        # （本机 PATH 里没有），归一全部 FileNotFoundError —— 而同一集的视频阶段
        # 用的是 `self.ffmpeg` 属性，所以"视频能合、归一失败"。
        exe = "ffmpeg"
        for _cand in (getattr(self, "ffmpeg", None),
                      r"D:\AI\FFmpeg\bin\ffmpeg.exe",
                      str(self._ffmpeg() or "")):
            if _cand and pathlib.Path(str(_cand)).is_file():
                exe = str(_cand)
                break
        try:
            import subprocess as _sp

            _sp.run([exe, "-y", "-hide_banner", "-loglevel", "error",
                     "-i", str(path), "-vf", vf, "-frames:v", "1", str(tmp)],
                    check=True, capture_output=True, timeout=180)
            tmp.replace(path)
            return path.read_bytes()
        except Exception as exc:                              # noqa: BLE001
            print(f"  ⚠️ 云端产物归一失败（保留原尺寸）：{type(exc).__name__}: {exc}")
            return None

    def _materialize_batch(self, batch, picked) -> None:
        """把渲染产物**落到 workdir**：`assets/shots/<shotId>/<shotId>-<原文件名>`。

        ## 为什么必须要这一步（2026-11-13 云端整集跑出来的洞）

        本地通道（local-gen / ComfyUI）的产物引用是 URL，缓存层按名字存进
        `cache/t*/` 分片目录，`artifact_resolver` 能按文件名 rglob 找回来。
        但**云端 b64 产物**（`response_b64_path`）是由 HttpChannel 解码后写到
        **系统临时目录**（`%TEMP%/little-crew-inline/...`）的 —— workdir 里没有，
        于是可数审片 32 镜全部 `missing-artifact`、门禁 32 镜全要返工。

        这一步把字节**按镜头写进 workdir**，并把产物名改成稳定且唯一的
        `<shotId>-<basename>`（去掉了 Windows 临时路径里的盘符/反斜杠）。
        """
        wd = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
        if not wd:
            return
        import re as _re

        base = Path(str(wd)) / "assets" / "shots"
        prefix = f"G-{self.episode_id}-"
        for o in self._outcomes_of(batch):
            blobs = getattr(o, "artifacts", None)
            if not isinstance(blobs, dict) or not blobs:
                continue
            gid = str(getattr(o, "graph_id", "") or "")
            sid = gid[len(prefix):] if gid.startswith(prefix) else gid
            clean_map: dict[str, bytes] = {}
            for name, blob in blobs.items():
                if not isinstance(blob, (bytes, bytearray)):
                    continue
                bn = _re.split(r"[\\/]", str(name))[-1]
                if not _re.search(r"\.(png|jpe?g|webp)$", bn, _re.I):
                    bn += ".png"
                nm = f"{sid}-{bn}"
                d = base / sid
                try:
                    d.mkdir(parents=True, exist_ok=True)
                    dst = d / nm
                    dst.write_bytes(bytes(blob))
                except Exception:                             # noqa: BLE001
                    continue
                # 云端产物尺寸由服务商决定 -> 落盘后统一到引擎分辨率
                try:
                    norm = self._normalize_cloud_image(dst)
                except Exception:                             # noqa: BLE001
                    norm = None
                clean_map[nm] = norm if norm is not None else bytes(blob)
            if clean_map:
                try:
                    o.artifacts = clean_map
                except Exception:                             # noqa: BLE001
                    pass

    def _stage_image_review(self, picked, per_shot_artifacts, artifact_hashes):
        """渲染产物可数审片（2026-11-10 抽出）。

        只审可数项（存在/尺寸/体积/串镜）—— 本机没有视觉能力时，
        至少把「没审什么」写进产物：缺观测 != 通过。
        """
        # ---- 渲染产物可数审片（§3.10.2 第二层，2026-11-05）----
        #
        # 审片接在**渲染之后**：审的是真产物，不是意图。
        # 但本机 LLM 客户端没有视觉能力，所以只审**可数项**
        # （存在/尺寸/体积/串镜），并把"没审什么"写进产物 ——
        # 缺观测 != 通过。
        try:
            from little_crew.review import image_review as _irv

            _base0 = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
            _paths: dict[str, list[str]] = {}
            for _sid, _names in per_shot_artifacts.items():
                _got = []
                for _n in _names:
                    _f0 = Path(str(_base0)) / _n if _base0 else None
                    if _f0 is not None and not _f0.is_file():
                        _hits = list(Path(str(_base0)).rglob(_n)) if _base0 else []
                        _f0 = _hits[0] if _hits else _f0
                    _got.append(str(_f0) if _f0 is not None else "")
                _paths[_sid] = [x for x in _got if x]
            # 期望尺寸：本地引擎用 IR 分辨率；**云端后端产物尺寸由服务商决定**
            # （实测 1536x1024 请求返回 1672x941），用"首张实际尺寸"当基准，
            # 校验的是"所有镜一致"，而不是"等于本地配置"。
            _expect = None
            if isinstance(self.engine_resolution, (tuple, list)):
                _expect = tuple(self.engine_resolution)
            _be0 = getattr(self, "backend", None)
            _local0 = bool(getattr(_be0, "is_local_by_default", True)) if _be0 else True
            if not _local0:
                for _names0 in _paths.values():
                    for _p0 in _names0:
                        _got0 = _irv.png_size(_p0)
                        if _got0:
                            _expect = tuple(_got0)
                            break
                    if _expect and tuple(_expect) != tuple(self.engine_resolution or ()):
                        break
            _review = _irv.review(
                picked,
                artifacts_by_shot=_paths,
                expect_size=_expect,
                hashes_by_shot=dict(artifact_hashes),
            )
            _wd1 = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
            if _wd1:
                (_out1 := Path(str(_wd1)) / "delivery").mkdir(parents=True, exist_ok=True)
                import json as _j1

                (_out1 / "image_review.json").write_text(
                    _j1.dumps(_review, ensure_ascii=False, indent=2),
                    encoding="utf-8")
            _err_n = int((_review.get("counts") or {}).get("error", 0))
            # ⚠️ **dry-run 不许谎报**（2026-11-06 体检发现）：
            # 没渲染时每个镜头都会被记成 `missing-artifact` 严重缺陷，
            # 一次 27 镜就是 27 条 error —— 看起来像灾难，实际是"没跑渲染"。
            # 判据：**一镜都没尝试渲染且没有失败**（真有失败时仍然要报）。
            _attempted = int(getattr(self, "rendered", 0) or 0) + int(
                getattr(self, "failed", 0) or 0)
            if _attempted == 0:
                _review["skipped"] = "dry-run（未尝试渲染），审片不适用"
                print("  审片（可数项）：未渲染 —— 跳过（不是通过）")
            else:
                print(f"  审片（可数项）：{_review['total']} 个产物 · 严重 {_err_n} 条")
            self._image_review = _review
        except Exception as _e_rv:                            # noqa: BLE001
            print(f"  ⚠️ 渲染审片未运行：{type(_e_rv).__name__}: {_e_rv}")





    def _render_backend_healthcheck(self) -> bool:
        """渲染前探一次后端（2026-11-11）。

        EF27 有 6 镜栽在 `channel_unavailable: 无法连接 127.0.0.1:8188`，
        而集成是在**整集跑完 20 分钟后**才发现 —— 6 镜已经永久缺图。
        这里先探一次：不可用就打印一行警告（**不阻断**：可能只是想跑缓存）。
        """
        backend = getattr(self, "backend", None)
        url = str(getattr(backend, "endpoint", "") or "")
        if "8188" not in url:
            return True
        try:
            import urllib.request as _ur

            with _ur.urlopen(url.rstrip("/") + "/system_stats", timeout=5) as r:
                ok = getattr(r, "status", 0) == 200
        except Exception as e:                                # noqa: BLE001
            print(f"  ⚠️ 渲染后端探活失败：{type(e).__name__}: {e}")
            print(f"     （{url}）后面 6 镜级的丢失通常就是它引起的")
            return False
        return bool(ok)

    def _retry_channel_failures(self, batch, irs, rstats):
        """把"通道不可用"的失败镜重试一次（2026-11-11）。

        返回 `(新 batch, 重试成功的镜数)`。只重试瞬时错误
        （`channel_unavailable` / `needs_reconcile`），别的失败不动 ——
        "一律重试"会把模型本身的问题拖成 2 倍时间。
        """
        _retryable = {"channel_unavailable", "needs_reconcile"}
        bad = [o for o in (getattr(batch, "outcomes", ()) or ())
               if not o.ok and str(getattr(o, "failure", "")) in _retryable]
        if not bad:
            return batch, 0
        _ids = {str(getattr(o, "graph_id", "")) for o in bad}
        _irs = [ir for ir in irs if str(ir.get("graphId") or "") in _ids]
        if not _irs:
            return batch, 0
        try:
            _again = self._render(_irs)
        except Exception as e:                                # noqa: BLE001
            print(f"  ⚠️ 渲染重试未运行：{type(e).__name__}: {e}")
            return batch, 0
        _b2 = (_again or {}).get("batch")
        if _b2 is None:
            return batch, 0
        _fixed = {str(getattr(o, "graph_id", "")) for o in _b2.succeeded}
        # 用重试结果替换原 batch 里同名镜头的结论
        merged = tuple(_b2.succeeded) + tuple(
            o for o in (getattr(batch, "outcomes", ()) or ())
            if str(getattr(o, "graph_id", "")) not in _fixed)
        try:
            from little_crew.runtime.cached_render import CachedBatch

            new_batch = CachedBatch(outcomes=merged)
        except Exception:                                     # noqa: BLE001
            return batch, 0
        return new_batch, len(_fixed)


    def video_out_dir(self) -> Path:
        return (self.workdir or Path(".")) / self.video_subdir

    def _assemble_video(
        self,
        manifest: EpisodeManifest,
        artifacts: Mapping[str, Sequence[str]],
    ) -> tuple[Any, Path | None]:
        """把清单编译成合成计划并出片。

        **产物名 → 实际文件的解析由调用方提供**（`resolve_artifact`）——
        产物散落在缓存的分片目录里，视频层不该去猜目录结构。

        合成失败**不影响前面各步的结论**：图已经出了、清单已经产出了，
        视频只是多一种呈现。所以这里捕获异常并返回 `(结果, None)`，
        而不是让整集失败。
        """
        resolver = self.resolve_artifact
        if resolver is None:
            return (None, None)

        from little_crew.runtime.video import (
            FFmpegTools, VideoAssembler, compile_clip_plan)

        try:
            plan = compile_clip_plan(
                manifest.as_dict(), resolve=resolver,
                width=self.video_width, height=self.video_height)
            if not plan.clips:
                return (None, None)

            out_dir = self.video_out_dir()
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / self.video_filename
            asm = VideoAssembler(FFmpegTools(), workdir=out_dir)
            res = asm.assemble(plan, out)
            return (res, res.out_path if res.ok else None)
        except Exception as exc:                              # noqa: BLE001
            # 合成是**附加价值**，不是主链路的必要条件 —— 不让它毁掉整集记录
            return ({"error": f"{type(exc).__name__}: {exc}"}, None)

    #: 产物名 → Path 的解析函数。为 None 时跳过视频合成。
    resolve_artifact: Any = None

    #: 成片分辨率
    video_width: int = 832
    video_height: int = 480

    # ---- 空间控制：白模 ------------------------------------------------

    #: **L3 跨模型族盲读器**（`agent.blind.BlindReader`）。`None` = 不做盲读。
    #:
    #: ⚠️ 它是**增量能力**：`None` 时盲读约束会被如实报成"缺观测"而跳过，
    #: 不是静默通过 —— 这条保证了"没配 VLM"和"盲读说没问题"可区分。
    blind_reader: Any = None

    #: **返工闭环最多跑几轮。**
    #:
    #: `0` = 关闭返工（门禁只判定不重跑）。
    #:
    #: ⚠️ **没有这个上限，循环可能白烧算力** ——
    #: 门禁不退让（例如约束本身不可满足）时会一直重跑。
    #: 上限是三道防烧闸里的第一道（见 `_repair_loop`）。
    max_repair_rounds: int = 2

    #: **是否在渲染后跑验收门禁**（C3）。
    #:
    #: 关闭时证据里的 `gate` 为空 —— 与"门禁跑了且全过"**可区分**。
    #: 这个区分是刻意的：把"没检查"与"检查了没问题"混为一谈，
    #: 正是 C3 要消灭的自我签收。
    verify_artifacts: bool = True

    #: ⚠️ **调试用**：无视剧本门禁裁定，强行进入分镜与渲染。
    #:
    #: 存在的理由：**"剧本标定"与"下游链路"是两件独立的验证**。
    #: 剧本门禁的阈值还在标定中（分数有噪声），而下游（桥接/渲染/合成）
    #: 的正确性不该被一个可调阈值挡住 —— 否则每次验下游都要先赌剧本能过。
    #:
    #: **正式跑绝不能开。** 开了会让不合格剧本也出片，
    #: 所以：① 默认 False；② 结果里 `scriptAccepted` 仍如实标记为 False；
    #: ③ CLI 的帮助文案明确写"调试用"。
    force_accept_script: bool = False

    #: **视频模式**（2026-10-18 V2）：开了就在静帧之后自动跑第二段，
    #: 把每个静帧动画化成片段，再拼成成片。
    #:
    #: ⚠️ **成本**：实测 331s/镜。2 镜 ≈ 11 分钟，24 镜 ≈ **2.3 小时**。
    #: 所以默认关闭 —— 打开它是个决定，不是一个顺手。
    video_mode: bool = False

    #: 每镜片段的秒数（H3 实测上限 10 秒，2 秒最稳）
    video_seconds: float = 2.0

    #: **顾问批评家**（2026-10-18）：这些职位的意见会进报告，但**不进裁定**。
    #:
    #: 例如 `("continuity_supervisor", "compliance_officer")`。
    #: 空元组 = 不跑（默认）。
    #:
    #: ⚠️ 它们**不是免费的**：每个职位每集一次 LLM 调用。
    advisory_critics: tuple[str, ...] = ()

    #: **每镜的白模首帧**（`shotId -> Path`），由 `_run_previs()` 填。
    #: 有它就用它当控制图；没有才退到旧的启发式挑帧。
    # ⚠️ **必须在这里声明默认值，不能写 `field(default_factory=dict)`** ——
    # `EpisodeRunner` 是**普通类**（手写 `__init__`），不是 dataclass。
    # 写 `field(...)` 会让类属性变成 `Field` 对象，`self.previs_first` 直接是坏的
    # （实测：`AttributeError: 'Field' object has no attribute 'items'`）。
    # 真正的每实例字典在 `__init__` 里建。
    previs_first: dict[str, Path] = {}

    #: 白模渲染的 Blender 可执行文件。空则不需要白模。
    blender_exe: str = ""

    #: 白模分辨率 —— **必须与出图一致**，否则首帧做 ControlNet 会被缩放。
    previs_resolution: tuple[int, int] = (640, 384)

    #: 是否把白模预演作为控制信号注入（需要 ComfyUI 通道 + Z-Image ControlNet）
    use_white_model: bool = False

    #: 是否把模型源换成 Z-Image 的**分离加载**。
    #:
    #: ⚠️ **与 `use_white_model` 是两件独立的事**，必须能分别开关 ——
    #: 否则**做不了对照实验**：
    #:   · 两个都开 —— 有控制
    #:   · 只开这个 —— 无控制的基线（用来量控制到底带来了多少改变）
    #:   · 只开白模 —— 无意义（控制节点需要 Z-Image 的 VAE 与架构）
    #:
    #: 把它们耦合在一起的后果，实测就是对照组全部编译失败
    #: （退回 `model.checkpoint` -> `CheckpointLoaderSimple`，
    #: 而 Z-Image 环境里没有可用的 checkpoint 文件）。
    use_zimage_source: bool = False

    #: 白模预演序列的根目录（每个动作一个子目录，内含 `gray/` 与 `depth/`）
    previs_root: Path | None = None

    #: 控制通道：`"gray"` 或 `"depth"`。
    #:
    #: **实测结论：gray 优于 depth** —— depth 那版产出的图无头、手臂怪异
    #: （见 CHANGELOG alpha.19）。这与策划书 §3.8.8 的 U2 预期相反。
    previs_channel: str = "gray"

    #: ComfyUI 的 `input/` 目录。
    #:
    #: ⚠️ **必须设置，否则白模控制不生效**（且不会报错）。
    #: ComfyUI 的 `LoadImage` 走 `folder_paths.get_annotated_filepath()`，
    #: **只接受该目录内的相对文件名** —— 绝对路径会被
    #: `custom_validation_failed` 拒绝；而若把绝对路径当相对名传进去，
    #: 它会在 `input/` 下找一个同名文件，找不到就失败。
    #:
    #: 白模帧原本在项目外的预演目录里，因此必须先**投放**进来。
    comfy_input_dir: Path | None = None

    #: ControlNet 强度。
    #:
    #: ⚠️ **实测 0.5 是"定位准 + 人自然"的平衡点**（见 CHANGELOG alpha.21）。
    #: 三档强度（1.0 / 0.6 / 0.35）的**构图、人物大小、位置完全一致**，
    #: 只有姿态自由度不同 —— 所以**降强度不损失定位**，只让人更自然。
    #: 早期默认 1.0 时人物被白模骨架驯化，"完全不像人"。
    control_strength: float = 0.5

    #: 控制信号的高斯模糊半径（像素，@832×480 基准）。**0 = 不模糊。**
    #:
    #: 为什么需要它：白模的实心剪影带内部明暗，模型会去描摹骨架的内部细节
    #: （手臂、腿的精确位置），人物被驯化成骨架形。模糊抹掉内部结构，
    #: 只留"这一团是中景偏左的一个人，这么大" —— **定位保住，外观还给模型**。
    #:
    #: 做成**图节点**而非平台侧预处理：`blur_radius` 会自动进入 IR 哈希，
    #: 改半径必然重渲。平台侧处理则要额外维护一份参数哈希 ——
    #: 漏了就是"改了参数却命中旧缓存"，那类失效**无法察觉**。
    previs_blur_radius: int = 18

    #: 模糊的高斯标准差（配合 `previs_blur_radius`）
    previs_blur_sigma: float = 5.0

    #: 动作名 -> 白模序列目录名。**不在表里的动作退回 `act_stand`。**
    #:
    #: 镜头动作 -> **白模渲染模板名**。
    #:
    #: ⚠️ **权威定义已下移到 `agent/messages.py`** —— 它定义的是
    #: `Shot.action` 能取什么值，而 `Shot` 在消息层。
    #: 这里保留这个名字是**别名**，避免破坏既有调用方
    #: （`episode.py` 自身与测试都用 `EpisodeRunner.PREVIS_ACTIONS`）。
    #:
    #: 为什么下移：`agent/storyboard_llm.py` 要用它校验 LLM 输出，
    #: 而 `agent`(层 6) 不能 import `runtime`(层 8)。**不要把这份表抄回来** ——
    #: 两份真相比一份错更难查。
    #:
    #: 做成 property 而不是类属性：`episode.py` 全篇用**函数内延迟导入**
    #: （避免顶层循环 import），而类属性在**定义时**求值 ——
    #: 那会强制在模块顶层导入 `messages`，破坏这套约定。
    @property
    def PREVIS_ACTIONS(self) -> Mapping[str, str]:     # noqa: N802 - 保持原名
        from little_crew.agent.messages import PREVIS_ACTIONS as _PA

        return _PA

    def _control_frame_for(self, shot: Any) -> Path | None:
        """取这一镜的白模控制帧：**新白模优先，旧的启发式兜底**。"""
        sid = str(getattr(shot, "shot_id", "") or "")
        got = self.previs_first.get(sid) if sid else None
        if got is not None and Path(got).is_file():
            return Path(got)
        return self._previs_frame_for(shot)

    def _run_previs(self, shots: Any = None) -> dict[str, Any]:
        """跑**预演师**：分镜 → 布局 → 每镜白模 → 首帧。

        产出的首帧存进 `self.previs_first`，供 `_control_frame_for` 取用。

        **失败不阻断**（没有白模，分镜照样能出图，只是失去空间约束），
        但失败原因要进返回值，不能静默。
        """
        impls = getattr(self.orch, "_impls", {}) or {}
        if "previs" not in impls:
            # **不能返回空 dict**（2026-10-29 修）。
            #
            # `{}` 与「白模跑了但没产出」在结报告里长得一模一样 ——
            # 于是「没绑上实现」会被误读成「白模失败」，而白模失败又被
            # 设计成"不阻断"，最终表现是：一集"跑通"了，控制图一张没进画面。
            #
            # 本项目一条硬规矩：**不可用必须报出来，不能假装成功。**
            return {"ok": False,
                    "reason": "没有绑定 previs 实现 —— 加 --llm"
                              "（--llm-previs 需要它）"}
        shot_list = self.orch.blackboard.value("shot.list")
        # 白模只预演**将要渲染的那几镜**（`--limit` 必须同时管这两处）。
        # 原来取的是全量表 —— 于是 `--limit 3` 仍渲 39 镜的白模，
        # 多出来的永远不会被用到：白花钱、白等时间。
        if shots:
            try:
                import dataclasses as _dc

                shot_list = _dc.replace(shot_list, shots=tuple(shots))
            except Exception:                            # noqa: BLE001
                import types as _t

                shot_list = _t.SimpleNamespace(shots=tuple(shots))
        if shot_list is None:
            return {"ok": False, "reason": "没有镜头表 —— 无法预演"}
        # ⚠️ 2026-10-21 实测：`--limit 3` 只限制了**渲染**，白模仍按全量
        # 39 镜跑（多花约 10 分钟），因为这里取的是 `blackboard` 的**全量**表。
        #
        try:
            out = self.orch.run_stage("previs", "previs.layouts",
                                     {"shot.list": shot_list})
        except Exception as e:                                # noqa: BLE001
            return {"ok": False, "reason": f"{type(e).__name__}: {e}"[:200]}
        if not getattr(out, "ok", False):
            return {"ok": False, "reason": str(getattr(out, "error", ""))[:200]}
        layouts = self.orch.blackboard.value("previs.layouts") or []
        made: list[str] = []
        failed: list[dict[str, str]] = []
        from little_crew.runtime.previs import render_layout
        for lay in layouts:
            if str(lay.get("verdict")) != "KEEP":
                failed.append({"shotId": str(lay.get("shotId")),
                               "reason": f"布局未通过：{lay.get('verdict')}"})
                continue
            r = render_layout(lay.get("layout") or {}, blender=self.blender_exe,
                              out_root=str(self.workdir / "previs"),
                              resolution=self.previs_resolution)
            if r.get("ok"):
                # **存"人形"那份，不存圆柱那份**（`alpha.108` 实测）：
                # 圆柱当 ControlNet 输入会把人体压成"柱子形状的涂抹"；
                # 人形（锥台躯干+球头+两腿）能出正常人体。
                # 圆柱版留着给人看（`previs.mp4`），不进控制链路。
                ctrl = r.get("control") or ""
                target = Path(ctrl) if ctrl else Path(r["first"])
                if not ctrl:
                    failed.append({
                        "shotId": str(lay.get("shotId")),
                        "reason": "没有控制图（退回圆柱首帧，画质会差）"})
                self.previs_first[str(lay.get("shotId"))] = target
                made.append(str(lay.get("shotId")))
            else:
                failed.append({"shotId": str(lay.get("shotId")),
                               "reason": str(r.get("reason"))[:160]})
        return {"ok": bool(made), "shots": made, "failed": failed}

    def _previs_frame_for(self, shot: Any) -> Path | None:
        """给一个镜头挑白模控制帧。**确定性且有理由，不是随便挑。**

        两条规则：

        ① **按动作选序列** —— 走路的镜头用走路的白模，而不是站姿
        ② **取动作的峰值帧而非首帧** —— 动作模板在 ``t=0`` 是**起始站姿**
           （见 `pose_at`），取首帧会得到一张"没在动"的图。
           这是实测踩过的**采样陷阱**：以为是"姿态没生效"，其实是取错了帧。

        取不到时返回 ``None``，由调用方退化为"无控制"——
        **缺白模不该让整镜失败**，只是失去空间约束。
        """
        if self.previs_root is None:
            return None
        act = str(getattr(shot, "action", "") or "").strip().lower()
        sub = self.PREVIS_ACTIONS.get(act, "act_stand")
        d = Path(self.previs_root) / sub / self.previs_channel
        if not d.is_dir():
            return None
        frames = sorted(d.glob("*.png"))
        if not frames:
            return None
        # 峰值帧：动作模板在 t=0 是起始站姿，取正中而非首帧
        return frames[len(frames) // 2]

    def _stage_control_image(self, frame: Path) -> str:
        """把控制图**投放**到 ComfyUI 的 `input/` 目录，返回可用的文件名。

        **为什么必须做这一步**：ComfyUI 的 `LoadImage` 走
        `folder_paths.get_annotated_filepath()`，**只认 `input/` 内的相对文件名**：
          · 传绝对路径 -> `custom_validation_failed`（提交被拒）
          · 传一个不在 input/ 里的"相对名" -> 找不到文件

        投放是**幂等**的：按内容哈希命名，同一帧重复投放只覆盖同名文件。
        用哈希而不是原名，是为了避免不同动作序列里同名的 `f00020.png` 互相覆盖
        —— 那会让"换了白模"这件事**看不出来**（图还是老的），
        与缓存串号是同一类无法察觉的失效。
        """
        import hashlib
        import shutil

        if self.comfy_input_dir is None:
            return str(frame)

        raw = frame.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()[:16]
        name = f"previs_{frame.parent.parent.name}_{digest}.png"
        dest = Path(self.comfy_input_dir) / name
        if not dest.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(frame, dest)
        return name

    def _apply_white_model(self, ir: dict[str, Any], shot: Any) -> dict[str, Any]:
        """把白模控制接进 IR。**不需要控制时返回原 IR。**

        变换三步：

        ① `model.checkpoint` -> `model.source_zimage`
           （ComfyUI 里 Z-Image 是分离加载：UNET + CLIP + VAE 三个节点）
        ② **把所有指向模型源的 CLIP / VAE 端口改指到辅助加载器**
        ③ 插入 `image.file`（控制图）+ `composition.zimage_controlnet`，
           并把采样的 model 输入改接到控制节点

        **为什么做在 IR 层而不是图里**：这样缓存键（`irHash`）会包含控制图路径 ——
        **换了控制图就是换了 IR，缓存会正确失效**。
        若在图里偷偷加节点，改了白模却命中旧缓存，而那是无法察觉的。

        ⚠️ 第 ② 步不是可选的。`shot_to_ir` 把 prompt 节点的 `clip` 端口**显式**
        指向模型源（`{"ref": "n_ckpt", "port": "CLIP"}`）。换源之后
        `n_ckpt` 是 `UNETLoader`，它的 `output[0]` 是 **MODEL** ——
        ComfyUI 会以 `return_type_mismatch: clip, received_type(MODEL)` 拒绝。
        """
        want_ctrl = self.use_white_model
        want_src = self.use_zimage_source or want_ctrl
        # **新白模优先**（2026-10-19）：`PrevisAgent` 判断布局 + Blender 渲染的首帧。
        # 旧的启发式挑帧（按动作选序列 + 取峰值帧）**降为兜底** ——
        # 它是为「姿态」服务的，而新范围只要位置与机位。
        frame = self._control_frame_for(shot) if want_ctrl else None
        if frame is None:
            want_ctrl = False
        if not want_src:
            return ir

        out = dict(ir)
        nodes = [dict(n) for n in ir.get("nodes", ())]

        # ---- ① 模型源 + ② 辅助加载器 ----
        #
        # 这两步抽到 `runtime/model_source.py` 了 —— 因为
        # **「哪些模型源可用、怎么换」是 Z-Image 通路的公共知识**，
        # 不该埋在 `EpisodeRunner` 的私有方法里；
        # 白模 A/B 实验脚本是它的第二个调用方，而**复制即漂移**。
        from little_crew.runtime.model_source import apply_zimage_source
        src = apply_zimage_source(
            nodes,
            unet_name=self.zimage_unet,
            clip_name=self.zimage_clip,
            vae_name=self.zimage_vae,
            aux_loader_kind=self._aux_loader_kind,
            src_node_id="n_ckpt",
        )
        src_id = src["src_id"]
        node_ids = {n["id"] for n in nodes}

        # ---- ③ 控制（只在需要时）----
        #
        # 这一步也抽到了 `runtime/model_source.py`（同理由：A/B 脚本要用它，
        # 而复制即漂移）。
        from little_crew.runtime.model_source import apply_control
        # **只在真的要用控制图时调用**：
        # 早先这里无条件调用，于是 `want_ctrl=False`（拿不到控制图）时
        # 仍会求值 `_stage_control_image(None)` -> `AttributeError`。
        # 白模失败本应「降级为不出控制图」，不该把整集打崩。
        if want_ctrl:
            apply_control(
                nodes,
                src=src,
                staged_image=self._stage_control_image(frame),
                blur_radius=int(self.previs_blur_radius),
                blur_sigma=float(self.previs_blur_sigma),
                strength=float(self.control_strength),
                channel=str(self.previs_channel),
            )
        out["nodes"] = nodes
        return out

    def _aux_loader_kind(self, dtype: str) -> str:
        """查编译器：模型源里哪个辅助加载器提供 ``dtype``。

        从**规则表**读而不是写死 —— 换模型源实现时不必改这里。
        编译器不可得时返回空串（调用方退回原引用）。
        """
        try:
            compiler = getattr(
                getattr(self.renderer, "session", None), "compiler", None)
            rules = getattr(compiler, "rules", None)
            if rules is None:
                return ""
            src = rules.get(getattr(compiler, "model_source_kind",
                                    "model.checkpoint"))
            if src is None:
                return ""
            for lkind, ldtype in (src.requires_loader or {}).items():
                if str(ldtype).upper() == dtype.upper():
                    return str(lkind)
        except Exception:                                     # noqa: BLE001
            pass
        return ""

    #: Z-Image 的三个权重文件名（ComfyUI 侧）
    zimage_unet: str = "z_image_turbo_int8_convrot.safetensors"
    zimage_clip: str = "qwen_3_4b_fp4_mixed.safetensors"
    zimage_vae: str = "ae.safetensors"

    # ---- 渲染（由调用方提供引擎参数）----------------------------------

    #: 引擎名（写进 IR 的 checkpoint 字段）
    engine_checkpoint: str = "z-image-turbo"
    #: 采样步数
    engine_steps: int = 8
    #: 随机种子
    engine_seed: int = 7
    #: 风格后缀（应与引擎语言匹配）
    engine_style: str = "2D 动画风格，赛璐璐上色，电影感构图，细致背景，戏剧性光影"
    #: 提示词片段语言
    engine_language: str = "zh"

    #: 产出分辨率 `(宽, 高)`。
    #:
    #: ⚠️ **这个值必须与引擎实际产出一致** —— 它会变成
    #: `cr_res_*` 硬约束（`resolution_matches`），不符就阻断交付。
    #: 把约束写死成一个与实际产出无关的常量，
    #: 结果是**每一镜都被判不合格**（实测：夹具产 64x48 而约束写 832x480，
    #: 12 镜全部 RETRY）。门禁没错，是约束没反映真实规格。
    engine_resolution: tuple[int, int] = (832, 480)
    #: 采样器与 CFG。
    #:
    #: ⚠️ **这两个值对 ControlNet 的效果影响极大，不是"随便选一个"。**
    #: 实测（2026-10-12，Z-Image + Fun-ControlNet lite）：
    #:   · `euler` + cfg 4.0 -> 干净、机位跟随
    #:   · `dpmpp_2m` + cfg 5.5 -> **大片暗块、荧光错色、人物数量错乱**
    #: 后者与"控制强度过高把画面压垮"的表现一致。
    #: 默认取 Z-Image-Turbo 官方示例的 `euler` + 4.0。
    engine_sampler: str = "euler"
    engine_cfg: float = 4.0
    #: 采样调度器。
    #:
    #: ⚠️ **Z-Image 上不能用 `karras`** —— 实测产出大片暗块 + 荧光错色
    #: + 人物数量错乱；换 `simple` 后完全干净（其余参数全同）。
    engine_scheduler: str = "simple"

    def _bridge(self, shot: Any) -> dict[str, Any]:
        """把镜头桥接为 IR。

        **角色外观在这里进入提示词** —— 这是"角色一致"的文字层实现。

        ⚠️ 外观必须**逐字稳定**（同一角色任何镜头都得到同一串字符），
        否则 `irHash` 会随镜头漂移，缓存永不命中，而且看起来像正常行为。
        `Appearance.prompt_fragment()` 保证了这一点。
        """
        from little_crew.agent.ir_bridge import shot_to_ir

        text = self._with_appearance(shot)
        # 记下"我们要求的是什么"（**并入外观之后**的描述）——
        # 那才是真正决定画面内容的文本。写进实例字典而不是改签名：
        # `_bridge` 的签名被测试 monkeypatch 依赖，改它等于改契约。
        try:
            self._prompts_seen[str(getattr(shot, "shot_id", ""))] = str(
                getattr(text, "description", "") or "")
        except Exception:                                # noqa: BLE001
            pass
        # ---- 角色锚点图（2026-11-05 接入）----
        #
        # `shot_to_ir(anchor_sets=...)` 从 alpha.130 就支持，编译器的
        # `identity.ipadapter -> lora -> lora_model_only` 降级链也写好了，
        # **但从来没有任何调用方传过它** —— 于是"角色一致性"一直只在提示词文字层。
        # 这里补上调用方：能找到定妆图就传，找不到就**不传**（不编路径）。
        _anchors = getattr(self, "_anchor_table", None)
        if _anchors is None:
            try:
                from little_crew.runtime import anchors as _anc

                _wd = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
                _anchors = {}
                if _wd:
                    _anchors = _anc.discover(Path(str(_wd)) / "assets")
                if not _anchors:
                    # 退到仓库级资产库（跨集复用）
                    _anchors = _anc.discover(
                        Path(__file__).resolve().parents[3] / "assets")
                self._anchor_table = _anchors
            except Exception:                              # noqa: BLE001
                _anchors = {}
                self._anchor_table = {}
        _sets = {}
        try:
            from little_crew.runtime import anchors as _anc2

            _sets = _anc2.sets_for(getattr(shot, "characters", ()), _anchors)
            if _sets:
                self._anchors_used[str(getattr(shot, "shot_id", ""))] = sorted(_sets)
        except Exception as _e_anc:                        # noqa: BLE001
            _sets = {}
            print(f"  ⚠️ 角色锚点未生效：{type(_e_anc).__name__}: {_e_anc}")
        ir = shot_to_ir(
            text, episode_id=self.episode_id,
            anchor_sets=dict(_sets) if _sets else None,
            checkpoint=self.engine_checkpoint, steps=self.engine_steps,
            cfg=self.engine_cfg, sampler=self.engine_sampler,
            scheduler=self.engine_scheduler,
            resolution=self.engine_resolution,
            style_suffix=self.engine_style,
            prompt_language=self.engine_language)
        # 空间控制：白模预演 -> ControlNet
        return self._apply_white_model(ir, shot)

    def _with_appearance(self, shot: Any) -> Any:
        """把出场角色的外观片段并进镜头描述。

        产出的是一个**新的 Shot**（`Shot` 是 dataclass，用 `replace` 复制）——
        不改调用方传进来的对象，因为那一份可能还要用于别处。

        ⚠️ **每段外观都必须带上角色名。**
        同框两个角色时，若只写「（外观A；外观B）」，模型**分不清哪段是谁的** ——
        实测产出的正是"乌黑高马尾…；乌黑高马尾…"这种无法归属的描述。
        加上名字后变成「少女：…；旅人：…」，归属才明确。

        这与 `ir_bridge` 里给角色标记 ``@名字`` 是同一个道理：
        **提示词里的每一段都必须能追溯到它说的是谁。**
        """
        if not self.characters or not self.characters.sheets:
            return shot

        try:
            names = tuple(getattr(shot, "characters", ()) or ())
            frags: list[str] = []
            for nm in names:
                sheet = self.characters.by_name(nm)
                if sheet is None:
                    continue
                f = sheet.prompt_fragment
                if not f:
                    continue
                tagged = f"{nm}：{f}"
                if tagged not in frags:
                    frags.append(tagged)
            if not frags:
                return shot

            import dataclasses
            import re

            # ⚠️ **去掉 `@名字` 标记**（2026-10-17 从证据链里发现的）。
            #
            # `@名字` 是**剧本解析约定**（`_characters_of()` 靠它认角色），
            # 不是画面内容。留着它会原样进提示词：
            #
            #     实测：`@少女 独自走在山坡上采花…（少女：…）`
            #
            # 模型看到的是一个无意义的 `@` 符号 —— 轻则被忽略，
            # 重则被当成"另一个人"的标记。**解析标记不该漏进产物。**
            desc = re.sub(r"@([^\s@（）,，。；;]+)", r"\1",
                          str(shot.description or ""))
            merged = f"{desc}（{'；'.join(frags)}）"
            return dataclasses.replace(shot, description=merged)
        except Exception:                                     # noqa: BLE001
            # 外观合并失败**不该毁掉整集** —— 退化成没有外观的提示词，
            # 结果只是"角色可能不一致"，而不是"出不了片"。
            return shot

    # ---- 角色定妆 ------------------------------------------------------

    #: 角色外观的人工覆盖：``{角色名: {字段: 值}}``。给了就以它为准。
    character_overrides: Any = None

    def _design_characters(self, script: str) -> Any:
        """跑角色设计师，产出定妆表。**失败不阻断整集。**"""
        from little_crew.agent.character_sheet import CharacterDesignerRules

        try:
            rules = CharacterDesignerRules(
                overrides=self.character_overrides or {})
            return rules.design(script, episode_id=self.episode_id)
        except Exception:                                     # noqa: BLE001
            return None

    def _render(self, irs: list[dict[str, Any]],
                on_progress: Any = None) -> dict[str, Any]:
        """走缓存感知渲染器。

        **引擎差异只在 `render_http_batch` 与 `render_batch` 之间** ——
        缓存、检查点、失败分类全部复用。
        """
        backend = self.engine_backend()
        # 用途守卫（2026-11-13）：主链路渲染**不允许**走"专属某职位"的云端通道
        # （如只给角色设计师用的云端生图）。这里抛错比"悄悄用了"好一万倍。
        try:
            from little_crew.runtime.backend_guard import assert_allowed

            if backend is not None:
                assert_allowed(backend, "render", what="主链路渲染")
        except Exception as _e_bg:                            # noqa: BLE001
            from little_crew.runtime.backend_guard import BackendNotAllowed

            if isinstance(_e_bg, BackendNotAllowed):
                raise
            raise
        # **按后端种类分流**（2026-10-22 修）。
        #
        # 原来只判 `backend is not None` —— 而 ComfyUI 的后端**也非 None**，
        # 于是它被送进 `render_http_batch`（local-gen 的 http 形态），
        # 被 `ComfyChannel` 以「不能提交 http 形态的任务」拒掉。
        #
        # 真编译器、真控制图都准备好了却没生效，卡在这一行判断上。
        from little_crew.models.spec import BackendKind
        kind = getattr(backend, "kind", None) if backend is not None else None
        if backend is not None and kind is not BackendKind.COMFYUI:
            try:
                from little_crew.runtime.render_policy import (
                    explain as _explain_policy,
                    render_workers as _workers_of,
                )

                _w = _workers_of(backend)
                print(f"  {_explain_policy(backend)}")
            except Exception:                                 # noqa: BLE001
                _w = 1
            jobs = [(self._values(ir), ir["graphId"]) for ir in irs]
            if _w > 1 and len(jobs) > 1:
                # 云端才走到这里（本地策略恒为 1）：远端排队，本地只等 I/O
                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(max_workers=min(_w, len(jobs))) as _ex:
                    _outs = list(_ex.map(
                        lambda j: self.renderer.render_http(j[0], backend, job_id=j[1]),
                        jobs))
                # ⚠️ 必须包成 `CachedBatch` 再返回：下游全靠它的派生属性
                # （`cache_hits` / `rendered` / `seconds_saved` …）。
                # 2026-11-13 云端并行首次真跑：直接返回 list -> 成片阶段
                # `AttributeError: 'list' object has no attribute 'cache_hits'`。
                from little_crew.runtime.cached_render import CachedBatch

                return {"batch": CachedBatch(outcomes=tuple(_outs))}
            return {"batch": self.renderer.render_http_batch(jobs, backend)}
        return {"batch": self.renderer.render_batch(irs)}

    def _run_cloud_shots(self, picked, per_shot_artifacts, script_draft=None):
        """混合渲染（方案 C，2026-11-13）：关键镜走云端图生图，**角色用现有库锚点**。

        三条约定（用户明确要求）：
        1 **没有的锚点就现生**：镜头里出现但库里没有的角色 -> 用角色设计师现生成设定图
        2 **画风一致**：现生的设定图与关键镜都用同一个 `character_sheet_style`
        3 **云端并行**：远端请求并行（`workers`，默认 4）；失败逐条记录、不阻断

        - 默认关（`cloud_max_shots=0`）
        - **只替换不删除**：本地版仍在，可比对/回退
        """
        self._cloud_shots = {"enabled": False}
        limit = int(getattr(self, "cloud_max_shots", 0) or 0)
        if limit <= 0:
            return
        wd = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
        if not wd:
            return
        wd = Path(str(wd))
        style = str(getattr(self, "character_sheet_style", "") or "")
        try:
            from little_crew.runtime import cloud_policy as _cp
            from little_crew.runtime import cloud_render as _cr

            rows = _cp.plan(picked, max_cloud=limit)
            if not rows:
                self._cloud_shots = {"enabled": True, "planned": [],
                                     "note": "无关键镜需要上云端"}
                print("  混合渲染：无关键镜需要上云端（全部保留本地）")
                return

            # ---- 1) 现有库锚点 + 缺失角色现生（同一画风）----
            # 锚点：先 workdir，再退到**仓库角色库**（跨集复用；用户要求"角色用现有库里的"）
            anchors = _cr.load_anchors(wd)
            repo_anchors = _cr.load_anchors(Path(__file__).resolve().parents[3])
            for cid, pth in repo_anchors.items():
                anchors.setdefault(cid, pth)
            shot_by_id = {str(getattr(s, "shot_id", "")): s for s in picked}
            need_names: list[str] = []
            for r in rows:
                shot = shot_by_id.get(str(r.get("shotId") or ""))
                for nm in (getattr(shot, "characters", ()) or ()) if shot else ():
                    if not _cr._anchor_for([nm], anchors) and str(nm) not in need_names:
                        need_names.append(str(nm))
            born: list[dict] = []
            if need_names:
                print(f"  混合渲染：库里缺 {len(need_names)} 个角色锚点 -> 现生"
                      f"（画风：{style or '默认 3D 半国漫'}）")
                try:
                    from little_crew.agent.character_sheet import CharacterDesignerRules
                    from little_crew.runtime.character_sheets import CharacterSheetPainter

                    lib = CharacterDesignerRules().design(
                        script_draft if script_draft is not None else "",
                        episode_id=str(getattr(self, "episode_id", "")))
                    want = set(need_names)
                    subset = [s for s in lib.sheets
                              if str(getattr(s, "name", "")) in want
                              or str(getattr(s, "character_id", "")) in want]
                    if subset:
                        rep = CharacterSheetPainter(limit=len(subset)).paint(
                            subset, wd / "assets" / "characters", style=style)
                        born = rep.get("painted") or []
                        anchors = _cr.load_anchors(wd)
                        print(f"  混合渲染：现生 {len(born)} 张设定图"
                              + (f" · 失败 {len(rep.get('failed') or [])}"
                                 if rep.get("failed") else ""))
                except Exception as exc:                      # noqa: BLE001
                    print(f"  ⚠️ 现生角色设定失败（继续用无锚点模式）："
                          f"{type(exc).__name__}: {exc}")

            # ---- 2) 云端渲染（并行）----
            print(f"  混合渲染：{_cp.summarize(rows)}（锚点 {len(anchors)} 个 · 并行）")
            res = _cr.render_plan(picked, rows, anchors,
                                  wd / "assets" / "cloud_shots",
                                  model=str(getattr(self, "cloud_model", "") or "")
                                  or _cr.DEFAULT_MODEL,
                                  extra=(f"画风统一为：{style}" if style else ""))
            self._cloud_shots = {
                "enabled": True, "planned": rows, "anchors": sorted(anchors),
                "generatedAnchors": born, "max": limit,
                "rendered": res.get("rendered"), "failed": res.get("failed"),
                "workers": res.get("workers"), "seconds": res.get("seconds"),
                "records": res.get("records"),
            }
            for rec in res.get("records") or ():
                if rec.get("ok"):
                    sid = str(rec.get("shotId") or "")
                    per_shot_artifacts[sid] = [Path(str(rec["out"])).name]
            (_d := wd / "delivery").mkdir(parents=True, exist_ok=True)
            (_d / "cloud_shots.json").write_text(
                __import__("json").dumps(self._cloud_shots, ensure_ascii=False,
                                         indent=2, default=str), encoding="utf-8")
            print(f"  混合渲染：云端完成 {res.get('rendered')}/{len(rows)} 镜"
                  f" · {res.get('seconds')}s（并行 {res.get('workers')}）"
                  + (f" · ⚠️ 失败 {res.get('failed')}" if res.get("failed") else ""))
        except Exception as exc:                              # noqa: BLE001
            print(f"  ⚠️ 混合渲染未运行：{type(exc).__name__}: {exc}")
            self._cloud_shots["error"] = f"{type(exc).__name__}: {exc}"

    def _run_post_production(self, picked, *, engine: str = "") -> dict:
        """后期统筹：产出 `post.plan`（字幕/转场/BGM/音轨策略/响度）。

        **不阻断**：拿不到计划就用代码里的默认值（成片照出）。
        """
        self._post_plan = {}
        impls = getattr(self.orch, "_impls", {}) or {}
        if "post_production" not in impls:
            return {}
        try:
            out = self.orch.run_stage(
                "post_production", "post.plan",
                {"shot.list": list(picked),
                 "script.final": getattr(self, "_script_text_for_post", "") or "",
                 "nativeAudio": bool(getattr(self, "native_audio", False)),
                 "engine": engine})
        except Exception as exc:                              # noqa: BLE001
            print(f"  ⚠️ 后期统筹未运行：{type(exc).__name__}: {exc}")
            return {}
        plan = self.orch.blackboard.value("post.plan")
        if not isinstance(plan, dict):
            plan = ((getattr(getattr(out, "result", None), "output", None) or {})
                    .get("post.plan"))
        if not isinstance(plan, dict):
            return {}
        self._post_plan = plan
        wd = getattr(self, "_workdir", None) or getattr(self, "workdir", None)
        if wd:
            (_d := Path(str(wd)) / "delivery").mkdir(parents=True, exist_ok=True)
            (_d / "post.plan.json").write_text(
                __import__("json").dumps(plan, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8")
        sub = plan.get("subtitle") or {}
        print(f"  后期统筹：音轨 {plan.get('audioStrategy')} · "
              f"字幕约 {sub.get('estimatedCount')} 条（{sub.get('maxCharsPerLine')}字/行）· "
              f"响度目标 {plan.get('loudness', {}).get('targetLufs')} LUFS")
        return plan

    def _render_clip_stage(self, per_shot: Any, *,
                          on_progress: Any = None) -> dict[str, Any]:
        """`run()` 里的第二段：静帧 -> 片段 -> 拼成成片。

        `per_shot`：`{shotId: 产物}`（`run()` 里那份）。值可能是路径、
        字节或对象 —— 所以用 `_still_path()` 统一转成路径。

        ## 三重失败都不覆盖前一步的结论

        片段没出来，**静帧成片仍然有效** —— 所以这里只返回状态，
        不抛、不改 `ok`。但**失败必须看得见**（`{"ok": False, "reason": ...}`）。
        """
        try:
            # ⚠️ **开跑前先确认 ffmpeg 可用**（见 `_ffmpeg_status` 的说明）。
            # 一镜 331 秒 —— 绝不能让"缺 ffmpeg"在最后一步才暴露。
            _exe, _ok_ff, _why_ff = self._ffmpeg_status()
            if not _ok_ff:
                return {"ok": False, "reason": _why_ff, "clips": [],
                        "audio": {"ok": False, "reason": _why_ff}}

            stills: dict[str, Any] = {}
            for sid, art in (per_shot or {}).items():
                path = self._still_path(art)
                if path is not None:
                    stills[sid] = path
            if not stills:
                return {"ok": False, "reason": "没有可用的静帧路径"}

            self._clip_shots = list(getattr(self, "_shots_for_clips", ()) or ())
            prompts = dict(getattr(self, "_prompts_seen", {}) or {})
            res = self.render_video_stage(
                stills, prompts, seconds=self.video_seconds,
                on_progress=on_progress)
            if not res.get("ok"):
                return {"ok": False, "reason": res.get("reason") or "片段渲染失败",
                        "clips": list(res.get("clips") or {}),
                        "failures": res.get("failures") or []}

            # 落盘 + 拼成成片
            from little_crew.runtime.h3_video import concat_clips

            outdir = pathlib.Path(getattr(self, "_workdir", None) or ".")
            clipdir = outdir / "clips"
            clipdir.mkdir(parents=True, exist_ok=True)
            paths: list[pathlib.Path] = []
            for sid, blob in (res.get("clips") or {}).items():
                dst = clipdir / f"{sid}.mp4"
                dst.write_bytes(blob)
                paths.append(dst)
            if not paths:
                return {"ok": False, "reason": "片段为空"}

            full = clipdir / "full_clips.mp4"
            try:
                concat_clips(self._ffmpeg(), paths, full, workdir=clipdir)
            except Exception as e:                            # noqa: BLE001
                # ⚠️ **异常分支不要引用后面才赋值的变量**
                # （第一版在这里写 `"audio": audio`，而 `audio` 在下面才创建 ->
                #   `UnboundLocalError`，把 1.75 小时的整集跑挂在了最后一步。）
                return {"ok": False,
                        "reason": f"合片失败 {type(e).__name__}: {e}"[:160],
                        "clips": [str(x) for x in paths]}
            # 合片自身也查一次（-c copy 通常保住音轨，但**不假设**）
            audio = self._check_clip_audio(paths + [full])
            return {"ok": bool(audio.get("ok")), "path": str(full),
                    "clips": [str(x) for x in paths], "audio": audio,
                    "reason": ("" if audio.get("ok")
                               else str(audio.get("reason") or ""))}
        except Exception as e:                                # noqa: BLE001
            return {"ok": False, "reason": f"{type(e).__name__}: {e}"[:160]}

    def _check_clip_audio(self, paths: list[Any]) -> dict[str, Any]:
        """对**真片段**逐条检查音轨（V3，2026-10-18）。

        ## 为什么放在渲染之后，而不是门禁里

        门禁在静帧阶段跑，**那时片段还不存在** —— 没法检查"有没有音轨"。
        所以音轨检查必须在片段产出后**再走一遍**。

        ## 为什么判据是 `hard`

        M3 的定义是"成片**含音轨**"。没有音轨的片段 = M3 没达成。
        所以缺音轨**让交付失败**（`ok=False`），而不是只留个 warning。

        `_missing()` 的规矩照旧：**取不到观测 ≠ 通过**。
        """
        from little_crew.review.observe import measure_artifact
        from little_crew.review.ops import audio_track_present

        def resolve(name: str) -> Any:
            for q in paths:
                qp = pathlib.Path(str(q))
                if qp.name == name:
                    return qp
            return None

        rows: list[dict[str, Any]] = []
        for q in paths:
            qp = pathlib.Path(str(q))
            try:
                probe = measure_artifact(qp.name, resolve)
                verdict = audio_track_present({}, probe)
                rows.append({
                    "clip": qp.name,
                    "container": probe.get("container"),
                    "hasVideo": probe.get("hasVideo"),
                    "hasAudio": probe.get("hasAudio"),
                    "passed": bool(verdict.passed),
                    "detail": str(verdict.detail)[:160],
                })
            except Exception as e:                            # noqa: BLE001
                rows.append({"clip": qp.name, "container": None,
                             "hasVideo": None, "hasAudio": None,
                             "passed": False,
                             "detail": f"{type(e).__name__}: {e}"[:160]})
        failed = [r for r in rows if not r["passed"]]
        return {"ok": not failed, "clips": rows,
                "passed": len(rows) - len(failed), "failed": len(failed),
                "reason": ("" if not failed else
                           f"{len(failed)} 个片段缺音轨 —— M3 要求成片含音轨")}

    def _still_path(self, art: Any) -> Any:
        """把一镜的产物转成**文件路径**（H3 要的是一个真实文件）。

        ⚠️ `per_shot_artifacts[shot]` 是**列表**（产物引用），不是单个值。
        第一版只认单个值，于是每一镜都返回 None、全部被跳过，
        **而失败又被吞掉了** —— 表现是"视频模式开了但什么都没发生"。
        （2026-10-18 真机跑 105 秒就"跑通"，比预期 660 秒短得多，才露的馅。）

        认的顺序：序列 -> 第一个能用的；Path；`.path`；形如路径的字符串。
        """
        if art is None:
            return None
        # 列表/元组：逐个试，取第一个能转的
        if isinstance(art, (list, tuple)):
            for item in art:
                got = self._still_path(item)
                if got is not None:
                    return got
            return None
        if isinstance(art, pathlib.PurePath):
            return art
        cand = getattr(art, "path", None)
        if cand:
            return cand
        if isinstance(art, (str, bytes)):
            s = art.decode("utf-8", "replace") if isinstance(art, bytes) else art
            if not s or len(s) >= 400:
                return None
            # 有分隔符 -> 当路径直接给
            if "/" in s or "\\" in s:
                return s
            # ⚠️ **裸文件名必须解析成真实路径**（2026-10-18 真机事故）。
            #
            # 产物引用里存的是**文件名**（如 `f8781a7e_img_...png`），真文件在
            # 缓存的哈希分片目录里。直接把它交给 H3 会得到：
            #     RuntimeError: image_path 不存在：f8781a7e_img_....png
            # 因为那是相对**服务端 CWD** 找的，而文件根本不在那儿。
            #
            # 调用方已经提供了 `resolve_artifact`（demo 里就是 `_resolve`），
            # 用它把名字换成 Path —— **自己猜目录结构是不对的**。
            if s.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                resolver = getattr(self, "resolve_artifact", None)
                if callable(resolver):
                    try:
                        got = resolver(s)
                    except Exception:                         # noqa: BLE001
                        got = None
                    if got is not None:
                        return got
                return None
        return None

    def _ffmpeg_status(self) -> tuple[str, bool, str]:
        """解析 ffmpeg 并**确认它真的能跑**。返回 `(可执行值, 可用, 原因)`。

        ## 为什么要有这个（2026-10-18）

        `alpha.73` 那轮整集视频跑完 **677.9 秒**之后，合片才炸在一个
        import 名字上。这次排整集（≈2.3 小时）前做前置检查，又发现
        **ffmpeg 根本不在 PATH、`FFMPEG` 环境变量也是空的** ——
        若没查，那 2.3 小时会全部白花在最后一步。

        > **贵的动作之前，先跑便宜的检查。**
        > 这句话在上一次已经写过一遍了 —— 这次把它**写进代码**，
        > 而不是指望下次记得。

        所以 `_render_clip_stage` 在**渲染第一镜之前**就调这里：
        不可用就直接失败，一秒钟都不浪费。
        """
        import shutil as _shutil

        from little_crew.config import env_value

        try:
            cand = str(env_value("FFMPEG", "") or "").strip()
        except Exception:                                     # noqa: BLE001
            cand = ""
        if cand:
            if pathlib.Path(cand).is_file():
                return cand, True, ""
            if _shutil.which(cand):
                return cand, True, ""
            return cand, False, (f"FFMPEG={cand!r} 既不是文件也不在 PATH 里")
        found = _shutil.which("ffmpeg")
        if found:
            return found, True, ""
        return "ffmpeg", False, (
            "找不到 ffmpeg —— 设 `FFMPEG` 环境变量指向可执行文件，"
            "或把 ffmpeg 加进 PATH。**这条在开跑前就要过，"
            "不要等到 2 小时后的合片步骤。**")

    def _ffmpeg(self) -> Any:
        """取 ffmpeg 可执行文件。**不猜路径就报错**，不静默用系统 PATH。"""
        return self._ffmpeg_status()[0]

    # ---- V1：video_mode 的第二段（2026-10-18）--------------------------
    #
    # ## 为什么是"第二段"而不是"整个 video_mode"
    #
    # `minimax-h3` 的 `body_template` **强制要 `image_path`** —— 它是图生视频。
    # 所以主流程要出视频，必须两段：先出静帧，再动画化。
    # 而 `_values(ir)` 在渲染前拿不到 `image_path`（那时静帧还不存在）。
    #
    # **所以 V1 只做第二段**：吃**已有**的静帧，把它们动画化。
    # 这样成本可控（2 镜 ≈ 12 分钟），且不触碰已跑通的静帧链路。
    #
    # 设计见 `docs/00-项目策划书.md` §5.10.15.1。

    def render_video_stage(
        self,
        stills: Mapping[str, Any],
        prompts: Mapping[str, str],
        *,
        seconds: float = 2.0,
        steps: int = 8,
        seed_base: int = 0,
        width: int = 640,
        height: int = 384,
        on_progress: Any = None,
    ) -> dict[str, Any]:
        """把**已有静帧**动画化成片段（`video_mode` 的第二段）。

        `stills`：`{shotId: 静帧路径}`；`prompts`：`{shotId: 提示词}`。

        返回：`{"ok", "clips": {shotId: bytes}, "failures", "backend", "reason"}`。

        ## 失败语义（这里最容易写错）

        | 情况 | 返回 |
        |---|---|
        | 拿不到本地后端 | `ok=False`，`reason` 说明**不退回付费 API** |
        | 全部镜头成功 | `ok=True` |
        | 部分成功 | `ok=False`，**但 `clips` 保留成功的** —— 半成品有用 |
        | 缺静帧或缺提示词 | 跳过该镜并记进 `failures`（**不静默丢**） |
        """
        # ---- 摄影职位（`camera_operator`，2026-11-08 接）----
        #
        # 名册声明 `shot.image -> shot.video`。默认关闭（`LITTLE_CREW_CLIPS_AGENT=1`
        # 才走），因为现有 H3 路径带提示词/重试/硬上限，换掉风险大。
        # 走职位时把**文件读成 bytes**，与下面 `clips: {shotId: bytes}` 的契约一致。
        import os as _os_ca

        if _os_ca.environ.get("LITTLE_CREW_CLIPS_AGENT") == "1" and \
                "camera_operator" in (getattr(self.orch, "_impls", {}) or {}):
            try:
                _outdir = Path(str(getattr(self, "_workdir", None) or ".")) / "clips"
                _ca = self.orch.run_stage(
                    "camera_operator", "shot.video",
                    {"shot.list": list(getattr(self, "_clip_shots", ()) or ()),
                     "images": dict(stills), "outDir": str(_outdir)})
                if getattr(_ca, "ok", False):
                    _cv = self.orch.blackboard.value("shot.video") or {}
                    _clips, _fails = {}, []
                    for _c in (_cv.get("clips") or ()):
                        try:
                            _clips[str(_c["shotId"])] = Path(str(_c["path"])).read_bytes()
                        except Exception:                      # noqa: BLE001
                            _fails.append({"shotId": str(_c.get("shotId")),
                                           "reason": "读不到片段文件"})
                    if _clips:
                        print(f"  摄影（camera_operator）：{len(_clips)} 段")
                        return {"ok": not _fails, "clips": _clips,
                                "failures": _fails,
                                "backend": _cv.get("backend", ""),
                                "reason": "" if not _fails else "部分片段失败"}
            except Exception as _e_ca:                          # noqa: BLE001
                print(f"  ⚠️ camera_operator 未运行：{type(_e_ca).__name__}: {_e_ca}")

        from little_crew.runtime.h3_video import (
            VideoShot,
            h3_backend,
            render_videos,
        )

        backend = h3_backend()
        if backend is None:
            # ⚠️ **绝不静默退回付费 API**（`alpha.52` 的不变式）。
            # 否则"本地服务没起"会变成"账单悄悄变了"。
            return {"ok": False, "clips": {}, "failures": [],
                    "backend": None,
                    "reason": ("拿不到本地 H3 后端 —— 本地服务未起或通道不可用。"
                               "**不退回付费 API。**")}

        shots: list[Any] = []
        skipped: list[dict[str, Any]] = []
        for sid, path in (stills or {}).items():
            prompt = str((prompts or {}).get(sid) or "").strip()
            if not prompt:
                skipped.append({"shotId": sid, "why": "没有提示词"})
                continue
            if path is None:
                skipped.append({"shotId": sid, "why": "没有静帧"})
                continue
            shots.append(VideoShot(shot_id=sid, prompt=prompt,
                                   image_path=path, width=width, height=height))
        if not shots:
            return {"ok": False, "clips": {}, "failures": skipped,
                    "backend": {"endpoint": backend.endpoint},
                    "reason": "没有可动画化的镜头（缺静帧或缺提示词）"}

        ok, failures = render_videos(
            self.renderer, backend, shots, seconds=seconds, steps=steps,
            seed_base=seed_base, on_progress=on_progress)
        return {
            "ok": bool(ok) and not failures,
            "clips": dict(ok or {}),
            "failures": list(failures or ()) + skipped,
            "backend": {"endpoint": backend.endpoint, "path": backend.path},
            "reason": ("" if (ok and not failures)
                       else f"{len(failures or ())} 个镜头失败"),
        }

    def _run_designer(self, script: Any) -> dict[str, Any]:
        """跑**美术指导**产出 `art.bible`，并注入分镜师。

        与 `_run_gaffer` 同一套模式；顺序在**灯光之前**（美术定空间、灯定光）。

        失败不阻断（没有美术设定，分镜照样能切）。
        """
        impls = getattr(self.orch, "_impls", {}) or {}
        if "production_designer" not in impls:
            return {}
        try:
            out = self.orch.run_stage("production_designer", "scene.design",
                                     {"script.draft": script})
        except Exception as e:                                # noqa: BLE001
            return {"ok": False, "reason": f"{type(e).__name__}: {e}"[:160]}
        if not getattr(out, "ok", False):
            return {"ok": False, "reason": str(getattr(out, "error", ""))[:160]}
        bible = self.orch.blackboard.value("art.bible")
        if not isinstance(bible, Mapping):
            return {"ok": False, "reason": "美术指导没有产出 art.bible"}
        sb = impls.get("storyboard_artist")
        if sb is not None and hasattr(sb, "art"):
            try:
                sb.art = dict(bible)
            except Exception:                                 # noqa: BLE001
                pass
        return {"ok": True, "bible": dict(bible)}

    def _run_line_producer(self) -> dict[str, Any]:
        """跑**制片主任**产出 `production.schedule`（**分镜之后**才跑）。

        它消费的是**镜头表**，所以必须在分镜产出后排。
        产物进 `EpisodeResult.schedule`，**不影响交付判定** ——
        它是"这片要花多少"的前置预估，不是质量门禁。
        """
        impls = getattr(self.orch, "_impls", {}) or {}
        if "line_producer" not in impls:
            return {}
        shot_list = self.orch.blackboard.value("shot.list")
        if shot_list is None:
            return {"ok": False, "reason": "没有镜头表 —— 无法排期"}
        try:
            out = self.orch.run_stage("line_producer", "production.schedule",
                                     {"shot.list": shot_list})
        except Exception as e:                                # noqa: BLE001
            return {"ok": False, "reason": f"{type(e).__name__}: {e}"[:160]}
        if not getattr(out, "ok", False):
            return {"ok": False, "reason": str(getattr(out, "error", ""))[:160]}
        sched = self.orch.blackboard.value("production.schedule")
        return {"ok": True, "schedule": dict(sched) if isinstance(sched, Mapping) else {}}

    def _run_director(self, script: Any) -> dict[str, Any]:
        """跑**导演**产出 `episode.plan`，并把它交给分镜师。

        ## 为什么这是"职位真正进入协作链"的关键

        在此之前 `episode.plan` 虽然产出了，但**没有下游消费** ——
        它只是一份没人看的文档。而分镜师自己决定每场几个镜头，
        于是出现过"一场只给一个镜头"。

        三步：
          1. `run_stage("director", "episode.direct", {"script.draft": ...})`
          2. 从黑板取 `episode.plan`
          3. **注入分镜师的 `plan` 属性**（它有就设，没有就跳过）

        ## 失败不阻断

        导演挂了只是没有预算 —— 分镜可以按"每场 ≥2 镜"独立工作。
        **但失败要看得见**（返回的 dict 里 `ok=False`），否则
        "没有预算"与"预算为空"就分不清了。
        """
        impls = getattr(self.orch, "_impls", {}) or {}
        if "director" not in impls:
            return {}
        try:
            out = self.orch.run_stage("director", "episode.direct",
                                     {"script.draft": script})
        except Exception as e:                                # noqa: BLE001
            return {"ok": False, "reason": f"{type(e).__name__}: {e}"[:160]}
        if not getattr(out, "ok", False):
            return {"ok": False,
                    "reason": str(getattr(out, "error", ""))[:160]}

        plan = self.orch.blackboard.value("episode.plan")
        if not isinstance(plan, Mapping):
            return {"ok": False, "reason": "导演没有产出 episode.plan"}

        # 注入分镜师（它有 plan 属性才设；别硬塞陌生属性）
        sb = impls.get("storyboard_artist")
        if sb is not None and hasattr(sb, "plan"):
            try:
                sb.plan = dict(plan)
            except Exception:                                 # noqa: BLE001
                pass
        return {"ok": True, "plan": dict(plan)}

    def _run_gaffer(self, script: Any) -> dict[str, Any]:
        """跑**灯光师**产出 `lighting.plan`，并注入分镜师。

        与 `_run_director` 同一套模式：

          1. `run_stage("gaffer", "lighting.plan", ...)`
          2. 从黑板取 `lighting.plan`
          3. 注入分镜师的 `lighting` 属性（有就设）

        **顺序**：导师之后、分镜之前 —— 光要影响分镜的画面描述。
        失败不阻断（没光方案分镜照样能切）。
        """
        impls = getattr(self.orch, "_impls", {}) or {}
        if "gaffer" not in impls:
            return {}
        try:
            out = self.orch.run_stage("gaffer", "lighting.plan",
                                     {"script.draft": script})
        except Exception as e:                                # noqa: BLE001
            return {"ok": False, "reason": f"{type(e).__name__}: {e}"[:160]}
        if not getattr(out, "ok", False):
            return {"ok": False, "reason": str(getattr(out, "error", ""))[:160]}

        plan = self.orch.blackboard.value("lighting.plan")
        if not isinstance(plan, Mapping):
            return {"ok": False, "reason": "灯光师没有产出 lighting.plan"}
        sb = impls.get("storyboard_artist")
        if sb is not None and hasattr(sb, "lighting"):
            try:
                sb.lighting = dict(plan)
            except Exception:                                 # noqa: BLE001
                pass
        return {"ok": True, "plan": dict(plan)}

    def _run_advisory(self, script: Any, *, on_progress: Any = None
                      ) -> tuple[dict[str, Any], ...]:
        """跑**顾问批评家**：意见进报告，**不进裁定**。

        ## 为什么单独一个方法，而不是把它们加进 `_REVIEWERS_BY_ARTIFACT`

        加了就变成门禁审核者，而 `merge_reviews` 对多个审核者取**各维度最低分** ——
        于是加 3 个未标定的批评家会让"通过与否"取决于哪一个更爱挑刺。
        **那不是严谨，是随机。**

        所以先让它们产出真实意见、量命中率，再决定升不升门禁。
        这与 `PASS_THRESHOLD` 的标定是同一套纪律：**先有分布，再有权重。**

        ## 失败不阻断

        顾问挂了只是少一份意见 —— 不该让整集停下。
        但**失败要记下来**（`verdict="error"`），否则"顾问没意见"与
        "顾问没跑"就分不清了。
        """
        ids = tuple(getattr(self, "advisory_critics", ()) or ())
        if not ids or script is None:
            return ()
        try:
            from little_crew.agent.critic_llm import CriticLlm
        except Exception:                                     # noqa: BLE001
            return ()

        # **用项目已有的渲染器**：`script_text()` 把 ScriptDraft 摊成可读文本，
        # 与定妆表用的是同一套（`alpha.40` 修过它丢 characters 的问题）。
        text = ""
        try:
            from little_crew.agent.character_sheet import script_text
            text = str(script_text(script) or "")
        except Exception:                                     # noqa: BLE001
            text = ""
        if not text.strip():
            for attr in ("as_text", "to_text"):
                v = getattr(script, attr, None)
                if callable(v):
                    try:
                        text = str(v())
                    except Exception:                         # noqa: BLE001
                        text = ""
                    if text.strip():
                        break
        if not text.strip():
            text = str(getattr(script, "text", "") or "")

        # ⚠️ **正文为空 -> 这不是"通过"，是"没审成"**（2026-10-18 实测）。
        #
        # 第一版没取到正文（`ScriptDraft` 没有 `as_text`），把空串喂给批评家，
        # 三个顾问齐刷刷返回 `PASS 100.0` —— **看起来全部合格，
        # 实际一个字都没看**。这正是"未检查 ≠ 已通过"的反面教材。
        # ⚠️ **判据要按类型与结构，不能只看"字符串空不空"。**
        #
        # 第一版只判 `text.strip()`，而 `script_text()` 对一个不相干对象
        # 会返回**非空的** repr —— 守卫没触发，批评家照样拿到垃圾文本。
        # 所以再叠一层：**不是 `ScriptDraft` 或没有场次，就不算可审。**
        try:
            from little_crew.agent.messages import ScriptDraft
            is_draft = isinstance(script, ScriptDraft)
            has_scenes = bool(getattr(script, "scenes", None))
        except Exception:                                     # noqa: BLE001
            is_draft, has_scenes = True, True
        if not text.strip() or not is_draft or not has_scenes:
            return tuple({"position": rid, "verdict": "error",
                          "reason": ("取不到可审的剧本正文 —— "
                                     "**未审查，不算通过**"),
                          "issues": [], "total": 0.0} for rid in ids)

        from little_crew.agent.protocol import Budget, CallEnvelope

        out: list[dict[str, Any]] = []
        for rid in ids:
            if on_progress is not None:
                try:
                    on_progress("advisory", rid, 0)
                except Exception:                             # noqa: BLE001
                    pass
            try:
                critic = CriticLlm(rid)
                env = CallEnvelope.root(
                    rid, critic.capability,
                    {critic.artifact_key: text}, Budget.unlimited(),
                    callee_kind="agent")
                res = critic(envelope=env, runtime=None)
            except Exception as e:                            # noqa: BLE001
                out.append({"position": rid, "verdict": "error",
                            "reason": f"{type(e).__name__}: {e}"[:160],
                            "issues": [], "total": 0.0})
                continue
            if not res.ok:
                out.append({"position": rid, "verdict": "error",
                            "reason": str(res.error_message)[:160],
                            "issues": [], "total": 0.0})
                continue
            fb = res.output.get(critic.capability)
            if fb is None:
                out.append({"position": rid, "verdict": "error",
                            "reason": "没有产出意见", "issues": [], "total": 0.0})
                continue
            out.append({
                "position": rid,
                "verdict": fb.verdict.value,
                "total": round(float(fb.score.total), 1) if fb.score else 0.0,
                "issues": [
                    {"severity": i.severity.value, "problem": i.problem[:200]}
                    for i in fb.issues[:8]],
            })
        return tuple(out)

    def _run_gate(self, *, batch: Any, picked: Sequence[Any],
                  irs: Sequence[Mapping[str, Any]]) -> Any:
        """对渲染产物跑验收门禁。

        返回 `ShotGateReport`；`verify_artifacts=False` 时返回 `None`
        （门禁关闭，证据里 `gate` 为空 —— 与"门禁全过"可区分）。

        **约束从哪来**：各镜自己的 IR（`ir["constraints"]`）。
        这正是 C1 那句"约束边不编译，而是生成可执行断言"的兑现点 ——
        约束跟着镜头走，所以门禁天然是**逐镜**的。
        """
        if not self.verify_artifacts:
            return None
        try:
            from little_crew.review import GateRunner
        except Exception:                                     # noqa: BLE001
            return None

        constraints: dict[str, list[Mapping[str, Any]]] = {}
        #: 镜头 id -> 该镜的 IR。返工规划器要用它判"动作会不会真的改变 IR"，
        #: 否则「把期望值写回已经是期望值的字段」就是恒等写入（审计 M-26）。
        ir_by_shot: dict[str, Mapping[str, Any]] = {}
        for s, ir in zip(picked, irs):
            constraints[s.shot_id] = list(ir.get("constraints") or ())
            ir_by_shot[s.shot_id] = ir

        # 产物按**镜头 id** 组织。`batch.outcomes` 的 graphId 形如
        # `G-{episode}-{shotId}`，这里减掉前缀还原成 shotId ——
        # 因为约束是按 shotId 记的，两边必须用同一个键。
        #
        # ⚠️ **直接把 `o.artifacts` 原样递过去，不要 `list(...)`。**
        # `CacheOutcome.artifacts` 是 **`{文件名: bytes}`** ——
        # 字节就在里面，门禁可以直接从内存量尺寸/字节数。
        # 早期写成 `list(...)` 只取了**键**（文件名字符串），
        # 于是 `measure_artifact` 拿不到字节、也解析不到文件，
        # 结果 `resolution_matches` 被判"缺观测 width/height"而**跳过** ——
        # **门禁接了却什么都不检查，且不报任何错。**
        prefix = f"G-{self.episode_id}-"
        artifacts: dict[str, Any] = {}
        for o in getattr(batch, "outcomes", ()):
            gid = str(getattr(o, "graph_id", ""))
            sid = gid[len(prefix):] if gid.startswith(prefix) else gid
            artifacts[sid] = getattr(o, "artifacts", None) or {}

        if not artifacts:
            return None
        # 传 resolve —— 产物引用上的尺寸/字节数实测为空，
        # 不传的话分辨率类约束会全被跳过（见 observe.py）。
        # 来自**镜头计划**的观测 —— 产物文件里量不出来的那些。
        # 不传的话 `count_at_most(field="characterCount")` 会被判"缺观测"而跳过。
        context: dict[str, Mapping[str, Any]] = {}
        for s in picked:
            entry: dict[str, Any] = {
                "characterCount": len(getattr(s, "characters", ()) or ()),
                # ⚠️ **键名必须是 `durationSec`**：这是 `review/observe.py` 的
                # `OPERATOR_NEEDS["duration_within"]` 要的名字。
                # 它在 2026-10 之前叫 `plannedDurationSec`，于是
                # `need - avail` 恒非空 -> 每条 `duration_within`（hard！）
                # 被**静默跳过**，报告里只写「未执行：缺观测」——
                # 看起来像"本机能力不足"，实际是字段名拼错。
                # 那条约束是防 H3 OOM 的硬上限，等于从未生效过。
                "durationSec": float(
                    getattr(s, "duration_sec", 0.0) or 0.0),
                #: `cast_consistent_with_text` 要的两个字段（2026-10-17）。
                # 它们是**文字层**的，产物文件里量不出来 ——
                # 只能从镜头计划带进来。
                "characters": list(getattr(s, "characters", ()) or ()),
                "description": str(getattr(s, "description", "") or ""),
            }
            # ---- L3：跨模型族盲读（C3 的第三层）----
            #
            # ⚠️ **只在真的需要时才调**（IR 里有盲读约束）。
            # 盲读要跑一次视觉模型，代价远高于 L1/L2 的纯计算；
            # 无条件调的话，"跑一集"会平白多出 N 次模型调用。
            if self.blind_reader is not None and self._wants_blind_read(
                    constraints.get(s.shot_id)):
                entry.update(self.blind_reader.observations_for(
                    s.shot_id, artifacts.get(s.shot_id)))
            context[s.shot_id] = entry
        return GateRunner().run(constraints_by_shot=constraints,
                                artifacts_by_shot=artifacts,
                                resolve=self.resolve_artifact,
                                context_by_shot=context,
                                ir_by_shot=ir_by_shot)

    # ---- 返工闭环 ------------------------------------------------------

    @staticmethod
    def _wants_blind_read(constraints: Any) -> bool:
        """这一镜的约束里有没有盲读类的。

        **按约束决定要不要付出模型调用的代价** —— 与 L2 一致性子同一个思路
        （那边是"有 `similarity` 类约束才算签名"）。
        """
        for c in (constraints or ()):
            a = c.get("assertion") if isinstance(c, Mapping) else None
            op = a.get("op") if isinstance(a, Mapping) else c.get("op")
            if op == "blind_read_verdict":
                return True
        return False

    def _repair_loop(self, *, gate_report: Any, batch: Any,
                     irs: list[dict[str, Any]], picked: Sequence[Any]
                     ) -> tuple[Any, Any, list[str], list[dict[str, Any]]]:
        """门禁判 RETRY -> 改 IR -> 只重跑那几镜 -> 复检。最多 N 轮。

        **三道防烧 GPU 的闸**（缺任一道，循环都可能白烧算力）：

        | 闸 | 作用 |
        |---|---|
        | `max_repair_rounds` | 硬上限。没有它，门禁不退让就会无限重跑 |
        | `repairable_shots()` | 动作**全不可应用**的镜头不重跑 —— 跑了也是同一张图 |
        | **IR 哈希必须变** | 没变就是命中缓存，重跑等于空转 |

        最后一道尤其重要：**它是"循环真的前进了"的唯一判据**。
        改 IR 是唯一能让哈希变的手段，所以返工动作必须是"对 IR 的编辑"。
        """
        from little_crew.review.repair import apply_repairs
        from little_crew.ir.canonical import content_hash

        log: list[str] = []
        #: 循环里**实际应用过**的动作（含轮次与镜头 id）。
        #:
        #: ⚠️ 必须单独累积：循环结束时最终裁定是 KEEP，
        #: 那里面**没有任何动作**（都修好了）——
        #: 而"当初改了什么"恰恰是审查方要追溯的东西。
        applied: list[dict[str, Any]] = []
        idx = {s.shot_id: i for i, s in enumerate(picked)}

        for round_no in range(1, self.max_repair_rounds + 1):
            if gate_report is None:
                break
            targets = gate_report.repairable_shots()
            if not targets:
                # 没有可修的镜头 —— **明确说出来**，不要静默结束。
                # 静默结束会让"循环跑了但没修任何东西"看起来像"循环成功"。
                pending = [r.artifact_id for r in gate_report.results
                           if r.verdict.value == "RETRY"]
                if pending:
                    log.append(
                        f"第 {round_no} 轮：{len(pending)} 镜需返工，"
                        f"但**没有可应用的动作**（能力缺失，不是参数问题）—— "
                        f"不重跑，如实上报")
                break

            log.append(f"第 {round_no} 轮：重跑 {len(targets)} 镜 —— "
                       f"{', '.join(targets)}")

            # 记下**这一轮实际用到的动作** —— 循环跑完后最终裁定是 KEEP，
            # 里面就没有动作了，而"当初改了什么"必须留在证据里。
            for sid in targets:
                for act in gate_report.actions_for(sid):
                    if getattr(act, "applicable", False):
                        applied.append({
                            "round": round_no, "shotId": sid,
                            **act.as_dict(),
                        })

            # ---- 改 IR ----
            changed: list[int] = []
            for sid in targets:
                i = idx.get(sid)
                if i is None:
                    continue
                actions = gate_report.actions_for(sid)
                new_ir, alog = apply_repairs(irs[i], actions)
                for line in alog:
                    log.append(f"  {sid}: {line}")
                # **哈希必须变** —— 没变说明动作没实际改到东西
                try:
                    before = content_hash(irs[i])
                    after = content_hash(new_ir)
                except Exception:                             # noqa: BLE001
                    before = after = ""
                if before and before == after:
                    log.append(f"  {sid}: ⚠️ IR 未变化 —— 重跑会命中缓存，"
                               f"跳过（这是「动作没改到东西」的信号）")
                    continue
                irs[i] = new_ir
                changed.append(i)

            if not changed:
                log.append(f"第 {round_no} 轮：没有任何镜头的 IR 发生变化 —— "
                           f"停止（继续只会空转）")
                break

            # ---- 只重跑改过的那几镜 ----
            sub_irs = [irs[i] for i in changed]
            rstats = self._render(sub_irs)
            sub_batch = rstats["batch"]

            # ---- 把重跑结果合并回总批次 ----
            #
            # **按 graphId 对齐**：重跑用的还是同一批 IR（只是内容改了），
            # 所以 graphId 不变，可以直接替换。
            from little_crew.runtime.cached_render import CachedBatch

            by_gid = {str(getattr(o, "graph_id", "")): o
                      for o in self._outcomes_of(sub_batch)}
            merged = [by_gid.get(str(getattr(o, "graph_id", "")), o)
                      for o in self._outcomes_of(batch)]
            batch = CachedBatch(outcomes=tuple(merged))

            # ---- 复检 ----
            gate_report = self._run_gate(batch=batch, picked=picked, irs=irs)

        # ⚠️ **`irs` 不用返回** —— 它是 list，上面的改动是**原地**的，
        # 调用方看得到。而 `batch` 是**重新赋值**，**必须**返回：
        # 局部变量绑定不会传播回调用方。
        #
        # 这个区别踩过坑：早期签名返回了 `batch` 但调用方**没有接**，
        # 于是调用方拿到的还是**首跑**的 batch ——
        # 产物清单指向旧图、门禁报告来自新裁定，**两者对不上**，
        # 而且不报任何错。（日志丢空也是同一个原因：
        # `repair_log` 只被写进了函数内的局部 `gate_dict`。）
        return gate_report, batch, log, applied

    def engine_backend(self) -> Any:
        """HTTP 引擎的后端声明；返回 ``None`` 表示走编译路径（ComfyUI）。"""
        return None

    def _values(self, ir: Mapping[str, Any]) -> dict[str, Any]:
        """从 IR 里取出 HTTP 通道需要的入参。

        用**实例上**的步数与种子，而不是写死的常量 ——
        否则调用方改了 `engine_steps` 却不生效，而参数不生效比报错更难发现。

        ⚠️ 2026-11-12：这里**改走 IR 出口编译器**（`compiler/exits.py`）。
        以前是按节点 id 硬取字段（`n_prompt`/`n_neg`/`n_latent`）——
        那等于"HTTP 路径不经过编译"，节点改名或 IR 结构一变就静默取空，
        而且编译器的 notices（控制图/IPAdapter 不被支持）根本传不出来。
        现在：能编译就用编译结果；编译不了退回旧取值并**打印原因**。
        """
        def param(nid: str, key: str) -> Any:
            return next(n for n in ir["nodes"] if n["id"] == nid)["params"][key]

        _fallback = {"prompt": param("n_prompt", "text"),
                     "negative_prompt": param("n_neg", "text"),
                     "width": param("n_latent", "width"),
                     "height": param("n_latent", "height"),
                     "steps": self.engine_steps, "seed": self.engine_seed}
        try:
            from little_crew.compiler.exits import compile_ir

            out = compile_ir(ir, None, kind="image", seed=self.engine_seed)
        except Exception as exc:                              # noqa: BLE001
            print(f"  ⚠️ IR 出口编译异常（退回旧取值）：{type(exc).__name__}: {exc}")
            return _fallback
        if not out.ok:
            print(f"  ⚠️ IR 出口编译失败（退回旧取值）：{out.error}")
            return _fallback
        # 只回旧的那 6 个键：多带键会改变幂等哈希，让已有缓存全部失效
        values = {k: out.params[k] for k in
                  ("prompt", "negative_prompt", "width", "height")}
        values["steps"] = self.engine_steps
        values["seed"] = self.engine_seed
        # notices 只提示一次（一集 40 镜会刷屏）
        for note in out.notices:
            if note not in getattr(self, "_exit_notes_shown", set()):
                self._exit_notes_shown = getattr(self, "_exit_notes_shown", set()) | {note}
                print(f"  ℹ️ IR 出口：{note}")
        return values

    #: 供调用方覆盖的种子（实例级）
    def with_seed(self, seed: int) -> "EpisodeRunner":
        self.engine_seed = int(seed)
        return self
