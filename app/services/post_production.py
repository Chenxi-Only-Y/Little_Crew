"""后期三环的节点实现：动态合成师 / 视频质检师 / 修图师。

## 为什么它们"可跳过"是**必须**的，而不是偷懒

视频生成要**几分钟**（多镜头 + 轮询），而工作流节点是**线性同步**的。
把"等视频生成完"塞进图里会**阻塞整张图**。所以：

    动态合成师 —— **提交**后台任务就返回（不阻塞），视频在后台跑完落到会话 payload；
    视频质检师 —— 视频还没好就**跳过**并说明；
    修图师     —— **后端未接入**，跳过并说明（用户："先保留，后续开发，流程直接跳过"）。

用户 2026-10-07 的两条决定：
  · "**动态合成师就是视频生成**" —— 所以它的实现就是调 `get_video_service()`
    那条现成的路（`/api/crew/video` 用的同一套），能力 `video_gen`、产出视频。
  · "**修图师目前先保留，后续开发，流程直接跳过**"。

## 一个真实的坑（写在这里免得再踩）

视频生成的产物**不在图状态里**，而在**会话 payload** 的 `video` 键。
所以这几个节点都要通过 `session_id` 去会话里读 —— 图状态里没有。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger("little_crew")

#: 正在跑的静态视频任务（按会话）。
#:
#: ⚠️ 这是**独立于 HTTP 层**的登记表：`/api/crew/video/{sid}` 用
#: `app/main.py::_VIDEO_JOBS`，图节点用这一张。两张表**不共享**，
#: 所以理论上同一会话可能被提交两次（一次走接口、一次走图）。
#: 目前接受这个代价 —— 视频服务自己会覆盖同名产物，比引入跨模块全局锁简单。
_JOBS: dict[str, threading.Thread] = {}
_JOBS_LOCK = threading.Lock()


def _session_payload(state: dict) -> dict:
    """取当前会话已落盘的产出（视频结果不在图状态里，在会话 payload）。"""
    sid = str(state.get("session_id") or "")
    if not sid:
        return {}
    try:
        from app.services.session_store import get_session_store

        row = get_session_store().get(sid) or {}
        return dict(row.get("payload") or {})
    except Exception as exc:  # noqa: BLE001
        logger.warning("[post] 读取会话产出失败：%s", exc)
        return {}


def _session_row(state: dict) -> dict:
    sid = str(state.get("session_id") or "")
    if not sid:
        return {}
    try:
        from app.services.session_store import get_session_store

        return dict(get_session_store().get(sid) or {})
    except Exception as exc:  # noqa: BLE001
        logger.warning("[post] 读取会话失败：%s", exc)
        return {}


# ----------------------------------------------------------------------
# 动态合成师（= 视频生成）
# ----------------------------------------------------------------------

def composite_motion(state: dict) -> dict:
    """动态合成师节点：**提交**视频生成后台任务（不阻塞图）。

    用户："动态合成师就是视频生成"。所以这里就是把 `get_video_service()` 那条
    现成的路跑起来 —— 与 `POST /api/crew/video/{sid}` 同一套实现。

    **不阻塞**：起一个后台线程就返回。视频好没好由「视频质检师」下一步判断，
    或用户在前端轮询。
    """
    phase = "motion_compositor"
    sid = str(state.get("session_id") or "")
    if not sid:
        return {"video": {}, "current_phase": phase,
                "motion_note": "没有会话 id，无法提交视频生成。"}

    # 已经有视频（payload 里）且这次不是重跑 → 不重复生成
    payload = _session_payload(state)
    existing = payload.get("video") or {}
    if existing.get("produced") and not state.get("video_regenerate"):
        logger.info("[motion] 该会话已有 %s 段视频，跳过重复生成", existing["produced"])
        return {"video": existing, "current_phase": phase,
                "motion_note": f"已有 {existing['produced']} 段视频，未重复生成。"}

    images = state.get("images") or []
    if not images:
        return {"video": {}, "current_phase": phase,
                "motion_note": "还没有图片，无法生成视频（视频以图片为首帧）。"}

    with _JOBS_LOCK:
        running = _JOBS.get(sid)
        if running is not None and running.is_alive():
            return {"video": existing, "current_phase": phase,
                    "motion_note": "视频正在生成中（后台任务已在跑）。"}
        # ⚠️ **必须在这里（节点线程）capture 上下文**，再在线程入口 restore。
        # 只传 user_id/session_id 会**丢掉工程名** → 视频产物落进
        # 「未命名工程」目录，同一个工程的图片/配音在 `{工程名}/`，视频却在
        # `未命名工程/`，按工程打包资产时视频就丢了。
        # 这是实测出来的（图片保住了工程名、视频没有）。
        # `image_generator` 用同一套 capture/restore（那边是 ThreadPoolExecutor）。
        from app.services.runtime_settings import capture as _ctx_capture

        ctx = _ctx_capture()
        t = threading.Thread(
            target=_run_video_job,
            args=(sid, str(state.get("user_id") or "local"),
                  str(state.get("user_input") or ""), images,
                  state.get("script") or {}, ctx),
            daemon=True,
            name=f"video-{sid}",
        )
        _JOBS[sid] = t
        t.start()

    logger.info("[motion] 已提交视频生成后台任务：%s", sid)
    return {
        "video": {},
        "current_phase": phase,
        "motion_note": ("已提交视频生成（后台任务，需要几分钟）。"
                        "它不在流程里等 —— 跑完后到「视频质检师」那一步检查，"
                        "或在前端轮询状态。"),
    }


def _run_video_job(session_id: str, user_id: str, user_input: str,
                   images: list, script: dict, ctx: dict | None = None) -> None:
    """后台线程：合成视频并写回会话 payload。

    ⚠️ **必须在这里 `restore` 上下文**：后台线程不继承 threading.local，
    而运行时设置与**产物归档路径**都靠它。不恢复的话这一线程会用 `_global`
    的设置，且产物落进「未命名工程」目录。

    `ctx` 由调用方（节点线程）用 `runtime_settings.capture()` 抓取 ——
    它带 `project`（工程名），这正是产物归档需要的第二层分组。
    （`app/main.py::_run_video` 是 HTTP 侧的同款实现，那边只 set_context
    user/session，**同样丢了工程名**，是另一个待修点。）
    """
    from app.core.config import settings
    from app.services.runtime_settings import restore as _rs_restore
    from app.services.runtime_settings import set_context as _rs_set_ctx
    from app.services.video import get_video_service

    if ctx:
        _rs_restore(ctx)
    else:
        _rs_set_ctx(user_id, session_id)
    try:
        from app.services.session_store import get_session_store

        store = get_session_store()
        out, produced = get_video_service().synthesize(
            images, script, str(settings.output_dir_path), session_id,
            text_prompt=user_input,
        )
        row = store.get(session_id) or {}
        payload = dict(row.get("payload") or {})
        payload["video"] = {"dir_or_path": str(out), "produced": int(produced)}
        store.save(session_id, user_id, row.get("user_input", user_input),
                   row.get("status", "done"), row.get("phase", "done"), payload)
        logger.info("[motion] 视频完成：%s 段 -> %s", produced, out)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[motion] 视频生成失败：%s", exc)
        try:
            from app.services.session_store import get_session_store

            store = get_session_store()
            row = store.get(session_id) or {}
            payload = dict(row.get("payload") or {})
            payload["video"] = {"dir_or_path": "", "produced": 0,
                                "error": f"{type(exc).__name__}: {exc}"}
            store.save(session_id, user_id, row.get("user_input", user_input),
                       row.get("status", "done"), row.get("phase", "done"), payload)
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            from app.services.runtime_settings import set_user as _set_user

            _set_user(None)
        except Exception:  # noqa: BLE001
            pass


# ----------------------------------------------------------------------
# 视频质检师
# ----------------------------------------------------------------------

def assess_video(state: dict) -> dict:
    """视频质检师节点：检查视频有没有闪烁/变形/崩坏，筛出最佳片段。

    **视频还没好就跳过**（视频在后台跑，不阻塞图）—— 这不是失败，是时序。
    """
    phase = "video_assessor"
    payload = _session_payload(state)
    video = payload.get("video") or state.get("video") or {}

    if not video:
        logger.info("[video_assessor] 视频尚未生成，跳过质检")
        return {
            "video_report": {},
            "current_phase": phase,
            "video_assessor_note": ("视频还没生成（它在后台任务里跑），已跳过视频质检。"
                                    "生成完成后可单独再跑这一步。"),
        }
    err = str(video.get("error") or "")
    if err:
        return {"video_report": {}, "current_phase": phase,
                "video_assessor_note": f"视频生成失败，无法质检：{err}"}

    produced = int(video.get("produced") or 0)
    if not produced:
        return {"video_report": {}, "current_phase": phase,
                "video_assessor_note": "视频列表为空，没有可质检的片段。"}

    # ⚠️ 真做闪烁/变形检测需要**读视频帧**（ffmpeg + 逐帧比较）。
    # 现在只产出"有几段可检"的事实，**不编造分数** ——
    # 编一个假的"闪烁评分"比不检查更糟（用户会以为检过了）。
    report = {
        "clips": produced,
        "dir_or_path": str(video.get("dir_or_path") or ""),
        "checked": False,
        "reason": "逐帧闪烁/变形检测未接入（需要 ffmpeg 抽帧 + 帧间比较）",
    }
    logger.info("[video_assessor] 有 %d 段视频，但逐帧检测未接入", produced)
    return {"video_report": report, "current_phase": phase,
            "video_assessor_note": (f"有 {produced} 段视频。"
                                    f"**逐帧闪烁/变形检测尚未接入**，"
                                    f"所以这里只报数量、不给评分。")}


# ----------------------------------------------------------------------
# 修图师（占位）
# ----------------------------------------------------------------------

def inpaint(state: dict) -> dict:
    """修图师节点：**占位，直接跳过**（用户："先保留，后续开发，流程直接跳过"）。

    为什么不假装能修：`ImageService` 只有 `generate()`（文生图），
    **没有 edit / inpaint 方法**；`z-image-inpaint` 只是个声明，
    没有代码调用它。`fixed_images` 这个产物**从来没有被写入过**。

    所以这里如实说明"修复后端未接入"，而不是产出一份空的"修复结果"
    让用户以为修过了。
    """
    phase = "inpainter"
    images = state.get("images") or []
    logger.info("[inpainter] 跳过（修复后端未接入）")
    return {
        "fixed_images": [],
        "current_phase": phase,
        "inpainter_note": (f"修图环节已跳过：**修复后端未接入**"
                           f"（`ImageService` 没有 inpaint 方法）。"
                           f"当前 {len(images)} 张图按原样进入后续环节。"),
    }


__all__ = ["composite_motion", "assess_video", "inpaint", "publish"]


# ----------------------------------------------------------------------
# 发行总监
# ----------------------------------------------------------------------

def publish(state: dict) -> dict:
    """发行总监节点：生成发布物料 + 各平台状态。

    **没有成片就跳过**（发行不是必需的）—— 返回说明而不是报错。

    ## 两个坑（都是实测出来的，别改回去）

    **坑 1：字段名不一致。** `Distributor.run()` 读的是 **`state["video"]`**
    （见它自己的 docstring：`video: dict 最终视频（dir_or_path / produced，
    来自 payload.video）`），而图状态里那玩意儿叫 **`final_video`**。
    所以要把 `final_video` **补成 `video`** 再交给它。

    **坑 2：不能只看 `final_video` 就早退。** `final_video` 这个字段
    **全项目没有任何生产者**（只有 `CrewState` 的声明和这里的读取）——
    真正的视频在**会话 payload** 的 `video` 键里（异步任务写的）。
    原先这里先判 `final_video` 空就 return，于是**图里的发行环节永远走
    "跳过"分支**，会话里明明有视频也不发。
    修法：**先取 payload 里的视频**，两者都空才算没成片。
    """
    phase = "distributor"
    final = state.get("final_video") or {}
    payload_video = _session_payload(state).get("video") or {}

    if not final and not payload_video:
        logger.info("[distributor] 没有成片，跳过发行")
        return {
            "publish_results": {},
            "current_phase": phase,
            "distributor_note": ("还没有成片，已跳过发行。"
                                 "发行不是必需的 —— 生成视频（并剪辑）后再跑，"
                                 "或直接跳过。"),
        }

    run_state = dict(state)
    if not run_state.get("video"):
        run_state["video"] = (
            payload_video if payload_video else
            {"dir_or_path": final.get("dir_or_path", ""),
             "produced": final.get("segments", 0)})

    try:
        from app.agents.distributor import Distributor

        res = Distributor().run(run_state)
        if isinstance(res, dict) and "publish_results" in res:
            return {**res, "current_phase": phase}
        return {"publish_results": res if isinstance(res, dict) else {},
                "current_phase": phase}
    except Exception as exc:  # noqa: BLE001  发行失败不该让流程挂掉
        logger.warning("[distributor] 发行失败：%s", exc)
        return {
            "publish_results": {},
            "current_phase": phase,
            "distributor_note": f"发行失败：{type(exc).__name__}: {exc}",
        }
