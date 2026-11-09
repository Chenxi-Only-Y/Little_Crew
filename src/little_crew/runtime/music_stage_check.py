"""音乐床决策自检：三层优先级 + **失败不抛**（不用跑整集）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from little_crew.runtime import music_stage as MS

_r = []
captured: dict = {}


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


class _Shot:
    def __init__(self, secs):
        self.duration_sec = secs


class _BB:
    def __init__(self, value):
        self._v = value

    def value(self, key):
        return self._v


class _Orch:
    def __init__(self, plan=None, bound=True, ok=True, err="", raises=False):
        self._impls = {"composer": object()} if bound else {}
        self.blackboard = _BB(plan)
        self._ok, self._err, self._raises = ok, err, raises
        self.calls = 0

    def run_stage(self, rid, cap, payload):
        self.calls += 1
        captured.clear()
        captured.update(payload or {})
        if self._raises:
            raise RuntimeError("boom")
        class _O:
            ok = self._ok
            error = self._err
        return _O()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("音乐床决策（composer 优先 / 内联兜底）—— 自检")
    print("=" * 66)
    ff = ""
    for c in (r"D:\AI\FFmpeg\bin\ffmpeg.exe", "ffmpeg"):
        if c == "ffmpeg" or Path(c).is_file():
            ff = c
            break
    if not ff:
        print("  跳过：找不到 ffmpeg")
        return 0

    shots = [_Shot(3.0), _Shot(5.0)]
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)

        print(); print("A. ① composer 成功 -> 用它")
        fake = d / "bed.mp3"
        fake.write_bytes(b"x" * 100)
        o = _Orch(plan={"path": str(fake), "source": "asset:bed.mp3",
                        "seconds": 9.0})
        r = MS.resolve_bed(orch=o, shots=shots, out_dir=d, ffmpeg=ff)
        check("A1 ★★★ via=composer", r["via"] == "composer", r["via"])
        check("A2 ★★★ source 如实来自职位", r["source"] == "asset:bed.mp3", r["source"])
        check("A3 ★★ 调了职位一次", o.calls == 1, str(o.calls))
        check("A4 ★★ 请求时长=镜头总时长", r["requestedSeconds"] == 8.0,
              str(r["requestedSeconds"]))
        # ffmpeg 必须透传给职位：Agent 默认 "ffmpeg" 不在 PATH 时，
        # 不透传就会"agent 失败 → 退回内联"，外部看着像"没接上"（今天真踩）
        check("A5 ★★★ ffmpeg 透传给职位", captured.get("ffmpeg") == ff,
              str(captured.get("ffmpeg")))

        print(); print("B. ② 职位失败 -> 退回内联（且说明原因）")
        o2 = _Orch(ok=False, err="GPU 忙")
        r2 = MS.resolve_bed(orch=o2, shots=shots, out_dir=d, ffmpeg=ff)
        check("B1 ★★★ via=inline（兜底生效）", r2["via"] == "inline", r2["via"])
        check("B2 ★★★ note 里写明职位为什么没成", "GPU 忙" in r2["note"], r2["note"])
        check("B3 ★★★ 内联能给出文件", bool(r2["path"]) and Path(r2["path"]).is_file(),
              r2["path"])

        print(); print("C. ③ 未绑定 -> 直接用内联")
        o3 = _Orch(bound=False)
        r3 = MS.resolve_bed(orch=o3, shots=shots, out_dir=d, ffmpeg=ff)
        check("C1 ★★★ via=inline", r3["via"] == "inline", r3["via"])
        check("C2 ★★ 没绑定就不该调它", o3.calls == 0, str(o3.calls))

        print(); print("D. 失败不抛（音乐床坏了不该拖垮整集）")
        o4 = _Orch(raises=True)
        r4 = MS.resolve_bed(orch=o4, shots=shots, out_dir=d, ffmpeg=ff)
        check("D1 ★★★ 抛异常也不向外抛", isinstance(r4, dict) and "via" in r4, str(r4)[:60])
        check("D2 ★★ 异常写进 note", "boom" in r4["note"], r4["note"])
        check("D3 ★★★ 总时长是统一算法", MS.film_seconds(shots) == 8.0,
              str(MS.film_seconds(shots)))
        check("D4 ★★ 空镜头表 -> 0 秒不崩", MS.film_seconds([]) == 0.0, "")

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
