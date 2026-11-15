"""口型视频通道自检（2026-11-15）。

锁住七件事：
1 **不联网**：全部用注入的假客户端（真实实现只在真跑时用）
2 调用顺序/模型绑定：检测图用 `wan2.2-s2v-detect` 上传，生成素材用 `wan2.2-s2v` 上传
3 **失败语义**：检测不过 / 任务失败 / 缺文件 / 超时长 / 分辨率非法 → `ok=False` 且带 error
4 费用：按官方单价（480P 0.5、720P 0.9 元/秒）计算
5 产物与报告：成功时落 mp4；usage/秒数/费用进返回
6 `do_detect=False` 时**不调检测**
7 视频层：`ShotClip.video` 覆盖时命令走"片段输入 + 统一画幅 + 丢音轨"
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import wave

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


class FakeClient:
    """记录调用顺序；可配置失败点。"""

    def __init__(self, *, detect_ok=True, task_ok=True, create_ok=True,
                 video_ok=True, fail_upload=False):
        self.calls: list[tuple] = []
        self.detect_ok = detect_ok
        self.task_ok = task_ok
        self.create_ok = create_ok
        self.video_ok = video_ok
        self.fail_upload = fail_upload

    def upload_file(self, model, path):
        self.calls.append(("upload", model, pathlib.Path(str(path)).name))
        if self.fail_upload:
            raise RuntimeError("模拟上传失败")
        return f"oss://fake/{model}/{pathlib.Path(str(path)).name}"

    def detect(self, url):
        self.calls.append(("detect", url))
        return {"_http": 200,
                "output": {"check_pass": self.detect_ok, "humanoid": True}}

    def create_task(self, img, aud, *, resolution="480P", style=""):
        self.calls.append(("create", resolution, style, img, aud))
        return {"_http": 200,
                "output": {"task_id": "T1" if self.create_ok else ""}}

    def poll(self, tid, *, timeout_s=1, interval_s=0, on_progress=None):
        self.calls.append(("poll", tid))
        st = "SUCCEEDED" if self.task_ok else "FAILED"
        out = {"task_status": st}
        if self.task_ok:
            out["results"] = {"video_url": "https://fake/v.mp4"}
        return {"output": out,
                "usage": {"duration": 4.75, "size": "512*640", "fps": 16}}

    def download(self, url, dst):
        self.calls.append(("download", url))
        p = pathlib.Path(str(dst))
        p.parent.mkdir(parents=True, exist_ok=True)
        if not self.video_ok:
            return {"ok": False}
        p.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 64)
        return {"ok": True, "out": str(p), "bytes": p.stat().st_size}


def _png(p: pathlib.Path) -> None:
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)


def _wav(p: pathlib.Path, seconds: float = 2.0, rate: int = 16000) -> None:
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("口型视频通道（lipsync）—— 自检")
    print("=" * 66)

    from little_crew.runtime import lipsync as LS
    from little_crew.runtime.video import ShotClip

    print(); print("A. 可用性与计价")
    orig_key = LS._key
    try:
        LS._key = lambda: ""
        ok_av, why = LS.available()
    finally:
        LS._key = orig_key
    check("A1 ★★★ 无 key -> 不可用且说明原因", ok_av is False and "DASHSCOPE" in why, why)
    check("A2 ★★★ 480P 计价：4.75s -> ¥2.375",
          abs(LS.estimate_cost_cny(4.75, "480P") - 2.375) < 1e-6,
          str(LS.estimate_cost_cny(4.75, "480P")))
    check("A3 ★★★ 720P 计价：4.75s -> ¥4.275",
          abs(LS.estimate_cost_cny(4.75, "720P") - 4.275) < 1e-6, "")
    check("A4 ★★ 非法分辨率 -> 0 元（不假装成功）",
          LS.estimate_cost_cny(10, "1080P") == 0.0, "")

    print(); print("B. 成功路径（假客户端，不联网）")
    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)
        img, aud = d / "face.png", d / "line.wav"
        _png(img); _wav(aud, 4.0)
        fake = FakeClient()
        rep = LS.synth(img, aud, out_path=d / "out.mp4", client=fake,
                       poll_s=0.0, timeout_s=1)
        models = [c[1] for c in fake.calls if c[0] == "upload"]
        check("B1 ★★★ 成功并落 mp4", rep.get("ok") and (d / "out.mp4").is_file(),
              str(rep.get("error")))
        check("B2 ★★★ 模型绑定正确：先 detect 模型传图，再 gen 模型传图/音",
              models == [LS.DETECT_MODEL, LS.GEN_MODEL, LS.GEN_MODEL], str(models))
        check("B3 ★★★ 顺序正确：检测 -> 建任务 -> 轮询 -> 下载",
              [c[0] for c in fake.calls] == ["upload", "detect", "upload",
                                             "upload", "create", "poll", "download"],
              str([c[0] for c in fake.calls]))
        check("B4 ★★★ 费用按 usage 时长计（4.75s × 0.5 = ¥2.38）",
              abs(float(rep.get("costCny") or 0) - 2.375) < 1e-6,
              str(rep.get("costCny")))
        check("B5 ★★ 返回 usage/秒数（可进报告）",
              rep.get("usage", {}).get("fps") == 16
              and float(rep.get("audioSeconds") or 0) == 4.0, str(rep.get("usage")))

        # do_detect=False：不调检测
        fake2 = FakeClient()
        rep2 = LS.synth(img, aud, out_path=d / "o2.mp4", client=fake2,
                        do_detect=False, poll_s=0.0, timeout_s=1)
        check("B6 ★★★ do_detect=False 时确实不调检测",
              rep2.get("ok") and not any(c[0] == "detect" for c in fake2.calls),
              str([c[0] for c in fake2.calls]))

    print(); print("C. 失败语义（不静默、不假装成功）")
    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)
        img, aud = d / "face.png", d / "line.wav"
        _png(img); _wav(aud, 2.0)
        r_det = LS.synth(img, aud, out_path=d / "a.mp4",
                         client=FakeClient(detect_ok=False), poll_s=0.0, timeout_s=1)
        check("C1 ★★★ 检测不过 -> ok=False", r_det.get("ok") is False
              and "检测" in str(r_det.get("error")), str(r_det.get("error")))
        r_task = LS.synth(img, aud, out_path=d / "b.mp4",
                          client=FakeClient(task_ok=False), poll_s=0.0, timeout_s=1)
        check("C2 ★★★ 任务失败 -> ok=False 且报状态",
              r_task.get("ok") is False and "FAILED" in str(r_task.get("error")),
              str(r_task.get("error")))
        r_dl = LS.synth(img, aud, out_path=d / "c.mp4",
                        client=FakeClient(video_ok=False), poll_s=0.0, timeout_s=1)
        check("C3 ★★★ 下载失败 -> ok=False", r_dl.get("ok") is False, "")
        r_up = LS.synth(img, aud, out_path=d / "d.mp4",
                        client=FakeClient(fail_upload=True), poll_s=0.0, timeout_s=1)
        check("C4 ★★★ 上传异常 -> ok=False 且带异常类型",
              r_up.get("ok") is False and "RuntimeError" in str(r_up.get("error")),
              str(r_up.get("error")))
        r_miss = LS.synth(d / "nope.png", aud, out_path=d / "e.mp4",
                          client=FakeClient(), poll_s=0.0, timeout_s=1)
        check("C5 ★★★ 图片不存在 -> ok=False", r_miss.get("ok") is False, "")
        long_wav = d / "long.wav"
        _wav(long_wav, 21.0, rate=8000)
        r_long = LS.synth(img, long_wav, out_path=d / "f.mp4",
                          client=FakeClient(), poll_s=0.0, timeout_s=1)
        check("C6 ★★★ 音频 ≥20s -> 明确拒绝（官方上限）",
              r_long.get("ok") is False and "20" in str(r_long.get("error")),
              str(r_long.get("error")))
        r_res = LS.synth(img, aud, resolution="1080P", out_path=d / "g.mp4",
                         client=FakeClient(), poll_s=0.0, timeout_s=1)
        check("C7 ★★★ 非法分辨率 -> ok=False", r_res.get("ok") is False, "")

    print(); print("D. 视频层：片段覆盖（ShotClip.video）")
    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)
        img = d / "x.png"; _png(img)
        clip = ShotClip(shot_id="S1", image=img, duration_sec=3.0,
                        width=832, height=480, video=d / "talk.mp4")
        cmd = clip.command("ffmpeg", d / "o.mp4")
        check("D1 ★★★ 有 video -> 用片段输入（无 -loop）",
              "-i" in cmd and str(d / "talk.mp4") in cmd and "-loop" not in cmd,
              " ".join(str(c) for c in cmd[:8]))
        check("D2 ★★★ 丢片段音轨（成片音轨统一混，避免双音轨）", "-an" in cmd, "")
        check("D3 ★★ 归一化到目标帧率/画幅",
              "fps=24" in clip.video_filter_chain()
              and "scale=832:480" in clip.video_filter_chain(),
              clip.video_filter_chain())
        static = ShotClip(shot_id="S2", image=img, duration_sec=3.0)
        cmd2 = static.command("ffmpeg", d / "o2.mp4")
        check("D4 ★★★ 无 video -> 保持原 Ken Burns 路径（-loop 1）",
              cmd2[:6][-2:] == ["-loop", "1"] or "-loop" in cmd2, " ".join(map(str, cmd2[:7])))

    n = sum(1 for _, ok, _ in _r if ok)
    print()
    for name, ok, detail in _r:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 66)
    print(f"结果：{n}/{len(_r)} 通过")
    return 0 if n == len(_r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
