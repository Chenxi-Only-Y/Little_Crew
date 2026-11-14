"""一键跑全部测试套件 + 架构守卫。

用法（从 platform 目录）：
    python tests/run_all.py

设计说明
--------
**为什么不用 pytest**：核心层刻意保持零第三方依赖（见 ARCHITECTURE.md §5.2），
测试也不例外。这样在任何只有 Python 3.12 的机器上都能验证正确性 ——
包括离线环境与答辩现场。

本脚本把每个套件作为**子进程**运行，原因有二：
  1. 各套件会调用 `sys.stdout.reconfigure(encoding="utf-8")`，同进程重复调用有副作用
  2. 子进程隔离能暴露 import 期错误（如循环依赖、路径依赖），同进程导入会掩盖问题

结果通过**退出码**判断，而非解析中文输出 —— 避免编码问题干扰判定。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
SRC = PLATFORM / "src"

#: (显示名, 运行方式, 期望项数或 None 表示不检查)
#:
#: 放在 ``src/little_crew/<层>/`` 下的是**单层单元测试**（只依赖更低的层）；
#: 放在 ``tests/`` 下的是**跨层集成测试**（架构守卫允许它那样做）。
SUITES: list[tuple[str, list[str], int | None]] = [
    ("IR 规范化与哈希", [sys.executable, "-m", "little_crew.ir.test_canonical"], 37),
    ("编译器与能力矩阵", [sys.executable, "-m", "little_crew.compiler.test_compiler"], 166),
    ("真实环境回归", [sys.executable, "-m", "little_crew.compiler.test_real_env"], 19),
    ("IR 出口（三模态×双通道）", [sys.executable, "-m", "little_crew.compiler.exits_check"], 19),
    ("模型授权门禁", [sys.executable, "-m", "little_crew.governance.test_license"], 33),
    ("三层调用协议", [sys.executable, "-m", "little_crew.agent.selfcheck"], 30),
    ("质检员", [sys.executable, "-m", "little_crew.review.qc_inspector_check"], 14),
    ("设定板质检", [sys.executable, "-m", "little_crew.review.sheet_qc_check"], 19),
    ("封面设计师", [sys.executable, "-m", "little_crew.review.cover_designer_check"], 24),
    ("后期统筹", [sys.executable, "-m", "little_crew.runtime.post_production_check"], 14),
    ("混合渲染", [sys.executable, "-m", "little_crew.runtime.cloud_shots_check"], 13),
    ("云端整集回归", [sys.executable, "-m", "little_crew.runtime.cloud_episode_check"], 8),
    ("自定义供应商", [sys.executable, "-m", "little_crew.runtime.custom_providers_check"], 30),
    ("角色设定图", [sys.executable, "-m", "little_crew.runtime.character_sheets_check"], 47),
    ("三级内容缓存", [sys.executable, "-m", "little_crew.runtime.selfcheck"], 42),
    ("模型路由与双通道", [sys.executable, str(PLATFORM / "tests" / "test_model_routing.py")], 60),
    # **同一模型的调用串行化**：GLM 端点并发受限，并发调用时第二个返回空内容，
    # 而上层只看到 [empty-output]（2026-10-19 实测，稳定复现）。
    ("LLM 调用串行化", [sys.executable, str(PLATFORM / "tests" / "test_llm_lock.py")], 7),
    # B 类最后两个职位：美术指导（art.bible）/ 制片主任（production.schedule）
    ("美术指导+制片主任", [sys.executable, "-m", "little_crew.agent.b_class_llm_check"], 33),
    # 白模布局校验器：PrevisAgent 的验收标准。纯几何，与 Blender 同一套数学。
    ("白模布局校验", [sys.executable, "-m", "little_crew.agent.previs_layout_check"], 24),
    # 白模生产模块：布局→spec→Blender→帧→mp4+首帧。**可选增强，但失败必须可见。**
    ("白模渲染", [sys.executable, "-m", "little_crew.runtime.previs_check"], 16),
    # 参考视频规格：按目标视频模型契约算"刚好过线"，并校核已渲的视频。
    ("参考视频规格", [sys.executable, "-m", "little_crew.runtime.video_ref_check"], 28),
    # 发布器：把产物变成 URL（视频模型只收 URL）。接口可替换，实现按需加。
    ("产物发布器", [sys.executable, "-m", "little_crew.runtime.publish_check"], 14),
    # 工作室：图上改一镜的布局并只重渲那一镜。真起服务、不调 Blender。
    ("工作室编辑", [sys.executable, "-m", "little_crew.runtime.studio_check"], 31),
    # 半成品报告：中途停下时把「到哪了」落成文件。纯文件读写。
    ("半成品报告", [sys.executable, "-m", "little_crew.runtime.partial_report_check"], 29),
    # 资产库：角色定妆表落盘 + 读回复用。纯文件。
    ("资产库", [sys.executable, "-m", "little_crew.runtime.assets_check"], 26),
    # 后端选择：把"用哪个后端"从入口脚本搬出来。含源码守卫（不再写死 HTTP）。
    ("后端选择", [sys.executable, "-m", "little_crew.runtime.backend_select_check"], 22),
    # 场景主体冲突：下游越权新增场景主体（2026-10-29 实测撞出来的真事）。
    ("场景主体冲突", [sys.executable, "-m", "little_crew.review.scene_subject_check"], 19),
    ("暂存发布", [sys.executable, "-m", "little_crew.runtime.staging_publish_check"], 13),
    ("接口缺陷清单", [sys.executable, "-m", "little_crew.review.interface_defects_check"], 20),
    ("渲染审片", [sys.executable, "-m", "little_crew.review.image_review_check"], 11),
    ("视觉闸", [sys.executable, "-m", "little_crew.review.vision_review_check"], 15),
    ("对白编辑职位", [sys.executable, "-m", "little_crew.runtime.dialogue_editor_agent_check"], 12),
    ("配乐摄影职位", [sys.executable, "-m", "little_crew.runtime.music_camera_agents_check"], 12),
    ("音乐床决策", [sys.executable, "-m", "little_crew.runtime.music_stage_check"], 14),
    ("逐场分镜", [sys.executable, "-m", "little_crew.runtime.storyboard_stage_check"], 13),
    ("场景主体修复", [sys.executable, "-m", "little_crew.runtime.subject_repair_check"], 16),
    ("视觉重渲", [sys.executable, "-m", "little_crew.runtime.vision_rework_check"], 18),
    ("断点续跑", [sys.executable, "-m", "little_crew.runtime.stage_checkpoint_check"], 18),
    ("人工 Gate", [sys.executable, "-m", "little_crew.runtime.human_gate_check"], 17),
    ("产物清理", [sys.executable, "-m", "little_crew.runtime.prune_artifacts_check"], 21),
    ("进度落盘", [sys.executable, "-m", "little_crew.runtime.progress_check"], 20),
    ("结构化日志", [sys.executable, "-m", "little_crew.runtime.runlog_check"], 12),
    ("调用树落盘", [sys.executable, "-m", "little_crew.runtime.trace_dump_check"], 12),
    ("场景预算协调器", [sys.executable, "-m", "little_crew.governance.plan_alloc_check"], 38),
    ("处置闭环", [sys.executable, "-m", "little_crew.governance.disposition_check"], 20),
    ("provider 指纹", [sys.executable, "-m", "little_crew.governance.provider_fingerprint_check"], 14),
    ("配音规则", [sys.executable, "-m", "little_crew.review.voice_cast_check"], 19),
    ("配音轨", [sys.executable, "-m", "little_crew.runtime.voice_track_check"], 19),
    ("对白编辑", [sys.executable, "-m", "little_crew.governance.dialogue_editor_check"], 19),
    ("混流", [sys.executable, "-m", "little_crew.runtime.mixdown_check"], 12),
    ("音乐床", [sys.executable, "-m", "little_crew.runtime.music_bed_check"], 14),
    ("存音色工具", [sys.executable, "-m", "little_crew.runtime.voice_save_check"], 4),
    ("音色设计", [sys.executable, "-m", "little_crew.runtime.voice_design_check"], 15),
    ("能力通道层", [sys.executable, "-m", "little_crew.runtime.capability_channels_check"], 14),
    ("模型路由", [sys.executable, "-m", "little_crew.runtime.model_routing_check"], 16),
    ("成片合成", [sys.executable, "-m", "little_crew.runtime.finalize_check"], 9),
    ("摄像接口", [sys.executable, "-m", "little_crew.runtime.video_operator_check"], 12),
    ("证据链可校验", [sys.executable, "-m", "little_crew.runtime.evidence_verify_check"], 9),
    ("证据包导出", [sys.executable, "-m", "little_crew.runtime.evidence_bundle_check"], 10),
    ("定向返工", [sys.executable, "-m", "little_crew.runtime.rework_check"], 13),
    ("定向返工执行", [sys.executable, "-m", "little_crew.runtime.rework_pipeline_check"], 13),
    ("角色锚点发现", [sys.executable, "-m", "little_crew.runtime.anchors_check"], 9),
    ("角色定妆表", [sys.executable, "-m", "little_crew.agent.character_sheet_check"], 77),
    ("多智能体协作", [sys.executable, str(PLATFORM / "tests" / "test_agent_collab.py")], 49),
    ("剧组编排闭环", [sys.executable, "-m", "little_crew.agent.orchestrator_check"], 60),
    ("预算账本", [sys.executable, "-m", "little_crew.agent.budget_check"], 21),
    ("并发调度", [sys.executable, "-m", "little_crew.agent.scheduler_check"], 14),
    ("核心职位规则桩", [sys.executable, "-m", "little_crew.agent.crew_stubs_check"], 82),
    # 真实 LLM 职位的自检。**用假客户端，不消耗 API** ——
    # 它验的是机制（返工意见真的传到模型了吗、空转会不会被当成成功），
    # 那些必须能被**确定性地**断言，不能靠"我看模型像是改了"。
    ("LLM 职位机制", [sys.executable, "-m", "little_crew.agent.crew_llm_check"], 38),
    # ⚠️ 这一套的存在理由（2026-10-17 的教训）：`crew_llm_check` 只碰
    # `DramaturgeLlm().client`，**从不调用它的 `__call__`** ——
    # 于是剧评人换完整性公式时漏了 import，1205 项测试全过、
    # 直到真实一集连崩 3 次才暴露。**"能构造"不等于"能工作"。**
    ("LLM 剧评人", [sys.executable, "-m", "little_crew.agent.dramaturge_llm_check"], 17),
    # A 类审查职位的**通用批评家**：一次覆盖 5 个（story_editor /
    # continuity_supervisor / compliance_officer / pacing_editor /
    # review_board）。假客户端，不吃 API。
    ("通用批评家", [sys.executable, "-m", "little_crew.agent.critic_llm_check"], 37),
    # B 类第一个：导演产出 episode.plan（每场镜头预算 + 基调 + 动机）
    ("导演职位", [sys.executable, "-m", "little_crew.agent.director_llm_check"], 41),
    # 导演**接线**（不是职位本身）：plan 有没有真的注入到分镜师，
    # 以及**调用顺序**对不对（晚于分镜则预算毫无意义）。
    # 单开文件是为了避开 episode_check 里既有的 G/H 段标签。
    ("导演接线", [sys.executable, "-m", "little_crew.runtime.director_wiring_check"], 15),
    ("灯光师", [sys.executable, "-m", "little_crew.agent.gaffer_llm_check"], 30),
    ("镜头→IR 桥接", [sys.executable, "-m", "little_crew.agent.ir_bridge_check"], 65),
    ("渲染编排", [sys.executable, "-m", "little_crew.runtime.render_check"], 60),
    ("HTTP 通道端到端", [sys.executable, "-m", "little_crew.runtime.channels.http_e2e_check"], 11),
    ("渲染并发策略", [sys.executable, "-m", "little_crew.runtime.render_policy_check"], 16),
    ("缓存感知渲染", [sys.executable, "-m", "little_crew.runtime.cached_render_check"], 66),
    ("渲染检查点与续跑", [sys.executable, "-m", "little_crew.runtime.checkpoint_check"], 54),
    # ⚠️ 这一套用**假通道**：H3 单镜约 340 秒，真打服务的测试没人会跑，
    # 而没人跑的测试等于没有。
    ("H3 音轨渲染", [sys.executable, "-m", "little_crew.runtime.h3_video_check"], 31),
    # 顾问批评家的守卫：**取不到可审正文 -> error，不是 PASS**。
    # 单开文件是为了避开 episode_check 里既有的 G/H 段标签（之前撞过两次）。
    ("顾问批评家守卫", [sys.executable, "-m", "little_crew.runtime.advisory_check"], 10),
    # **轮询必须节流**：紧循环曾把 Windows 临时端口打光（TIME_WAIT 21427 > 16384），
    # 表现为"异步渲染随机失败"。这条守住 sleep，防止后人当冗余删掉。
    ("轮询节流", [sys.executable, "-m", "little_crew.runtime.polling_check"], 6),
    # video_mode **第二段**：把已有静帧动画化。
    # 反馈循环很贵（2 镜 ≈12 分钟 / 整集 ≈2.4 小时），所以先用假通道验接线。
    # 重点是失败语义：**拿不到本地后端绝不退回付费 API**。
    ("video_mode 第二段", [sys.executable, "-m", "little_crew.runtime.video_mode_check"], 36),
    ("一集流水线", [sys.executable, "-m", "little_crew.runtime.episode_check"], 89),
    ("视频合成", [sys.executable, "-m", "little_crew.runtime.video_check"], 64),
    ("端到端真机", [sys.executable, str(PLATFORM / "tests" / "test_e2e_comfy.py")], 19),
    ("确定性验收门禁", [sys.executable, "-m", "little_crew.review.check"], 116),
    ("L3 跨模型族盲读", [sys.executable, "-m", "little_crew.agent.blind_check"], 19),
    ("L2 一致性描述子", [sys.executable, "-m", "little_crew.review.embed_check"], 51),
    ("命令行与 HTTP 接口", [sys.executable, "-m", "little_crew.cli.check"], 71),
    ("运维 CLI（doctor/library/report）", [sys.executable, "-m", "little_crew.cli.ops_check"], 16),
    ("对外接口面", [sys.executable, "-m", "little_crew.api.surface_check"], 19),
    ("架构依赖守卫", [sys.executable, str(PLATFORM / "tests" / "test_architecture.py")], None),
]


def _extract_count(text: str) -> int | None:
    """从输出里提取「结果：N/M 通过」的 M。解析失败返回 None。"""
    for line in text.splitlines():
        if "结果：" in line and "通过" in line:
            try:
                frac = line.split("结果：")[1].split()[0]      # 形如 36/36
                return int(frac.split("/")[1])
            except (IndexError, ValueError):
                return None
    return None


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    import os

    # 测试不写仓库（2026-11-12）：否则每次跑都会改 research/script-rounds/E100.json
    os.environ.setdefault("LITTLE_CREW_RESEARCH_DUMP", "0")
    # 任务表现在会落盘（api.runs）：测试必须用**独立临时目录**，
    # 否则上一次跑留下的 runId 会让 `/v1/runs` 初始列表不为空（D10 直接挂）。
    if not os.environ.get("LITTLE_CREW_RUNS_DIR"):
        import tempfile

        os.environ["LITTLE_CREW_RUNS_DIR"] = tempfile.mkdtemp(prefix="lc_runs_")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"

    print("=" * 78)
    print("小剧组 · Little Crew —— 全量测试")
    print("=" * 78)
    print(f"源码根: {SRC}")
    print()

    total_passed = 0
    total_expected = 0
    failures: list[tuple[str, str]] = []
    t0 = time.perf_counter()

    for name, cmd, expected in SUITES:
        t = time.perf_counter()
        proc = subprocess.run(
            cmd, cwd=str(PLATFORM), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False,
        )
        elapsed = time.perf_counter() - t
        out = (proc.stdout or "") + (proc.stderr or "")
        count = _extract_count(out)
        ok = proc.returncode == 0

        if expected is not None:
            total_expected += expected
            if count is not None:
                total_passed += count
            elif ok:
                # 退出码为 0 但没解析到数字：按期望值计入，避免统计偏低
                total_passed += expected

        mark = "v" if ok else "X"
        count_txt = f"{count} 项" if count is not None else "—"
        print(f" [{mark}] {name:20} {count_txt:>8}   {elapsed:5.2f}s")

        if not ok:
            failures.append((name, out))

        if expected is not None and count is not None and count != expected:
            print(f"       ⚠ 用例数由 {expected} 变为 {count} —— "
                  f"请确认是有意增删，还是测试被误改")

    total_elapsed = time.perf_counter() - t0

    print()
    print("=" * 78)
    if failures:
        for name, out in failures:
            print(f"\n----- {name} 失败输出（尾部 40 行）-----")
            for line in out.splitlines()[-40:]:
                print("  " + line)
        print("=" * 78)
        print(f"结果：FAIL（{len(failures)} 个套件失败，耗时 {total_elapsed:.2f}s）")
        return 1

    print(f"结果：全部通过 —— 功能用例 {total_passed}/{total_expected}，"
          f"外加架构守卫 PASS，耗时 {total_elapsed:.2f}s")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
