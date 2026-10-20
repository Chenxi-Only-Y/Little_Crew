"""**半成品报告**：跑到一半停下（Ctrl+C、异常、或主动停）时，把"到哪了"落成文件。

## 为什么非有它不可

正常结束时 `run.report.json` 会把一切都写上。但**中途停下的时候什么都没有** ——
跑了 26 分钟、第 30 镜发现不对、停下，然后：

> 没有任何东西说明"前 29 镜是怎么判的、花了多久、哪一步是卡住的"。

**"能停"和"停得有记录"是两件事。** 这个模块补后面那件。

## 两条设计原则

**① 只写"确定的"**

能从磁盘上数出来的（白模几个镜头、缓存里几张图、交付有没有母片）就数；
数不出来的（"剧本为什么没过"）**宁可不写，也不猜**。

**② 明确写出"还差什么"**

`pending` 字段列出没完成的阶段。
**一份报告说清"缺哪几块"比假装完整有用得多** —— 后者会让人以为跑完了。

## 与正常报告的关系

文件名不同（`run.report.partial.json`），**不覆盖** `run.report.json`。
这样：同一次跑既可能有一次中断记录，也可能后来又跑出了完整报告，两份都在。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

__all__ = ["PARTIAL_NAME", "STAGES", "write_partial"]

#: 半成品报告文件名（**不覆盖** `run.report.json`）
PARTIAL_NAME = "run.report.partial.json"

#: 阶段顺序 —— 用于算"还差什么"。键要短，值是人话。
#: ⚠️ **顺序必须与 `EpisodeRunner.run()` 的真实执行顺序一致**。
#: 原来把 `storyboard` 写在 `director` 前面 —— 于是"停在分镜"时，
#: 已经跑过的导演被错误列进"还差 N 个阶段"。
#: 更要紧的是：**"已完成"要按实际 trace 算，不能按这张表的顺序算**。
STAGES: tuple[tuple[str, str], ...] = (
    ("script", "剧本闭环"),
    ("director", "导演定调"),
    ("art", "美术设定"),
    ("lighting", "灯光方案"),
    ("storyboard", "分镜"),
    ("previs", "白模预演"),
    ("render", "出图"),
    ("assemble", "成片合成"),
)


def _scan(workdir: Path) -> dict[str, Any]:
    """从磁盘数出**确定的**事实。数不出来的一律不写。"""
    out: dict[str, Any] = {}
    previs = workdir / "previs"
    if previs.is_dir():
        dirs = [d for d in previs.iterdir() if d.is_dir()]
        out["previsShots"] = len(dirs)
        out["previsControl"] = sum(
            1 for d in dirs if (d / "previs_control.png").is_file())
    cache = workdir / "cache"
    if cache.is_dir():
        n = 0
        for tier in ("t1", "t2", "t3"):
            base = cache / tier
            if base.is_dir():
                n += sum(1 for p in base.rglob("*") if p.is_file())
        out["cacheFiles"] = n
    delivery = workdir / "delivery"
    if delivery.is_dir():
        out["files"] = sorted(p.name for p in delivery.iterdir() if p.is_file())
        out["hasMaster"] = (delivery / "episode.master.mp4").is_file()
    return out


def write_partial(
    workdir: Path,
    *,
    episode_id: str = "",
    title: str = "",
    stage: str = "",
    reason: str = "",
    trace: Mapping[str, Any] | None = None,
    pending: Iterable[str] | None = None,
    started_at: float | None = None,
) -> Path:
    """写 `<workdir>/delivery/run.report.partial.json`，返回其路径。

    `pending` 不给时，按 `STAGES` 里"比 `stage` 靠后"的算 —— 这是最常见的用法。
    """
    workdir = Path(workdir)
    delivery = workdir / "delivery"
    delivery.mkdir(parents=True, exist_ok=True)

    trace_keys = set((trace or {}).keys())
    names = dict(STAGES)
    # **"已完成"按实际 trace 算** —— 静态顺序算不准（`director` 就跑在
    # `storyboard` 之前，按顺序取下标会把已完成的算成未完成）。
    done = [f"{k}（{names[k]}）" for k, _ in STAGES if k in trace_keys]
    if pending is None:
        pending = [f"{k}（{names[k]}）" for k, _ in STAGES
                   if k not in trace_keys]

    doc: dict[str, Any] = {
        "kind": "run.report.partial",
        "interrupted": True,
        "episodeId": episode_id,
        "title": title,
        "stoppedAt": stage,
        "stoppedAtName": dict(STAGES).get(stage, stage),
        "reason": reason,
        "finishedStages": done,
        "pendingStages": list(pending),
        "trace": dict(trace or {}),
        "disk": _scan(workdir),
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if started_at:
        doc["elapsedSeconds"] = round(time.time() - started_at, 1)

    path = delivery / PARTIAL_NAME
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def summary_line(path: Path) -> str:
    """一行摘要，给命令行打印用。"""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:                                        # noqa: BLE001
        return str(path)
    disk = d.get("disk") or {}
    bits = []
    if d.get("stoppedAtName"):
        bits.append(f"停在「{d['stoppedAtName']}」")
    if disk.get("previsShots"):
        bits.append(f"白模 {disk['previsShots']} 镜")
    if disk.get("cacheFiles"):
        bits.append(f"缓存文件 {disk['cacheFiles']} 个")
    if d.get("pendingStages"):
        bits.append(f"还差 {len(d['pendingStages'])} 个阶段")
    return " · ".join(bits)
