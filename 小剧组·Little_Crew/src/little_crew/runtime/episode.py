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
                 gate: Mapping[str, Any] | None = None
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

class EpisodeRunner:
    """把一集从头跑到尾。

    **它不做决策** —— 决策由各职位做，它只负责"按顺序把它们串起来，
    并把产出记录下来"。这与 :class:`CrewOrchestrator` 的分工一致：
    编排器管协作，本类管**交付**。
    """

    def __init__(self, orchestrator: Any, renderer: Any, *,
                 episode_id: str = "E001", title: str = "",
                 checkpoint: Any = None) -> None:
        self.orch = orchestrator
        self.renderer = renderer
        self.episode_id = episode_id
        self.title = title
        self.checkpoint = checkpoint
        self.assembler = AssemblyEditorRules()
        #: 本集的角色定妆表（文字半）。跑完 run() 后可用。
        self.characters: Any = None

    # ---- 主流程 --------------------------------------------------------

    def run(self, source: str, *, limit: int | None = None,
            on_progress: Any = None) -> EpisodeResult:
        t0 = time.perf_counter()

        # ---- 1) 剧本闭环 ----
        cycle = self.orch.run_script_cycle(
            {"source.novel": source, "episodeId": self.episode_id,
             "title": self.title})
        if not cycle.accepted:
            return EpisodeResult(
                episode_id=self.episode_id, ok=False,
                script_rounds=cycle.round_count,
                script_verdict=(cycle.rounds[-1].verdict.value
                                if cycle.rounds else ""),
                reason=f"剧本阶段未通过：{cycle.reason}",
                wall_seconds=time.perf_counter() - t0)

        # ---- 2) 分镜 ----
        out = self.orch.run_stage("storyboard_artist", "shot.list",
                                  {"script.final": cycle.final})
        if not out.ok:
            return EpisodeResult(
                episode_id=self.episode_id, ok=False,
                script_rounds=cycle.round_count,
                reason=f"分镜阶段失败：{out.error}",
                wall_seconds=time.perf_counter() - t0)

        shot_list = self.orch.blackboard.value("shot.list")
        shots = tuple(shot_list.shots)
        planned = len(shots) if limit is None else min(len(shots), int(limit))
        picked = shots[:planned]

        # ---- 2.5) 角色定妆（文字半）----
        #
        # 位置在分镜之后、桥接之前：外观要写进**每镜的提示词**，
        # 所以必须在桥接前拿到。
        #
        # **这是"角色一致"的前置**：没有它，每镜提示词里只有"少女"三个字，
        # 图像模型只能即兴发挥 —— 于是同一角色长得不一样。
        # 有了它，至少**文字层面**是一致的（图像层面还需参考图，见 §5.8）。
        self.characters = self._design_characters(cycle.final)

        # ---- 3) 桥接 ----
        irs: list[dict[str, Any]] = [self._bridge(s) for s in picked]
        if limit is not None:
            irs = irs[:planned]

        # ---- 4) 渲染 ----
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
            gate=gate_dict)

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

        return EpisodeResult(
            episode_id=self.episode_id, ok=ok,
            script_rounds=cycle.round_count,
            script_verdict=(cycle.rounds[-1].verdict.value
                            if cycle.rounds else ""),
            shot_count=manifest.shot_count, planned_shots=planned,
            rendered=len(batch.rendered), cache_hits=len(batch.cache_hits),
            failed=len(failed), total_seconds=manifest.total_seconds,
            manifest=manifest, evidence=evidence, reason=reason,
            failures=failed, video=video, video_path=video_path,
            characters=self.characters,
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
    #: 为什么用映射而不是直接用动作名：白模的 8 个动作模板
    #: （`build_character.ACTION_TEMPLATES`）比镜头的 `action` 字段粗，
    #: 需要一个显式的对应关系，否则名字对不上就静默退回站姿。
    PREVIS_ACTIONS: Mapping[str, str] = {
        "walk": "act_walk",
        "run": "act_run",
        "sit_down": "act_sit_down",
        "sit": "act_sit_down",
        "stand": "act_stand",
        "wave": "act_wave",
        "recoil": "act_recoil",
        "look_back": "act_look_back",
        "point_forward": "act_point_forward",
        "point": "act_point_forward",
    }

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
        frame = self._previs_frame_for(shot) if want_ctrl else None
        if frame is None:
            want_ctrl = False
        if not want_src:
            return ir

        out = dict(ir)
        nodes = [dict(n) for n in ir.get("nodes", ())]

        # ---- ① 模型源 ----
        params: dict[str, Any] = {
            "unet_name": self.zimage_unet,
            "clip_name": self.zimage_clip,
            "vae_name": self.zimage_vae,
            # `weight_dtype` 是 UNETLoader 的**必需**参数；缺了会被
            # `required_input_missing` 拒绝。规则默认值只在自动注入路径生效，
            # 走显式声明时必须自己带上。
            "weight_dtype": "default",
        }
        old_src = next((n["id"] for n in nodes
                        if n.get("kind") == "model.checkpoint"), "n_ckpt")
        for n in nodes:
            if n.get("kind") == "model.checkpoint":
                n["kind"] = "model.source_zimage"
                n["params"] = params
                n["meta"] = {**(n.get("meta") or {}),
                             "title": "Z-Image 模型源（分离加载）"}
                break
        else:
            nodes.insert(0, {"id": old_src, "kind": "model.source_zimage",
                             "params": params})
        src_id = next((n["id"] for n in nodes
                       if n.get("kind") == "model.source_zimage"), old_src)

        # ---- ② 在 IR 里显式声明 CLIP / VAE 加载器，并把引用改指过去 ----
        #
        # ⚠️ **必须显式声明，不能只改引用。**
        # 只改引用的后果：IR 指向 `n_zimage_vae`，而该节点是编译器
        # **自动注入**的 —— IR 里不存在它，拓扑排序直接报
        # `DANGLING_REF: 引用了不存在的节点`。
        #
        # 显式声明还有个好处：编译器看到 IR 已声明同类加载器，
        # 就**不会重复注入**（`_inject_aux_loaders` 的幂等规则）。
        clip_kind = self._aux_loader_kind("CLIP")
        vae_kind = self._aux_loader_kind("VAE")
        node_ids = {n["id"] for n in nodes}

        for lkind, lname, lparams in (
            (clip_kind, "n_zimage_clip",
             {"clip_name": self.zimage_clip, "type": "qwen_image"}),
            (vae_kind, "n_zimage_vae", {"vae_name": self.zimage_vae}),
        ):
            if lkind and lname not in node_ids:
                nodes.append({"id": lname, "kind": lkind, "params": lparams,
                              "meta": {"title": f"{lkind}（模型源的一部分）"}})
                node_ids.add(lname)

        remap = {"CLIP": "n_zimage_clip" if clip_kind else "",
                 "VAE": "n_zimage_vae" if vae_kind else ""}
        for n in nodes:
            for port, ref in list((n.get("inputs") or {}).items()):
                if not isinstance(ref, dict):
                    continue
                p = str(ref.get("port", "")).upper()
                target = remap.get(p)
                if ref.get("ref") == src_id and target:
                    n["inputs"][port] = {"ref": target, "port": p}

        # ---- ③ 控制（只在需要时）----
        if not want_ctrl:
            out["nodes"] = nodes
            return out

        img_id = "n_ctrl_img"
        if img_id not in node_ids:
            # 投放进 ComfyUI 的 input/ 目录 —— LoadImage 不认绝对路径
            nodes.append({"id": img_id, "kind": "image.file",
                          "params": {"image": self._stage_control_image(frame)}})
            node_ids.add(img_id)

        # 控制信号预处理：模糊。
        #
        # ⚠️ **必须插在 LoadImage 与 ControlNet 之间，且必须做成图节点。**
        # 做成图节点的收益：`blur_radius` 进入 IR 哈希 ——
        # 改半径就是改 IR，缓存会正确失效。
        # 若在平台侧先模糊好再投放，改半径时**文件内容变了但 IR 没变**，
        # 命中旧缓存，表现为"调了参数没反应"。
        feed_id, feed_port = img_id, "IMAGE"
        blur_id = "n_ctrl_blur"
        blur = int(self.previs_blur_radius)
        if blur > 0 and not 1 <= blur <= 31:
            # ComfyUI 的 ImageBlur 约束是 1..31，超范围它会在**提交时**拒绝。
            # 在这里先拦下来，把"提交被拒"变成"本地就能看懂的错"。
            raise ValueError(
                f"previs_blur_radius={blur} 超出 ComfyUI ImageBlur 的允许范围 "
                f"1..31（0 表示不模糊）")
        if blur > 0 and blur_id not in node_ids:
            nodes.append({
                "id": blur_id, "kind": "composition.image_blur",
                "inputs": {"image": {"ref": img_id, "port": "IMAGE"}},
                "params": {"blur_radius": blur,
                           "sigma": float(self.previs_blur_sigma)},
                "meta": {"title": f"白模模糊（半径 {self.previs_blur_radius}）"},
            })
            node_ids.add(blur_id)
            feed_id, feed_port = blur_id, "IMAGE"

        ctrl_id = "n_ctrl"
        if ctrl_id not in node_ids:
            nodes.append({
                "id": ctrl_id, "kind": "composition.zimage_controlnet",
                "inputs": {"model": {"ref": src_id, "port": "MODEL"},
                           "vae": {"ref": remap["VAE"] or src_id,
                                   "port": "VAE"},
                           "image": {"ref": feed_id, "port": feed_port}},
                "params": {"strength": self.control_strength},
                "meta": {"title": f"白模控制（{self.previs_channel}）"},
            })
            for n in nodes:
                if n.get("kind") == "sample.ksampler":
                    n.setdefault("inputs", {})["model"] = {
                        "ref": ctrl_id, "port": "MODEL"}
                    break

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

            merged = f"{shot.description}（{'；'.join(frags)}）"
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
        if backend is not None:
            jobs = [(self._values(ir), ir["graphId"]) for ir in irs]
            return {"batch": self.renderer.render_http_batch(jobs, backend)}
        return {"batch": self.renderer.render_batch(irs)}

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
        for s, ir in zip(picked, irs):
            constraints[s.shot_id] = list(ir.get("constraints") or ())

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
                "plannedDurationSec": float(
                    getattr(s, "duration_sec", 0.0) or 0.0),
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
                                context_by_shot=context)

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
