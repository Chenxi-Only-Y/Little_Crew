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
名册声明 ``assembly_editor`` 产出 ``episode.master`` / ``delivery.evidence``，
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "delivery.evidence",
            "episodeId": self.episode_id,
            "artifactCount": len(self.artifacts),
            "artifacts": list(self.artifacts),
            "irHashes": dict(self.ir_hashes),
            "cache": {
                "hits": self.cache_hits, "rendered": self.rendered,
                "secondsSaved": round(self.seconds_saved, 2),
                "secondsSpent": round(self.seconds_spent, 2),
            },
            "checks": dict(self.checks),
            "allChecksPassed": self.all_checks_passed,
            "gate": dict(self.gate),
            "gatePassed": self.gate_passed,
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
    if result is not None:
        _dump("run.report.json", result)
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
        #: 场景主体冲突（分镜越权引入了剧本没有的场景主体）
        self.scene_conflicts: list[dict] = []
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
        if self.stop_after and stage == self.stop_after:
            raise StopRequested(f"停在「{stage}」（--stop-after）")

    def run(self, source: str, *, limit: int | None = None,
            on_progress: Any = None) -> EpisodeResult:
        t0 = time.perf_counter()

        # ---- 1) 剧本闭环 ----
        #: `--limit` 的值。白模与渲染都要用它，否则两者看的镜头不一致。
        self._limit = int(limit or 0)
        self._mark("script")
        cycle = self.orch.run_script_cycle(
            {"source.novel": source, "episodeId": self.episode_id,
             "title": self.title})
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
        self._mark("art")
        # 灯光师在导演之后（参考基调）、分镜之前（光要进画面描述）
        lighting_info = self._run_gaffer(script_draft)
        self._mark("lighting")

        # ---- 2) 分镜 ----
        self._mark("storyboard")
        # （制片主任在分镜产出后跑 —— 它消费镜头表）
        out = self.orch.run_stage("storyboard_artist", "shot.list",
                                  {"script.final": script_draft})
        if not out.ok:
            return EpisodeResult(
                episode_id=self.episode_id, ok=False,
                script_rounds=cycle.round_count,
                    failure_kind="storyboard-failed",
                reason=f"分镜阶段失败：{out.error}",
                wall_seconds=time.perf_counter() - t0)

        shot_list = self.orch.blackboard.value("shot.list")
        shots = tuple(shot_list.shots)
        planned = len(shots) if limit is None else min(len(shots), int(limit))
        picked = shots[:planned]

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
            for _s in picked:
                _desc = str(getattr(_s, "description", "") or "")
                _r = check_scene_subject(_script_text, _desc)
                if not _r["ok"]:
                    self.scene_conflicts.append(
                        {"shotId": getattr(_s, "shot_id", ""), **_r})
        except Exception as _e:                              # noqa: BLE001
            print(f"  ⚠️ 场景主体检查未运行：{type(_e).__name__}: {_e}")
        if self.scene_conflicts:
            # **按严重度分开报**：越权新增 = 协作冲突（该退）；丢场景 = 特写的常态（不该退）
            _err = [c for c in self.scene_conflicts if c.get("severity") == "error"]
            _warn = [c for c in self.scene_conflicts if c.get("severity") == "warn"]
            if _err:
                _f = _err[0]
                print(f"  ⚠️ 场景越权 {len(_err)} 镜"
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
        previs_info = self._run_previs(picked)
        self._mark("previs_done")

        # ⚠️ **桥接必须在白模预演之后**（2026-10-22 修）。
        #
        # 原来桥接（IR 编译）在预演之前，于是 `_control_frame_for()` 拿到的
        # 永远是 `None` —— **控制图再也不会被编进施工单**。
        # 这不是「后端不支持」，是「控制图还没生成，施工单就编好了」。
        # 表现是崩溃（`_stage_control_image(None)`），真因在顺序。
        self._prompts_seen: dict[str, str] = {}
        irs: list[dict[str, Any]] = [self._bridge(s) for s in picked]
        _prompts = self._prompts_seen
        if limit is not None:
            irs = irs[:planned]
        # ---- 4) 渲染 ----
        self._mark("render_start")
        rstats = self._render(irs, on_progress=on_progress)
        batch = rstats["batch"]

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
        for o in batch.outcomes:
            # 产物名以缓存层为准（它保证唯一）
            artifacts[o.graph_id] = sorted(o.artifacts)

        # ---- 5) 合成 ----
        self._mark("assemble")
        ir_hashes: dict[str, str] = {}
        for (s, ir) in zip(picked, irs):
            try:
                from little_crew.ir.canonical import content_hash

                ir_hashes[s.shot_id] = content_hash(ir)
            except Exception:                                 # noqa: BLE001
                ir_hashes[s.shot_id] = ""

        per_shot_artifacts = {
            s.shot_id: artifacts.get(f"G-{self.episode_id}-{s.shot_id}", [])
            for s in picked
        }

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
    #: 例如 `("continuity_supervisor", "compliance_officer", "pacing_editor")`。
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
        ir = shot_to_ir(
            text, episode_id=self.episode_id,
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
            jobs = [(self._values(ir), ir["graphId"]) for ir in irs]
            return {"batch": self.renderer.render_http_batch(jobs, backend)}
        return {"batch": self.renderer.render_batch(irs)}

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
                      for o in sub_batch.outcomes}
            merged = [by_gid.get(str(getattr(o, "graph_id", "")), o)
                      for o in batch.outcomes]
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
        """
        def param(nid: str, key: str) -> Any:
            return next(n for n in ir["nodes"] if n["id"] == nid)["params"][key]

        return {"prompt": param("n_prompt", "text"),
                "negative_prompt": param("n_neg", "text"),
                "width": param("n_latent", "width"),
                "height": param("n_latent", "height"),
                "steps": self.engine_steps, "seed": self.engine_seed}

    #: 供调用方覆盖的种子（实例级）
    def with_seed(self, seed: int) -> "EpisodeRunner":
        self.engine_seed = int(seed)
        return self
