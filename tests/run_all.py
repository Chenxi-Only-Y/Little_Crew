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
    ("IR 规范化与哈希", [sys.executable, "-m", "little_crew.ir.test_canonical"], 36),
    ("编译器与能力矩阵", [sys.executable, "-m", "little_crew.compiler.test_compiler"], 145),
    ("真实环境回归", [sys.executable, "-m", "little_crew.compiler.test_real_env"], 19),
    ("模型授权门禁", [sys.executable, "-m", "little_crew.governance.test_license"], 33),
    ("三层调用协议", [sys.executable, "-m", "little_crew.agent.selfcheck"], 27),
    ("三级内容缓存", [sys.executable, "-m", "little_crew.runtime.selfcheck"], 42),
    ("模型路由与双通道", [sys.executable, str(PLATFORM / "tests" / "test_model_routing.py")], 43),
    ("角色定妆表", [sys.executable, "-m", "little_crew.agent.character_sheet_check"], 77),
    ("多智能体协作", [sys.executable, str(PLATFORM / "tests" / "test_agent_collab.py")], 49),
    ("剧组编排闭环", [sys.executable, "-m", "little_crew.agent.orchestrator_check"], 43),
    ("核心职位规则桩", [sys.executable, "-m", "little_crew.agent.crew_stubs_check"], 79),
    # 真实 LLM 职位的自检。**用假客户端，不消耗 API** ——
    # 它验的是机制（返工意见真的传到模型了吗、空转会不会被当成成功），
    # 那些必须能被**确定性地**断言，不能靠"我看模型像是改了"。
    ("LLM 职位机制", [sys.executable, "-m", "little_crew.agent.crew_llm_check"], 38),
    # ⚠️ 这一套的存在理由（2026-10-17 的教训）：`crew_llm_check` 只碰
    # `DramaturgeLlm().client`，**从不调用它的 `__call__`** ——
    # 于是剧评人换完整性公式时漏了 import，1205 项测试全过、
    # 直到真实一集连崩 3 次才暴露。**"能构造"不等于"能工作"。**
    ("LLM 剧评人", [sys.executable, "-m", "little_crew.agent.dramaturge_llm_check"], 17),
    ("镜头→IR 桥接", [sys.executable, "-m", "little_crew.agent.ir_bridge_check"], 60),
    ("渲染编排", [sys.executable, "-m", "little_crew.runtime.render_check"], 57),
    ("缓存感知渲染", [sys.executable, "-m", "little_crew.runtime.cached_render_check"], 53),
    ("渲染检查点与续跑", [sys.executable, "-m", "little_crew.runtime.checkpoint_check"], 54),
    ("一集流水线", [sys.executable, "-m", "little_crew.runtime.episode_check"], 82),
    ("视频合成", [sys.executable, "-m", "little_crew.runtime.video_check"], 64),
    ("端到端真机", [sys.executable, str(PLATFORM / "tests" / "test_e2e_comfy.py")], 19),
    ("确定性验收门禁", [sys.executable, "-m", "little_crew.review.check"], 80),
    ("L3 跨模型族盲读", [sys.executable, "-m", "little_crew.agent.blind_check"], 19),
    ("L2 一致性描述子", [sys.executable, "-m", "little_crew.review.embed_check"], 51),
    ("命令行与 HTTP 接口", [sys.executable, "-m", "little_crew.cli.check"], 38),
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
