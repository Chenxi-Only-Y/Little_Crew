"""对抗验证（第 2 部分）：报告点名的 "redo/重跑" 路径到底会不会命中 92 行的跳过。

两个子实验：
  A. 直接调用节点 + payload 里**确有** produced=4 的视频
     → 证实"没有 video_regenerate 时确实跳过"（报告对代码的读法没错）
  B. hitl=True 走**真实续跑（redo/approve）**链路：
     每次 resume 之前都把 produced=4 的视频写进 payload，然后看
     composite_motion 眼里到底有没有视频、有没有提交任务。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="verify_video_regen2_")
os.environ["DB_PATH"] = os.path.join(_TMP, "verify.db")
os.environ["OUTPUT_DIR"] = os.path.join(_TMP, "out")

from app.core.config import settings  # noqa: E402

settings.mock_llm = True
settings.mock_image = True
settings.mock_video = True
settings.mock_critics = True
settings.mock_quality = True

from app.services import post_production as pp          # noqa: E402
from app.services import task_runner as tr             # noqa: E402
from app.services.session_store import get_session_store  # noqa: E402

store = get_session_store()

_orig_payload = pp._session_payload
_seen: list[tuple[str, dict]] = []


def _spy(state):
    got = _orig_payload(state)
    _seen.append((str(state.get("session_id") or ""), got))
    return got


pp._session_payload = _spy
_submits: list[str] = []
pp._run_video_job = lambda sid, *a, **kw: _submits.append(str(sid))


def wait(sid, targets, timeout=120.0):
    end = time.time() + timeout
    row = {}
    while time.time() < end:
        row = store.get(sid) or {}
        if row.get("status") in targets:
            return row
        time.sleep(0.2)
    return row


def put_video(sid, produced=4):
    """把 produced=4 的视频写进 payload，**保留** status/phase（否则会破坏 review 前置条件）。"""
    row = store.get(sid) or {}
    pl = dict(row.get("payload") or {})
    pl["video"] = {"dir_or_path": "v/", "produced": produced}
    pl["images"] = [{"shot_id": "S1-01", "local_path": "x.png"}]
    store.save(sid, row.get("user_id") or "tester", row.get("user_input", ""),
               row.get("status") or "review", row.get("phase") or "review", pl)


# ---------------------------------------------------------------------------
# A. 直接调用节点（payload 确有视频）—— 报告对第 91 行行为的描述对不对
# ---------------------------------------------------------------------------
sidA = "verify_direct_a"
store.save(sidA, "tester", "输入", "done", "done",
           {"video": {"dir_or_path": "v/", "produced": 4}})
print("=== A. 直接调用 composite_motion（payload 确有 produced=4 视频） ===")
n0 = len(_submits)
out = pp.composite_motion({"session_id": sidA, "images": [{"shot_id": "1"}]})
print("  无标志  -> note =", out.get("motion_note"), "| 新提交 =", len(_submits) - n0)
n1 = len(_submits)
out = pp.composite_motion({"session_id": sidA, "images": [{"shot_id": "1"}],
                           "video_regenerate": True})
print("  带标志  -> note =", out.get("motion_note"), "| 新提交 =", len(_submits) - n1)

# ---------------------------------------------------------------------------
# B. 真实 redo/续跑链路（hitl=True），每次 resume 前都塞入 produced=4 的视频
# ---------------------------------------------------------------------------
settings.hitl = True
sidB = "verify_resume_b"
_seen.clear()
_submits.clear()
print("\n=== B. hitl=True 真实确认点续跑（每次 resume 前都写 produced=4 的视频） ===")
tr.start_run(sidB, "续跑视频验证", "tester")
row = wait(sidB, {"review", "done", "error"})
print("  首轮停在 =", row.get("status"), "review_point =",
      (row.get("payload") or {}).get("review_point"))

for i in range(12):
    if row.get("status") != "review":
        break
    put_video(sidB)                     # 模拟"会话 payload 里已有 4 段视频"
    before = (store.get(sidB).get("payload") or {}).get("video")
    ok, msg = tr.resume_run(sidB, "tester", "approve", {})
    after = (store.get(sidB).get("payload") or {}).get("video")
    print(f"  resume#{i + 1} accepted={ok} | resume 前 payload.video={before}"
          f" | resume 返回后 payload.video={after}")
    if not ok:
        break
    row = wait(sidB, {"review", "done", "error"})

print("  最终 status =", row.get("status"))
print("  composite_motion 在这次会话里看到的 payload：")
for s, p in _seen:
    v = (p or {}).get("video") if isinstance(p, dict) else None
    print(f"     sid={s} payload_empty={not p} payload.video={v}")
print("  提交的视频后台任务次数 =", len(_submits), _submits)
print("\n临时目录：", _TMP)
