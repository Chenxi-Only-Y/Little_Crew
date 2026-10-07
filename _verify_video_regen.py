"""对抗验证：post_production.py:91 的 `video_regenerate` 分支。

要回答两个**不同**的问题：
  A. 这个条件真的永远为假吗？（= 只有手工注入才走得到重跑分支）
  B. 报告宣称的后果（"用户无法重新生成视频"）真的会发生吗？
     —— 关键：图跑到 composite_motion 时，会话 payload 里到底有没有视频？

B 才是"bug 是否成立"的关键。做法：跑**真实**的工作流（mock 全开、hitl 关），
用 spy 记录 composite_motion 眼里 `_session_payload()` 的返回值，
并把 `_run_video_job`（真起后台线程去合成视频）换成记录器 —— 只看"是否提交"。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

# 隔离运行时数据（别污染真实 data/）
_TMP = tempfile.mkdtemp(prefix="verify_video_regen_")
os.environ["DB_PATH"] = os.path.join(_TMP, "verify.db")
os.environ["OUTPUT_DIR"] = os.path.join(_TMP, "out")

from app.core.config import settings  # noqa: E402

settings.mock_llm = True
settings.mock_image = True
settings.mock_video = True
settings.mock_critics = True
settings.mock_quality = True
settings.hitl = False          # 一口气跑完整张图

from app.services import task_runner as tr          # noqa: E402
from app.services import post_production as pp     # noqa: E402
from app.services.session_store import get_session_store  # noqa: E402

store = get_session_store()

# --- spy 1：composite_motion 眼里的 payload ---------------------------------
_orig_payload = pp._session_payload
_seen: list[tuple[str, dict]] = []


def _spy_payload(state):
    got = _orig_payload(state)
    _seen.append((str(state.get("session_id") or ""), got))
    return got


pp._session_payload = _spy_payload

# --- spy 2：后台视频任务是否被提交 ------------------------------------------
_submits: list[str] = []


def _fake_job(session_id, *a, **kw):
    _submits.append(str(session_id))


pp._run_video_job = _fake_job


def wait(sid: str, timeout: float = 120.0) -> dict:
    end = time.time() + timeout
    row = {}
    while time.time() < end:
        row = store.get(sid) or {}
        if row.get("status") in ("done", "error", "review", "cancelled"):
            return row
        time.sleep(0.2)
    return row


def run_graph(sid: str, label: str) -> dict:
    _seen.clear()
    _submits.clear()
    tr.start_run(sid, "验证视频重跑", "tester")
    immediately = store.get(sid) or {}
    row = wait(sid)
    print(f"\n=== {label}（sid={sid}）===")
    print("  start_run 刚返回时 store payload =", repr(immediately.get("payload")))
    print("  最终 status =", row.get("status"))
    print("  composite_motion 看到的 payload：")
    for s, p in _seen:
        print("     ", s, "->", repr(p))
    print("  提交的视频后台任务次数 =", len(_submits), _submits)
    return row


# ---------------------------------------------------------------------------
# 场景 1：干净会话（payload 空）跑一遍 —— 基线
# ---------------------------------------------------------------------------
sid1 = "verify_clean_1"
run_graph(sid1, "场景1 干净会话")

# ---------------------------------------------------------------------------
# 场景 2：报告描述的场景 —— 同一个会话 payload 里**已有 produced=4 的视频**，
#         然后再跑一次图（"redo/重跑"）。
# ---------------------------------------------------------------------------
sid2 = "verify_has_video_1"
tr.start_run(sid2, "验证视频重跑", "tester")
wait(sid2)                                            # 先让它跑完一次
row2 = store.get(sid2) or {}
# 手工把一条 produced=4 的视频写进 payload —— 等价于「用户此前已经生成过视频」
prev = dict(row2.get("payload") or {})
prev["video"] = {"dir_or_path": "v/", "produced": 4}
prev["images"] = [{"shot_id": "S1-01", "local_path": "x.png"}]
store.save(sid2, row2.get("user_id") or "tester", row2.get("user_input", ""),
           "done", "done", prev)
print("\n[准备] 重跑前 store payload =", repr(store.get(sid2)["payload"]))
run_graph(sid2, "场景2 已有视频的会话再跑一次图")

# ---------------------------------------------------------------------------
# 场景 3（反证用）：直接把 payload 有视频的 state 交给节点本身
#   —— 这才是报告描述的"命中 92 行"。
# ---------------------------------------------------------------------------
print("\n=== 场景3 直接调用节点（payload 确有视频） ===")
_direct_submits = []
pp._run_video_job = lambda sid, *a, **kw: _direct_submits.append(sid)
state = {"session_id": sid2, "images": [{"shot_id": "S1-01"}],
         "review_decision": "redo", "feedback": {"note": "重做"}}
out = pp.composite_motion(state)
print("  节点返回 motion_note =", out.get("motion_note"))
print("  提交任务次数 =", len(_direct_submits))
state2 = dict(state, video_regenerate=True)
pp._JOBS.clear()
out2 = pp.composite_motion(state2)
print("  （对照）显式 video_regenerate=True 时的 note =", out2.get("motion_note"))
print("  提交任务次数 =", len(_direct_submits))

# ---------------------------------------------------------------------------
# 场景 4：seed 白名单是否真的丢掉 video_regenerate
# ---------------------------------------------------------------------------
from app.services.task_runner import _sanitize_seed  # noqa: E402

print("\n=== 场景4 seed 白名单 ===")
print("  _sanitize_seed({'video_regenerate': True, 'mock': True}) =",
      _sanitize_seed({"video_regenerate": True, "mock": True}))
print("\n临时目录：", _TMP)
