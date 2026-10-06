"""pytest 全局夹具：把**运行时数据**重定向到临时目录，避免测试污染真实数据。

## 为什么需要

实测踩到：跑一次全套测试，往 `data/proposals/` 留下 **100 份** `author='test'`
的垃圾提案（真实目录里 105 份有 100 份是测试的）。

这类问题**不会让测试变红**，所以特别隐蔽 —— 靠人工核查才发现。
根因是 `proposal_store.proposals_dir()` 原先硬编码到 `data/proposals/`。

现在它支持 `LITTLE_CREW_PROPOSALS_DIR` 环境变量，这里在**每个测试**开始前
指向一个临时目录，结束后还原。
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_proposals_dir(tmp_path, monkeypatch):
    """让提案落盘写到临时目录，不碰 `data/proposals/`。"""
    monkeypatch.setenv("LITTLE_CREW_PROPOSALS_DIR", str(tmp_path / "proposals"))
    yield


@pytest.fixture(scope="session", autouse=True)
def _warn_if_real_data_touched():
    """兜底断言：测试跑完后，真实 `data/proposals/` 不该多出 `author='test'` 的文件。

    只在**真实目录本来不存在或为空**时才检查（避免误报历史遗留）。
    这是"防回归"的护栏：万一以后有人绕过 `proposals_dir()` 直接写文件，
    这条会在最后报出来。
    """
    import json
    from pathlib import Path

    real = Path(__file__).resolve().parent.parent / "data" / "proposals"
    before = set()
    if real.exists():
        before = {f.name for f in real.glob("*.json")}

    yield

    if not real.exists():
        return
    new = {f.name for f in real.glob("*.json")} - before
    offenders = []
    for name in new:
        try:
            rec = json.loads((real / name).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if (rec.get("author") or "") in ("test", "e2e"):
            offenders.append(name)
    if offenders:
        import warnings

        warnings.warn(
            f"测试污染了真实数据目录 data/proposals/：新增 {len(offenders)} 个"
            f"测试提案（{offenders[:3]}…）。请检查是不是绕过了 "
            f"proposal_store.proposals_dir() 的环境变量开关。",
            stacklevel=2,
        )
