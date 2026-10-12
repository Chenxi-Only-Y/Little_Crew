"""会话 payload 的**不丢失**保证。

## 为什么需要

2026-10-07 节点排查实测到一次静默数据丢失：

    视频生成是**独立的异步后台任务**，结果写进会话 `payload["video"]`；
    而工作流**每过一个节点**都会调 `save(..., payload=None)` 保存进度。
    原先 `save()` 对 payload 是"整列覆盖，None 就写 NULL"，于是：

        节点线程:   save(payload=None)        → payload 列被写成 NULL
        视频线程:   save(payload={"video":…}) → 视频登记写进去
        下一个节点: save(payload=None)        → **视频登记被抹掉**

    mp4 还在磁盘上，但 `/api/crew/status`、`assess_video`、发行都再也看不到它。
    mock/本地（视频很快）下稳定触发；确认点暂停期间手动触发也必现。

修法：`payload=None` 改成"只更新状态、**不改动** payload"。
"""

from __future__ import annotations

import time

import pytest


@pytest.fixture
def store():
    from app.services.session_store import get_session_store

    return get_session_store()


@pytest.fixture
def sid(store):
    s = f"test-nowipe-{int(time.time() * 1000)}"
    yield s
    try:
        store.delete(s)
    except Exception:  # noqa: BLE001
        pass


def test_none_payload_does_not_wipe(store, sid):
    """**最关键的一条**：进度保存（payload=None）不能抹掉已有的产物。"""
    store.save(sid, "t", "一句话", "running", "a", {"images": [{"shot_id": "1"}]})
    store.save(sid, "t", "一句话", "running", "b", None)     # 模拟节点进度保存
    got = store.get(sid)["payload"]
    assert got == {"images": [{"shot_id": "1"}]}, f"payload 被抹了：{got}"


def test_video_registration_survives_later_progress_saves(store, sid):
    """复现原始 bug 的三步序列：视频登记必须活下来。"""
    store.save(sid, "t", "一句话", "running", "a", None)
    store.save(sid, "t", "一句话", "running", "b",
               {"images": [], "video": {"dir_or_path": "v/", "produced": 3}})
    store.save(sid, "t", "一句话", "running", "c", None)      # ← 原先这里抹掉
    payload = store.get(sid)["payload"]
    assert payload.get("video", {}).get("produced") == 3, \
        f"视频登记被后续进度保存抹掉了：{payload}"


def test_explicit_empty_dict_still_clears(store, sid):
    """显式传 `{}` 仍然清空 —— 语义明确，不误伤。"""
    store.save(sid, "t", "一句话", "running", "a", {"images": [1]})
    store.save(sid, "t", "一句话", "done", "done", {})
    assert store.get(sid)["payload"] == {}


def test_status_and_phase_still_update_with_none_payload(store, sid):
    """`None` 只跳过 payload，**状态字段照常更新**（否则进度就不动了）。"""
    store.save(sid, "t", "一句话", "running", "a", {"x": 1})
    store.save(sid, "t", "一句话", "done", "done", None)
    row = store.get(sid)
    assert row["status"] == "done"
    assert row["phase"] == "done"
    assert row["payload"] == {"x": 1}


def test_new_session_with_none_payload_is_allowed(store, sid):
    """新会话传 `None` 也要能建（payload 就是 NULL，没有"已有值"要保）。"""
    store.save(sid, "t", "一句话", "running", "a", None)
    assert store.get(sid)["payload"] is None
