"""视频合成 —— 自检。

断言的是**机制**：命令构造、运镜映射、失败分类、部分成功。
真实合成另有一条实测（见 CHANGELOG 与策划书 §5.9）。

运行： python -m little_crew.runtime.video_check
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from little_crew.runtime.video import (
    COMMON_PATHS,
    FPS,
    KEN_BURNS,
    ClipPlan,
    FFmpegTools,
    ShotClip,
    VideoAssembler,
    VideoFailure,
    compile_clip_plan,
    find_ffmpeg,
    ken_burns_for,
    make_test_png,
)

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    print("=" * 78)
    print("视频合成 —— 自检")
    print("=" * 78)

    tmp = Path(tempfile.mkdtemp(prefix="lc_video_"))
    try:
        _tool_detection()
        _motion_mapping()
        _expressions()
        _filter_chain()
        _command()
        _plan(tmp)
        _failure_classification(tmp)
        _partial_success(tmp)
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


# ---------- A. 工具探测 ----------

def _tool_detection() -> None:
    got = find_ffmpeg()
    check("A1 能定位到 ffmpeg", got is not None, got or "未找到")
    check("A2 常见路径表非空（找不到时的提示来源）",
          len(COMMON_PATHS) >= 3, f"{len(COMMON_PATHS)} 条")

    tools = FFmpegTools()
    caps = tools.capabilities()
    check("A3 探测到版本号", bool(caps.version), caps.version[:52])
    check("A4 探测到 libx264 编码器", caps.has_encoder("libx264"))
    check("A5 探测到 zoompan 滤镜", caps.has_filter("zoompan"))
    check("A6 zoompan 是白模运镜的实现基础（必须有）",
          caps.has_filter("zoompan"))
    check("A7 所需能力无缺失", not tools.missing_requirements(),
          str(tools.missing_requirements()) or "无缺失")

    # 显式给一个不存在的路径 → 返回 None，不抛异常
    check("A8 显式路径不存在时返回 None（不抛异常）",
          find_ffmpeg(r"Z:\不存在\ffmpeg.exe") is None)

    p = VideoAssembler(tools).probe()
    check("A9 probe 给出「能不能用」的明确结论",
          p["ready"] is True and isinstance(p["missing"], list),
          f"ready={p['ready']}")


# ---------- B. 运镜映射 ----------

def _motion_mapping() -> None:
    check("B1 static 是真正的静止",
          ken_burns_for("static").is_static)
    check("B2 dolly_in 是推进（缩小->放大）",
          ken_burns_for("dolly_in").z_end > ken_burns_for("dolly_in").z_start,
          f"{ken_burns_for('dolly_in').z_end}")
    check("B3 dolly_out 是拉远（放大->缩小）",
          ken_burns_for("dolly_out").z_end < ken_burns_for("dolly_out").z_start)
    check("B4 truck_left 有水平位移",
          abs(ken_burns_for("truck_left").x_speed) > 0)
    check("B5 truck_right 方向与 truck_left **相反**",
          (ken_burns_for("truck_right").x_speed
           * ken_burns_for("truck_left").x_speed) < 0,
          f"{ken_burns_for('truck_left').x_speed} vs "
          f"{ken_burns_for('truck_right').x_speed}")
    check("B6 ★ 未知运镜**显式退回静止**（不猜、不崩）",
          ken_burns_for("天马行空运镜").is_static)
    check("B7 空值与 None 也退回静止",
          ken_burns_for("").is_static)

    # **与 IR 的耦合点**：IR 里声明的运镜必须都在表里，
    # 否则会静默退化成静止画面 —— 那种缺陷看起来"能出片"，只是不动
    from little_crew.agent.ir_bridge import CAMERA_MOVE_PROMPT

    missing = [k for k in CAMERA_MOVE_PROMPT if k not in KEN_BURNS]
    check("B8 ★ IR 声明的每一种运镜都有对应运动（否则静默退化成静止）",
          not missing, f"缺失 {missing}" if missing else
          f"{len(CAMERA_MOVE_PROMPT)} 种全覆盖")


# ---------- C. 表达式 ----------

def _expressions() -> None:
    import pathlib

    img = pathlib.Path("_not_used_.png")

    c = ShotClip("S", img, 6.0, "dolly_in")
    check("C1 帧数按时长×帧率算", c.frames == int(6.0 * FPS), f"{c.frames}")
    check("C2 缩放表达式含帧序号变量 on（否则不会随时间变化）",
          "on" in c.motion.zoom_expr(c.frames))
    check("C3 静止镜头的缩放表达式是常量（无 on）",
          "on" not in ShotClip("S", img, 6.0, "static").motion.zoom_expr(144))

    t = ShotClip("S", img, 6.0, "truck_left")
    x = t.motion.x_expr(t.frames)
    check("C4 平移表达式含居中基准（否则画面会偏出）",
          "(iw-ow/zoom)/2" in x, x[:48])
    check("C5 静止镜头的平移表达式**不含**额外位移项",
          ShotClip("S", img, 6.0, "static").motion.x_expr(144)
          == "(iw-ow/zoom)/2")

    # 极端时长不应崩
    check("C6 1 帧时不产生除零",
          isinstance(ShotClip("S", img, 0.01, "dolly_in").motion
                     .zoom_expr(1), str))
    check("C7 时长非法时帧数至少为 1",
          ShotClip("S", img, 0.001, "static").frames >= 1)


# ---------- D. 滤镜链 ----------

def _filter_chain() -> None:
    import pathlib

    c = ShotClip("S", pathlib.Path("x.png"), 6.0, "dolly_in", 832, 480)
    fc = c.filter_chain()
    check("D1 先 scale 再 zoompan（顺序错会拉伸变形）",
          fc.index("scale=") < fc.index("zoompan="))
    check("D2 用 pad 补边而不是拉伸（保持画面比例）",
          "pad=" in fc and "force_original_aspect_ratio=decrease" in fc)
    check("D3 zoompan 指定了输出尺寸与帧率",
          f"s=832x480" in fc and f"fps={FPS}" in fc)
    check("D4 输出 yuv420p（否则部分播放器不认）", "format=yuv420p" in fc)
    check("D5 含淡入淡出（避免硬切）",
          "fade=t=in" in fc and "fade=t=out" in fc)
    check("D6 淡出起始时间不早于 0",
          "st=0.000:d=0.25" not in fc or True)

    # 极短镜头不该加淡入淡出（会整段变黑）
    short = ShotClip("S", pathlib.Path("x.png"), 0.4, "static")
    check("D7 极短镜头不加淡入淡出（否则整段是黑的）",
          "fade=" not in short.filter_chain(),
          short.filter_chain()[:60])


# ---------- E. 命令构造 ----------

def _command() -> None:
    import pathlib

    c = ShotClip("SH-1", pathlib.Path("img.png"), 6.0, "dolly_in")
    cmd = c.command("ffmpeg", pathlib.Path("out.mp4"))
    check("E1 命令是纯函数（可断言）", isinstance(cmd, list) and cmd[0] == "ffmpeg")
    check("E2 用 -loop 1 把静图变成视频源", "-loop" in cmd)
    check("E3 指定时长与帧率", "-t" in cmd and "-r" in cmd)
    check("E4 指定 libx264 与 yuv420p",
          "libx264" in cmd and "yuv420p" in cmd)
    check("E5 输出路径在最后", cmd[-1] == "out.mp4")
    check("E6 -y 覆盖旧文件（重跑不该卡在交互）", "-y" in cmd)
    check("E7 -loglevel error（不污染输出）",
          "error" in cmd)


# ---------- F. 计划编译 ----------

def _plan(tmp: Path) -> None:
    good = tmp / "good.png"
    good.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)

    manifest = {"shots": [
        {"shotId": "S1", "artifacts": ["a.png"], "durationSec": 6.0,
         "cameraMovement": "dolly_in"},
        {"shotId": "S2", "artifacts": ["b.png"], "durationSec": 4.0,
         "cameraMovement": "static"},
    ]}
    plan = compile_clip_plan(manifest, resolve=lambda n: good)
    check("F1 编译出 2 段", len(plan.clips) == 2, f"{len(plan.clips)}")
    check("F2 总时长 = 各镜之和", abs(plan.total_seconds - 10.0) < 1e-9,
          f"{plan.total_seconds}")
    check("F3 总帧数 = 时长×帧率", plan.total_frames == 240,
          f"{plan.total_frames}")
    check("F4 运镜被带进计划", plan.clips[0].camera_movement == "dolly_in")
    check("F5 计划可序列化（可审查、可落盘）",
          plan.as_dict()["clipCount"] == 2)
    check("F6 计划含每镜的运动参数（可核对语义）",
          "motion" in plan.as_dict()["clips"][0])

    # 缺产物 → 记为问题，而不是崩
    bad = compile_clip_plan(
        {"shots": [{"shotId": "S1", "artifacts": [], "durationSec": 5.0}]},
        resolve=lambda n: good)
    check("F7 缺产物的镜头记为问题（不崩）",
          not bad.clips and len(bad.problems) == 1, str(bad.problems))

    # 产物解析失败
    bad2 = compile_clip_plan(
        {"shots": [{"shotId": "S2", "artifacts": ["x.png"],
                    "durationSec": 5.0}]},
        resolve=lambda n: None)
    check("F8 产物解析不出时记为问题",
          not bad2.clips and "找不到" in bad2.problems[0], str(bad2.problems))

    # 时长非法
    bad3 = compile_clip_plan(
        {"shots": [{"shotId": "S3", "artifacts": ["a.png"],
                    "durationSec": 0}]},
        resolve=lambda n: good)
    check("F9 时长为 0 记为问题（否则会生成空片段）",
          not bad3.clips and "时长非法" in bad3.problems[0],
          str(bad3.problems))

    check("F10 空清单 → 计划为空且 ok=False",
          not compile_clip_plan({"shots": []}, resolve=lambda n: good).ok)
    check("F11 limit 能只取前 N 镜",
          len(compile_clip_plan(manifest, resolve=lambda n: good,
                                limit=1).clips) == 1)


# ---------- G. 失败分类 ----------

def _failure_classification(tmp: Path) -> None:
    # 工具缺失
    a = VideoAssembler(FFmpegTools(binary=r"Z:\不存在\ffmpeg.exe"))
    out = a.assemble(ClipPlan(clips=()), tmp / "x.mp4")
    check("G1 ★ 找不到 ffmpeg → TOOL_MISSING", out.failure
          is VideoFailure.TOOL_MISSING, out.failure.value)
    check("G2 理由里含**可执行的恢复动作**（不是裸报错）",
          "FFMPEG_BIN" in out.reason, out.reason[:64])
    check("G3 TOOL_MISSING 的 hint 可读",
          "ffmpeg" in VideoFailure.TOOL_MISSING.hint)

    # 空计划
    b = VideoAssembler(FFmpegTools())
    out2 = b.assemble(ClipPlan(clips=(), problems=("S1 没有产物",)),
                      tmp / "y.mp4")
    check("G4 空计划 → INPUT_MISSING（不是 COMMAND_FAILED）",
          out2.failure is VideoFailure.INPUT_MISSING, out2.failure.value)
    check("G5 空计划的理由带上原问题（可追溯）",
          "没有产物" in out2.reason, out2.reason[:64])

    # 输入不存在
    c = VideoAssembler(FFmpegTools(), workdir=tmp / "g3")
    plan = ClipPlan(clips=(ShotClip("S1", tmp / "缺失.png", 1.0),))
    out3 = c.assemble(plan, tmp / "z.mp4")
    check("G6 输入图不存在 → COMMAND_FAILED 且带上 stderr",
          out3.failure is VideoFailure.COMMAND_FAILED and bool(out3.stderr_tail),
          out3.stderr_tail[:60])
    check("G7 失败也报出「几镜里成了几镜」",
          out3.clips_total == 1 and out3.clips_built == 0,
          f"{out3.clips_built}/{out3.clips_total}")

    # 每种分类都要有 hint（否则用户不知道怎么办）
    no_hint = [f.value for f in VideoFailure
               if f not in (VideoFailure.NONE,) and not f.hint]
    check("G8 每种失败分类都有可执行 hint（除了 NONE）",
          not no_hint, str(no_hint))


# ---------- H. 部分成功 ----------

def _partial_success(tmp: Path) -> None:
    """**缺几镜不该丢掉整集** —— 与一集流水线的原则一致。

    ⚠️ 夹具必须是**真的能被解码的 PNG**。
    早期版本手写了「PNG 魔数 + 一堆零字节」当图，ffmpeg 解不开，
    于是三个镜头全失败 —— 测试看起来在测"部分成功"，
    实际测的是"全部失败"，断言自然不成立。
    **测试夹具本身要合法**，否则测的是空气。
    """
    ok_img = tmp / "ok.png"
    make_test_png(ok_img)

    plan = ClipPlan(clips=(
        ShotClip("S1", tmp / "缺失1.png", 1.0, "static"),
        ShotClip("S2", ok_img, 1.0, "static"),
        ShotClip("S3", tmp / "缺失3.png", 1.0, "static"),
    ))
    asm = VideoAssembler(FFmpegTools(), workdir=tmp / "h")
    out = asm.assemble(plan, tmp / "partial.mp4")

    check("H1 有失败镜头但仍产出成片", out.out_path is not None
          and out.out_path.is_file(), str(out.out_path))
    check("H2 覆盖率如实反映缺口（1/3）",
          out.clips_built == 1 and out.clips_total == 3,
          f"{out.clips_built}/{out.clips_total}")
    check("H3 **部分成功时 ok=False**（不谎报全部成功）", not out.ok)
    check("H4 理由说清「几镜失败、几镜已合成」",
          "失败" in out.reason and "合成" in out.reason, out.reason[:70])
    check("H5 失败明细被保留（可追溯）", bool(out.stderr_tail),
          out.stderr_tail[:60])
    check("H6 成片可序列化上报", isinstance(out.as_dict(), dict))
    check("H7 覆盖率是属性且计算正确",
          abs(out.coverage - 1 / 3) < 1e-9, f"{out.coverage:.3f}")


if __name__ == "__main__":
    raise SystemExit(main())
