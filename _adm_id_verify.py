"""对抗验证（独立复现）：graph 跑真实工作流时，动态合成师拿到的身份是谁？

验证三条：
  A. composite_motion 眼里的 graph state 是否有 "user_id" 键（报告说有/没有？）
  B. 后台视频任务线程拿到的 user_id 参数是什么（报告的 "local"？）
  C. 后台任务落盘后，会话行的属主 user_id 变成什么（副作用）
  D. 视频服务在该线程里读到的运行时设置是哪一份（alice 的还是 _global 的）
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent
TMP = pathlib.Path(tempfile.mkdtemp(prefix="adm_id_"))
os.environ["DB_URL"] = f"sqlite:///{(TMP / 't.db').as_posix()}"
os.environ["DB_PATH"] = str(TMP / "t.db")
os.environ["OUTPUT_DIR"] = str(TMP / "out")
for k, v in {
    "MOCK_LLM": "1", "MOCK_IMAGE": "1", "MOCK_VIDEO": "1",
    "MOCK_QUALITY": "1", "MOCK_CRITICS": "1", "HITL_ENABLED": "0",
    "LITTLE_CREW_SKIP_CLIP": "1",
}.items():
    os.environ[k] = v
sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402
from app.services import runtime_settings as rs  # noqa: E402
from app.services import task_runner as tr  # noqa: E402
from app.services.session_store import get_session_store  # noqa: E402
import app.services.post_production as pp  # noqa: E402
import app.services.video as vsvc  # noqa: E402

print("DB =", settings.db_url)
seen: dict = {}

# 给 alice 配一份"只有她自己有"的视频密钥；_global 留空 → 用来区分读到的是谁的设置
rs.set_many("alice", {"minimax_api_key": "ALICE-KEY", "video_provider": "minimax"})


class _FakeVideo:
    """替身视频服务：记录本线程解析到的身份与密钥，睡 3 秒模拟真实耗时。"""

    def synthesize(self, images, script, out_dir, session_id, text_prompt=""):
        seen["job_thread_user"] = rs.user()
        seen["job_thread_session"] = rs.run_session()
        seen["job_thread_key"] = rs.get("minimax_api_key")
        time.sleep(3.0)                      # 真实视频要几分钟；这里保证晚于图结束
        return (str(pathlib.Path(out_dir) / "fake_video"), 2)


vsvc.get_video_service = lambda *a, **k: _FakeVideo()

_orig_cm = pp.composite_motion
_orig_job = pp._run_video_job


def _spy_cm(state):
    seen["cm_called"] = True
    seen["state_has_user_id"] = "user_id" in state
    seen["state_user_id"] = state.get("user_id")
    seen["state_keys"] = sorted(state.keys())
    seen["run_context"] = state.get("run_context")
    seen["parent_thread_user"] = rs.user()          # 节点所在线程（图线程）的身份
    return _orig_cm(state)


def _spy_job(session_id, user_id, *a, **k):
    seen["job_arg_user_id"] = user_id
    return _orig_job(session_id, user_id, *a, **k)


pp.composite_motion = _spy_cm
pp._run_video_job = _spy_job

SID = "adm_id_sess_1"
tr.start_run(SID, "对抗验证：身份", "alice")
store = get_session_store()

deadline = time.time() + 120
while time.time() < deadline:
    row = store.get(SID) or {}
    if seen.get("job_thread_user") and row.get("status") in ("done", "error", "review"):
        break
    time.sleep(0.3)

row_after_graph = store.get(SID) or {}
print("\n--- 图刚跑完 ---")
print("status           =", row_after_graph.get("status"))
print("row.user_id      =", row_after_graph.get("user_id"))
print("payload.video    =", (row_after_graph.get("payload") or {}).get("video"))

# 等后台视频任务落盘（它写 store.save(sid, user_id=<背景任务的身份>, ...)）
deadline = time.time() + 60
rowed = None
while time.time() < deadline:
    rowed = store.get(SID) or {}
    if (rowed.get("payload") or {}).get("video"):
        break
    time.sleep(0.3)

print("\n--- 观测 ---")
print("composite_motion 被调用    :", seen.get("cm_called"))
print("state 里有 user_id 键      :", seen.get("state_has_user_id"), "值 =", repr(seen.get("state_user_id")))
print("run_context                :", seen.get("run_context"))
print("图线程里 rs.user()         :", seen.get("parent_thread_user"))
print("传给后台线程的 user_id     :", repr(seen.get("job_arg_user_id")))
print("后台线程里 rs.user()       :", repr(seen.get("job_thread_user")))
print("后台线程里读到的 minimax key:", repr(seen.get("job_thread_key")))
print("alice 自己那份 key         :", repr(rs.snapshot("alice").get("minimax_api_key")))
print("\n--- 最终会话行 ---")
print("row.user_id =", (rowed or {}).get("user_id"))
print("row.payload.video =", ((rowed or {}).get("payload") or {}).get("video"))

print("\n--- 无 writer 复核 ---")
from app.core.state import CrewState  # noqa: E402
for k in ("user_id", "video_regenerate", "retry_shots"):
    print(f"  {k!r} in CrewState:", k in CrewState.__annotations__,
          "| _sanitize_seed:", tr._sanitize_seed({k: True, "mock": True}))
