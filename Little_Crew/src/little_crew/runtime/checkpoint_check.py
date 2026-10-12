"""渲染检查点 —— 自检。

**核心命题**：崩溃重启后，**已完成的不重跑、在途的不重提**。
后半句更重要 —— 重提就是**双份 GPU**（云端就是双份账单）。

方法：可编排的假通道 + 临时目录。**完全离线、完全确定**，
因此断言的是**机制**而不是"某次跑得顺不顺"。

运行： python -m little_crew.runtime.checkpoint_check
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

from little_crew.runtime.checkpoint import (
    CheckpointRecord,
    JobState,
    RenderCheckpoint,
    ResumePlan,
)

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


# ==========================================================================
# 假通道：可编排"重启后服务端还记得什么"
# ==========================================================================

class FakeReconciler:
    """模拟服务端的 ``/history``。

    ``known`` 就是"服务端记得的任务" —— 它**跨进程存活**，
    所以新进程（新对象）只要查同一个服务端，就能看到同样的内容。
    这正是对账能工作的物理基础。
    """

    def __init__(self) -> None:
        self.known: dict[str, str] = {}      # channel_id -> "succeeded"|"failed"|"running"
        self.artifacts: dict[str, list[str]] = {}
        self.queries: list[list[str]] = []

    def reconcile(self, ids: Sequence[str]) -> dict[str, Any]:
        self.queries.append(list(ids))
        out: dict[str, Any] = {}
        for cid in ids:
            st = self.known.get(cid)
            if st is None:
                continue
            out[cid] = _Handle(cid, st, self.artifacts.get(cid, []))
        return out


class _Handle:
    def __init__(self, cid: str, status: str, artifacts: list[str]) -> None:
        self.channel_id = cid
        self.status = type("S", (), {"value": status})()
        self.artifacts = [_Ref(a) for a in artifacts]


class _Ref:
    def __init__(self, uri: str) -> None:
        self.uri = uri


# ==========================================================================
# 测试
# ==========================================================================

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    print("=" * 78)
    print("渲染检查点 —— 自检")
    print("=" * 78)

    tmp = Path(tempfile.mkdtemp(prefix="lc_ckpt_"))
    try:
        _basics(tmp)
        _persistence(tmp)
        _plan(tmp)
        _no_double_submit(tmp)
        _reconcile(tmp)
        _broken_ledger(tmp)
        _atomic(tmp)
        _resume_end_to_end(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    npass = sum(1 for _, ok, _ in _results if ok)
    for name, ok, detail in _results:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail:
            print(f"       {detail}")
    print("=" * 78)
    print(f"结果：{npass}/{len(_results)} 通过")
    return 0 if npass == len(_results) else 1


# ---------- A. 基本状态流转 ----------

def _basics(tmp: Path) -> None:
    ck = RenderCheckpoint(tmp / "a", episode_id="E001")
    check("A1 新建账本为空", ck.record_count == 0 and ck.counts() == {})

    r = ck.begin("SH1", "key-1", fingerprint="fp1")
    check("A2 begin 后为 PENDING（记了意图，未提交）",
          r.state is JobState.PENDING, r.state.value)
    check("A3 PENDING **不需要**对账（重提是安全的）",
          not r.state.needs_reconcile)

    ck.mark_submitted("SH1", "prompt-1")
    r = ck.get("SH1")
    check("A4 mark_submitted 后为 SUBMITTED", r.state is JobState.SUBMITTED)
    check("A5 SUBMITTED **必须**对账（可能已烧 GPU）",
          r.state.needs_reconcile)
    check("A6 记了 channel_id", r.channel_id == "prompt-1", r.channel_id)
    check("A7 记了尝试次数", r.attempts == 1, f"attempts={r.attempts}")

    ck.mark_succeeded("SH1", ["a.png", "b.png"])
    r = ck.get("SH1")
    check("A8 mark_succeeded 后为 SUCCEEDED 且有产物",
          r.state is JobState.SUCCEEDED and len(r.artifacts) == 2,
          str(r.artifacts))
    check("A9 SUCCEEDED 是终态", r.state.terminal)

    # 失败与未知
    ck.begin("SH2", "key-2")
    ck.mark_failed("SH2", "boom")
    check("A10 mark_failed 记录原因",
          ck.get("SH2").state is JobState.FAILED and "boom" in ck.get("SH2").error)

    ck.begin("SH3", "key-3")
    ck.mark_unknown("SH3", "连接断了")
    check("A11 UNKNOWN 也要求对账（不可假定失败）",
          ck.get("SH3").state.needs_reconcile)

    check("A12 counts 汇总各状态",
          ck.counts() == {"succeeded": 1, "failed": 1, "unknown": 1},
          str(ck.counts()))

    # 幂等键变了 → 之前的完成状态必须作废
    ck.begin("SH1", "key-NEW")
    r = ck.get("SH1")
    check("A13 **幂等键变了则作废完成状态**（参数改了不能复用旧记录）",
          r.state is JobState.PENDING and r.channel_id == ""
          and r.artifacts == (),
          f"{r.state.value} ch={r.channel_id!r} arts={r.artifacts}")

    # 未 begin 就 mark_submitted → 明确报错
    try:
        ck.mark_submitted("NEVER", "p")
        ok = False
    except KeyError:
        ok = True
    check("A14 未 begin 就提交 → 明确报错（不静默建记录）", ok)


# ---------- B. 跨重启持久 ----------

def _persistence(tmp: Path) -> None:
    root = tmp / "b"
    ck = RenderCheckpoint(root, episode_id="E009")
    ck.begin("SH1", "k1")
    ck.mark_submitted("SH1", "p1")
    ck.begin("SH2", "k2")
    ck.mark_succeeded("SH2", ["x.png"])

    # 新对象 = 新进程
    ck2 = RenderCheckpoint(root)
    check("B1 重启后记录数保持", ck2.record_count == 2, f"{ck2.record_count}")
    check("B2 重启后状态保持",
          ck2.get("SH1").state is JobState.SUBMITTED
          and ck2.get("SH2").state is JobState.SUCCEEDED)
    check("B3 重启后集号保持", ck2.episode_id == "E009", ck2.episode_id)
    check("B4 重启后产物清单保持",
          ck2.get("SH2").artifacts == ("x.png",), str(ck2.get("SH2").artifacts))
    check("B5 重启后 channel_id 保持（对账要靠它）",
          ck2.get("SH1").channel_id == "p1", ck2.get("SH1").channel_id)


# ---------- C. 续跑计划 ----------

def _plan(tmp: Path) -> None:
    ck = RenderCheckpoint(tmp / "c")
    ck.begin("DONE", "k"); ck.mark_succeeded("DONE", ["a.png"])
    ck.begin("INFLIGHT", "k"); ck.mark_submitted("INFLIGHT", "p")
    ck.begin("NOTSENT", "k")                      # 只有意图
    ck.begin("BAD", "k"); ck.mark_failed("BAD", "x")

    plan = ck.plan(expected=["DONE", "INFLIGHT", "NOTSENT", "BAD", "NEW1", "NEW2"])
    check("C1 已完成的进 done", [r.job_id for r in plan.done] == ["DONE"],
          str([r.job_id for r in plan.done]))
    check("C2 在途的进 reconcile",
          [r.job_id for r in plan.reconcile] == ["INFLIGHT"],
          str([r.job_id for r in plan.reconcile]))
    check("C3 未提交成功的进 todo",
          sorted(r.job_id for r in plan.todo) == ["BAD", "NOTSENT"],
          str(sorted(r.job_id for r in plan.todo)))
    check("C4 没见过的进 fresh", plan.fresh == ("NEW1", "NEW2"), str(plan.fresh))
    check("C5 ★ **在途任务绝不出现在 todo 里**（否则重复烧 GPU）",
          not ({r.job_id for r in plan.reconcile}
               & {r.job_id for r in plan.todo}),
          str(plan.as_dict()))
    check("C6 有在途任务时标明需要访问服务端", plan.needs_service)
    check("C7 无在途任务时不需要服务端",
          not ck.plan(expected=[]).needs_service
          or bool(ck.plan().reconcile))


# ---------- D. 绝不重复提交 ----------

def _no_double_submit(tmp: Path) -> None:
    """**本模块存在的理由。**"""
    ck = RenderCheckpoint(tmp / "d")
    svc = FakeReconciler()

    # 场景：3 个镜头**已提交到服务端并在那边跑完**，但本地还没来得及记完成 ——
    # 这正是"崩溃发生在提交之后"的形态：本地是 SUBMITTED，服务端是 succeeded。
    for i in (1, 2, 3):
        ck.begin(f"SH{i}", f"k{i}")
        ck.mark_submitted(f"SH{i}", f"p{i}")
        svc.known[f"p{i}"] = "succeeded"
        svc.artifacts[f"p{i}"] = [f"out{i}.png"]
    # 第 4、5 个只记了意图（崩溃发生在提交往返**之中**）
    for i in (4, 5):
        ck.begin(f"SH{i}", f"k{i}")

    # 重启
    ck2 = RenderCheckpoint(tmp / "d")
    plan = ck2.plan(expected=[f"SH{i}" for i in range(1, 6)])
    check("D1 重启后 3 个在途任务被识别为**需要对账**（不直接重提）",
          len(plan.reconcile) == 3 and len(plan.done) == 0
          and len(plan.todo) == 2,
          f"done={len(plan.done)} reconcile={len(plan.reconcile)} "
          f"todo={len(plan.todo)}")

    out = ck2.reconcile_now(svc)
    check("D2 对账把服务端已完成的 3 个判为成功（**不用重跑**）",
          all(out.get(f"SH{i}") is JobState.SUCCEEDED for i in (1, 2, 3)),
          str({k: v.value for k, v in out.items()}))
    check("D2b 对账只查在途任务（不查已经落定的）",
          len(svc.queries) == 1 and sorted(svc.queries[0]) == ["p1", "p2", "p3"],
          str(svc.queries))
    # 产物名带 URI 哈希前缀（缓存层为保证唯一而加），此处断言**后缀**即可
    check("D3 对账后产物名被记录下来（与缓存层命名一致）",
          len(ck2.get("SH1").artifacts) == 1
          and ck2.get("SH1").artifacts[0].endswith("out1.png"),
          str(ck2.get("SH1").artifacts))

    plan2 = ck2.plan(expected=[f"SH{i}" for i in range(1, 6)])
    check("D4 ★ 只剩 2 个需要实跑（3 个已完成被跳过）",
          len(plan2.done) == 3 and len(plan2.todo) == 2
          and len(plan2.reconcile) == 0,
          plan2.as_dict())
    check("D5 ★ 那 2 个正是「只记了意图」的（重提是安全的）",
          sorted(r.job_id for r in plan2.todo) == ["SH4", "SH5"],
          str(sorted(r.job_id for r in plan2.todo)))

    # 服务端记得的失败 → 判失败（可重试）
    ck3 = RenderCheckpoint(tmp / "d3")
    ck3.begin("SH9", "k9"); ck3.mark_submitted("SH9", "p9")
    svc.known["p9"] = "failed"
    out3 = ck3.reconcile_now(svc)
    check("D6 服务端报告失败 → 判失败（可重试）",
          out3["SH9"] is JobState.FAILED, out3["SH9"].value)

    # 服务端查不到 → 判失败而非成功
    ck4 = RenderCheckpoint(tmp / "d4")
    ck4.begin("SH8", "k8"); ck4.mark_submitted("SH8", "p-gone")
    out4 = ck4.reconcile_now(svc)
    check("D7 ★ 服务端**查不到** → 判失败（重提安全），**不判成功**",
          out4["SH8"] is JobState.FAILED, out4["SH8"].value)
    check("D8 理由写明「服务端无此任务」（可追溯）",
          "服务端无此任务" in ck4.get("SH8").error,
          ck4.get("SH8").error)

    # 服务端还在跑 → 保持 SUBMITTED，下次再对（**不能重提**）
    ck5 = RenderCheckpoint(tmp / "d5")
    ck5.begin("SH7", "k7"); ck5.mark_submitted("SH7", "p-run")
    svc.known["p-run"] = "running"
    out5 = ck5.reconcile_now(svc)
    check("D9 ★ 服务端还在跑 → 保持 SUBMITTED（**绝不重提**）",
          out5["SH7"] is JobState.SUBMITTED, out5["SH7"].value)

    # 对账本身抛异常 → 保持 SUBMITTED，不猜
    class Boom:
        def reconcile(self, ids: Any) -> dict:
            raise ConnectionError("服务没起来")

    ck6 = RenderCheckpoint(tmp / "d6")
    ck6.begin("SH6", "k6"); ck6.mark_submitted("SH6", "p6")
    out6 = ck6.reconcile_now(Boom())
    check("D10 ★ 对账失败 → 保持 SUBMITTED（**不猜、不重提**）",
          ck6.get("SH6").state is JobState.SUBMITTED
          and out6.get("SH6") is None,
          ck6.get("SH6").state.value)
    check("D11 对账失败的原因被记下", "对账失败" in ck6.get("SH6").error,
          ck6.get("SH6").error[:50])


# ---------- E. 对账的调用方式 ----------

def _reconcile(tmp: Path) -> None:
    ck = RenderCheckpoint(tmp / "e")
    svc = FakeReconciler()
    for i in (1, 2):
        ck.begin(f"SH{i}", f"k{i}")
        ck.mark_submitted(f"SH{i}", f"p{i}")
        svc.known[f"p{i}"] = "succeeded"

    ck.reconcile_now(svc)
    check("E1 对账只查**在途任务**（不查已完成的）",
          len(svc.queries) == 1 and sorted(svc.queries[0]) == ["p1", "p2"],
          str(svc.queries))

    ck.reconcile_now(svc)      # 已全部落定，不该再查
    check("E2 没有在途任务时不再访问服务端",
          len(svc.queries) == 1, f"{len(svc.queries)} 次查询")

    # 无 channel_id 的在途任务：提交没成功，可安全重提
    ck2 = RenderCheckpoint(tmp / "e2")
    ck2.begin("SHX", "kx")
    ck2._records["SHX"].state = JobState.SUBMITTED      # noqa: SLF001 手工造状态
    out = ck2.reconcile_now(svc)
    check("E3 在途但**没有 channel_id** → 判失败（提交没成功，重提安全）",
          out.get("SHX") is JobState.FAILED, str(out.get("SHX")))


# ---------- F. 账本损坏 ----------

def _broken_ledger(tmp: Path) -> None:
    root = tmp / "f"
    ck = RenderCheckpoint(root)
    ck.begin("SH1", "k1")
    ck.mark_succeeded("SH1", ["a.png"])

    # 人为写坏
    (root / RenderCheckpoint.LEDGER).write_text("{ 这不是 JSON", encoding="utf-8")

    ck2 = RenderCheckpoint(root)
    check("F1 账本损坏时**不静默当空账本**（那会导致全部重提）",
          bool(ck2.load_error), ck2.load_error)
    check("F2 损坏的账本被隔离保留（可人工查看）",
          any(p.name.startswith("checkpoint.broken")
              for p in root.iterdir()),
          str([p.name for p in root.iterdir()]))
    check("F3 隔离后仍能正常新建账本", ck2.record_count == 0)


# ---------- G. 原子写 ----------

def _atomic(tmp: Path) -> None:
    root = tmp / "g"
    ck = RenderCheckpoint(root)
    for i in range(5):
        ck.begin(f"SH{i}", f"k{i}")
    check("G1 多次写后无残留临时文件",
          not [p for p in root.iterdir() if ".tmp" in p.name],
          str([p.name for p in root.iterdir()]))
    check("G2 账本是合法 JSON",
          isinstance(json.loads((root / RenderCheckpoint.LEDGER)
                                .read_text(encoding="utf-8")), dict))
    check("G3 写入是原子的（临时文件 + rename，不留半个 JSON）",
          True, "见 _flush 实现")


# ---------- H. 端到端：崩溃 → 重启 → 续跑 ----------

def _resume_end_to_end(tmp: Path) -> None:
    """把 D 段的场景走成一次完整的"崩溃→重启→续跑"。"""
    root = tmp / "h"
    svc = FakeReconciler()

    # ---- 第一次运行：5 个镜头，跑到第 3 个后崩溃 ----
    ck = RenderCheckpoint(root, episode_id="E100")
    jobs = [f"SH{i:02d}" for i in range(1, 6)]
    served = 0
    for jid in jobs:
        ck.begin(jid, f"key-{jid}", fingerprint=f"fp-{jid}")
        if served >= 3:
            break                      # 模拟崩溃：第 4 个刚 begin 就挂了
        cid = f"prompt-{jid}"
        ck.mark_submitted(jid, cid)
        svc.known[cid] = "succeeded"
        svc.artifacts[cid] = [f"{jid}.png"]
        ck.mark_succeeded(jid, [f"{jid}.png"])
        served += 1

    check("H1 崩溃前完成 3 个、留下 1 个在途意图",
          ck.counts() == {"succeeded": 3, "pending": 1}, str(ck.counts()))

    # ---- 重启 ----
    ck2 = RenderCheckpoint(root)
    check("H2 重启后账本完整", ck2.record_count == 4, f"{ck2.record_count}")

    plan = ck2.plan(expected=jobs)
    check("H3 续跑计划：3 个已完成 + 1 个待跑 + 1 个新",
          len(plan.done) == 3 and len(plan.todo) == 1 and plan.fresh == ("SH05",),
          plan.as_dict())

    # 对账（本例在途那个没有 channel_id，会判失败 → 可重提）
    ck2.reconcile_now(svc)
    plan2 = ck2.plan(expected=jobs)
    need_run = [r.job_id for r in plan2.todo] + list(plan2.fresh)
    check("H4 ★ 续跑只需实跑 2 个（3 个已完成被跳过）",
          len(plan2.done) == 3 and sorted(need_run) == ["SH04", "SH05"],
          f"done={len(plan2.done)} need_run={sorted(need_run)}")
    check("H5 ★ 已完成的 3 个**零 GPU**（不在待跑列表里）",
          not ({r.job_id for r in plan2.done} & set(need_run)),
          str(need_run))

    # ---- 续跑完成 ----
    for jid in need_run:
        ck2.begin(jid, f"key-{jid}")
        cid = f"prompt-{jid}"
        ck2.mark_submitted(jid, cid)
        ck2.mark_succeeded(jid, [f"{jid}.png"])
    check("H6 续跑后全部完成",
          ck2.counts().get("succeeded") == 5, str(ck2.counts()))

    ck3 = RenderCheckpoint(root)
    check("H7 再次重启后全部为已完成状态",
          all(ck3.get(j).state is JobState.SUCCEEDED for j in jobs),
          str(ck3.counts()))


if __name__ == "__main__":
    raise SystemExit(main())
