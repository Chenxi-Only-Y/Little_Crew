"""对抗验证：视频登记是否会被 task_runner 的整列覆盖抹掉。

不改任何被测代码；只用真实的 start_run / 真实图 / 真实 session_store，
在 SessionStore.save 外面套一层记录器，看每次写库时 payload 里有没有 video。

用法：.venv\\Scripts\\python.exe _repro_video_wipe.py [轮数]
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

T0 = time.time()
EVENTS: list[tuple] = []


def main(rounds: int = 2) -> int:
    from app.core.config import settings

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="wipe_"))
    settings.mock_llm = True
    settings.mock_image = True
    settings.mock_quality = True
    settings.mock_critics = True
    settings.mock_video = True
    settings.hitl = False                      # 一口气跑完，不暂停
    settings.output_dir = str(tmp / "outputs")
    settings.db_path = str(tmp / "sessions.db")
    settings.db_url = f"sqlite:///{(tmp / 'sessions.db').as_posix()}"

    from app.services import session_store as ss

    ss._store = None                            # 强制用上面的临时库
    store = ss.get_session_store()

    orig_save = ss.SessionStore.save

    def spy(self, session_id, user_id, user_input, status, phase, payload):
        if payload is None:
            kind = "NULL<-payload=None"
        elif isinstance(payload, dict) and payload.get("video"):
            kind = "HAS-VIDEO"
        elif isinstance(payload, dict):
            kind = "dict(no video)"
        else:
            kind = type(payload).__name__
        EVENTS.append((round(time.time() - T0, 3), session_id, status, phase, kind))
        return orig_save(self, session_id, user_id, user_input, status, phase, payload)

    ss.SessionStore.save = spy

    verdicts = []
    for i in range(rounds):
        sid = f"wipe_{i}"
        EVENTS.clear()
        from app.services import task_runner as tr

        tr.start_run(sid, "验证视频登记是否会被覆盖", "tester")

        deadline = time.time() + 240
        status = ""
        row = {}
        while time.time() < deadline:
            row = store.get(sid) or {}
            status = row.get("status", "")
            if status in ("done", "error", "cancelled"):
                break
            time.sleep(0.2)

        # 后台视频线程可能还在跑 —— 等它收尾（_JOBS 为空说明线程函数已返回）
        from app.services import post_production as pp
        pp_deadline = time.time() + 30
        while time.time() < pp_deadline:
            t = pp._JOBS.get(sid)
            if t is None or not t.is_alive():
                break
            time.sleep(0.2)

        row = store.get(sid) or {}
        payload = row.get("payload") or {}
        disk = sorted(str(p.relative_to(tmp)) for p in (tmp / "outputs").rglob("*")
                      if p.is_file() and ("video" in str(p).lower() or p.suffix == ".mp4"))

        print(f"\n===== 第 {i + 1} 轮 session={sid} =====")
        print("最终 status :", row.get("status"), "phase:", row.get("phase"))
        print("payload 顶层键:", sorted(payload)[:12], "...")
        print("payload['video'] 存在吗 :", "video" in payload, "->", payload.get("video"))
        print("磁盘上视频产物 :", disk[:5])
        print("写库时间线（只列关键项）:")
        for ev in EVENTS:
            print("   +%.3fs  %-8s %-14s %s" % (ev[0], ev[2], ev[3], ev[4]))

        verdicts.append({
            "sid": sid, "status": row.get("status"),
            "video_in_payload": "video" in payload,
            "video_artifact_on_disk": bool(disk),
        })

    print("\n===== 汇总 =====")
    for v in verdicts:
        print(v)
    return 0


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    raise SystemExit(main(n))
