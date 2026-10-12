"""pytest 全局夹具：把**运行时数据**重定向到临时目录，避免测试污染真实数据。

## 为什么需要（踩过两次同一类坑）

**第一次**：跑一次全套测试往 `data/proposals/` 留下 **100 份** `author='test'`
的垃圾提案（真实目录 105 份里 100 份是测试的）。

**第二次**（2026-10-07 清 outputs 时发现）：测试往真实 `data/outputs/` 写产物 ——
清查时那里堆了 **95 个测试目录 / 966 个文件**：

    · 93 个哈希目录，各 3 张 4.5KB 的 mock 占位图（会话名 `test_claim_http_*`）；
    · `_global/` 与 `tester/` 里 649 + 36 个文件，包含**真跑的 Edge TTS mp3**
      （14KB/个，说明 TTS 真的被调用了），会话名如 `adv_verify-1`、`e2e_full`。

这类问题的共同点：**不会让测试变红**，所以只有人工去看目录才会发现。

## 做法

  1. `LITTLE_CREW_PROPOSALS_DIR` → 临时目录（`proposal_store` 支持该环境变量）；
  2. `settings.output_dir` → 临时目录（产出归档的根）。
     必须在**任何 app 模块被导入之前**设置，否则 `settings` 已经用旧值初始化 ——
     所以这段代码放在 conftest 的**模块顶层**（pytest 保证先加载 conftest）。
  3. 兜底护栏：跑完后真实目录若多出东西，发 warning。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# ============================================================================
# ⚠️ 必须在导入任何 app 模块**之前**执行
#
# `app.core.config.settings` 在导入时就会读环境变量并固化 `output_dir`。
# 若放到 fixture 里再改，依赖它的模块可能已经拿到旧值。
#
# 用 `tempfile.gettempdir()` 拼路径而**不** `mkdtemp()` —— 后者会立刻建目录，
# 而多数测试不写产出，跑完就留一堆空目录。这里让**产出目录**按需创建
# （`asset_paths` / 输出代码都会 `mkdir(parents=True, exist_ok=True)`）。
#
# ⚠️ 但**根目录本身必须存在** —— sqlite 打开 `{root}/test.db` 时不会帮你建父目录，
# 否则报 `sqlite3.OperationalError: unable to open database file`
# （我第一版就是栽在这：10 个测试文件在 collection 阶段就 ERROR）。
_TMP_ROOT = Path(tempfile.gettempdir()) / "littlecrew-tests"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["LITTLE_CREW_PROPOSALS_DIR"] = str(_TMP_ROOT / "proposals")
#: 产出归档根：`data/outputs/{账号}/{工程名}/{部门}/{职业}/{会话}/…`
os.environ["OUTPUT_DIR"] = str(_TMP_ROOT / "outputs")
# 顺带把库也指到临时目录（测试不该写用户的会话/设置库）
os.environ.setdefault("DB_PATH", str(_TMP_ROOT / "test.db"))


@pytest.fixture(autouse=True)
def _isolate_proposals_dir(tmp_path, monkeypatch):
    """让提案落盘写到**每个测试自己的**临时目录，不碰 `data/proposals/`。"""
    monkeypatch.setenv("LITTLE_CREW_PROPOSALS_DIR", str(tmp_path / "proposals"))
    yield


@pytest.fixture(autouse=True)
def _isolate_output_dir(tmp_path, monkeypatch):
    """让产出归档写到**每个测试自己的**临时目录，不碰 `data/outputs/`。

    双重保险：即使某个模块缓存了 `settings`，这里也把**属性本身**改掉
    （`asset_paths` 每次调用都读 `settings.output_dir`，所以改属性是生效的）。
    """
    try:
        from app.core.config import settings

        monkeypatch.setattr(settings, "output_dir", str(tmp_path / "outputs"), raising=False)
    except Exception:  # noqa: BLE001  app 还没导入（极少见）就只靠环境变量
        pass
    yield


@pytest.fixture(scope="session", autouse=True)
def _warn_if_real_data_touched():
    """兜底护栏：跑完后真实 `data/proposals/` 与 `data/outputs/` 不该多出东西。

    只在**真实目录本来为空**时才判定（避免把历史遗留算到这次头上）。
    """
    import json
    import warnings

    root = Path(__file__).resolve().parent.parent / "data"
    props = root / "proposals"
    outs = root / "outputs"

    before_props = {f.name for f in props.glob("*.json")} if props.exists() else set()
    before_out = {p.name for p in outs.iterdir()} if outs.exists() else set()

    yield

    # proposals：新增的测试提案
    if props.exists():
        new = {f.name for f in props.glob("*.json")} - before_props
        offenders = []
        for name in new:
            try:
                rec = json.loads((props / name).read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if (rec.get("author") or "") in ("test", "e2e"):
                offenders.append(name)
        if offenders:
            warnings.warn(
                f"测试污染了真实目录 data/proposals/：新增 {len(offenders)} 个"
                f"测试提案（{offenders[:3]}…）。检查是不是绕过了 "
                f"`proposals_dir()` 的环境变量开关。",
                stacklevel=2,
            )

    # outputs：新增的账号目录
    if outs.exists():
        new_out = {p.name for p in outs.iterdir()} - before_out
        if new_out:
            warnings.warn(
                f"测试污染了真实目录 data/outputs/：新增 {len(new_out)} 个条目"
                f"（{sorted(new_out)[:3]}…）。检查是不是绕过了 "
                f"`settings.output_dir` / `OUTPUT_DIR` 的隔离。",
                stacklevel=2,
            )
