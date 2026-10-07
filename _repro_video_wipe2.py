"""对抗验证 2/2：视频登记被抹掉的两种触发路径（不改被测代码）。

路径 A：图自己提交的后台视频任务在「本轮图结束前」完成 → 被后续 save(..., None) / result 覆盖。
路径 B：会话停在确认点期间手动触发视频（等价 POST /api/crew/video）→ 用户点「通过」
        时 resume_run 的 save(..., "running", "resume", None) 立刻抹掉。

两种路径都保留「产物确实落到了磁盘」这一事实，用来验证报告里
「mp4 仍在磁盘，但谁也看不到它」的说法。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def _isolate():
    from app.core.config import settings
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="wipe2_"))
    settings.mock_llm = True
    settings.mock_image = True
    settings.mock_quality = True
    settings.mock_critics = True
    settings.mock_video = True
    settings.output_dir = str(tmp / "outputs")
    settings.db_path = str(tmp / "sessions.db")
    settings.db_url = f"sqlite:///{(tmp / 'sessions.db').as_posix()}"
    from app.services import session_store as ss
    ss._store = None
    return tmp, ss.get_session_store()


def _wait(store, sid, targets, timeout=240.0):
    deadline = time.time() + timeout
    row = {}
    while time.time() < deadline:
        row = store.get(sid) or {}
        if row.get("status") in targets:
            return row
        time.sleep(0.2)
    return row


def _wait_video_thread(sid, timeout=60.0):
    from app.services import post_production as pp
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = pp._JOBS.get(sid)
        if t is None or not t.is_alive():
            return
        time.sleep(0.2)


def _disk_of(path_str: str) -> list:
    """dir_or_path 现在在磁盘上还剩什么。"""
    p = pathlib.Path(path_str)
    if not p.exists():
        return []
    if p.is_file():
        return [f"{p.name} ({p.stat().st_size}B)"]
    return [f"{f.name} ({f.stat().st_size}B)" for f in sorted(p.rglob("*")) if f.is_file()]


def scenario_a(tmp, store):
    print("\n########## 路径 A：图内后台视频在图结束前完成 ##########")
    from app.services import session_store as ss
    from app.services import task_runner as tr

    captured = []
    orig_save = ss.SessionStore.save

    def spy(self, sid, uid, uin, st, ph, payload):
        if isinstance(payload, dict) and payload.get("video"):
            captured.append(payload["video"])
        return orig_save(self, sid, uid, uin, st, ph, payload)

    ss.SessionStore.save = spy
    try:
        sid = "A_motion"
        tr.start_run(sid, "验证图内视频登记会不会被覆盖", "tester")
        row = _wait(store, sid, {"done", "error", "cancelled"})
        _wait_video_thread(sid)
        row = store.get(sid) or {}
    finally:
        ss.SessionStore.save = orig_save

    payload = row.get("payload") or {}
    print("最终 status:", row.get("status"), "| phase:", row.get("phase"))
    print("视频后台任务写库时登记的 video:", captured)
    print("最终 payload 里有 video 吗:", "video" in payload)
    for v in captured:
        print("  该产物在磁盘上:", _disk_of(v.get("dir_or_path", "")))
    print("assess_video 现在会看到:", _assess(store, sid))
    return "video" in payload


def _assess(store, sid):
    from app.services.post_production import assess_video
    return assess_video({"session_id": sid}).get("video_assessor_note")


def scenario_b(tmp, store):
    print("\n########## 路径 B：确认点暂停期间手动触发视频，再点「通过」 ##########")
    from app.core.config import settings
    settings.hitl = True
    from app.services import task_runner as tr
    from app.services import post_production as pp

    sid = "B_pause"
    tr.start_run(sid, "验证暂停期间的手动视频会不会被 resume 抹掉", "tester")
    row = _wait(store, sid, {"review", "done", "error"})
    print("停在:", row.get("status"), "| review_point:",
          (row.get("payload") or {}).get("review_point"))

    # 等价于 POST /api/crew/video/{sid}（同一套读写 payload 的代码）
    pp._run_video_job(sid, "tester", "手动触发的视频", [{"shot_id": "1"}], {})
    _wait_video_thread(sid)
    mid = store.get(sid) or {}
    vid = (mid.get("payload") or {}).get("video")
    print("手动触发后 payload['video']:", vid)
    print("  产物在磁盘上:", _disk_of((vid or {}).get("dir_or_path", "")))

    started, msg = tr.resume_run(sid, "tester", "approve", {})
    print("resume_run ->", started, msg)
    after = store.get(sid) or {}
    print("「通过」刚返回时 payload:", repr(after.get("payload"))[:120])
    print("  此刻 payload 里还有 video 吗:", "video" in (after.get("payload") or {}))

    row2 = _wait(store, sid, {"done", "error", "cancelled"})
    _wait_video_thread(sid)
    row2 = store.get(sid) or {}
    p2 = row2.get("payload") or {}
    print("最终 status:", row2.get("status"), "| 最终 payload 里有 video 吗:", "video" in p2)
    print("assess_video 现在会看到:", _assess(store, sid))
    print("  手动那次产生的产物仍在磁盘:", _disk_of((vid or {}).get("dir_or_path", "")))
    return "video" in p2


if __name__ == "__main__":
    tmp, store = _isolate()
    a = scenario_a(tmp, store)
    b = scenario_b(tmp, store)
    print("\n========== 结论 ==========")
    print("路径 A 最终 payload 保留 video:", a)
    print("路径 B 最终 payload 保留 video:", b)
