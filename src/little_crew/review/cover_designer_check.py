"""封面设计师自检（2026-11-13）：`review/cover_designer.py`。

锁住六件事：
1 **粗筛**：缺失/过小/非图片被剔除，且按上限**等距抽样**（覆盖整集）
2 **选帧模式**：拼图 -> 一次判断 -> 复制**原分辨率**那张（不是拼图裁片）
3 **"不选"是合法结果**：模型回 pick=0 -> `ok=False`（宁缺毋滥），不随便挑
4 **越界/非法 JSON** -> 不出封面（不静默）
5 **生成模式**：用参考图做图生图；无参考图/无 key -> 明确失败
6 **统一入口** `build_cover(mode=...)` 正确分流
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


FFMPEG = r"D:\AI\FFmpeg\bin\ffmpeg.exe"


def _mk_image(path: Path, color: str = "gray") -> bool:
    """用 ffmpeg 造一张测试图（项目本身依赖 ffmpeg）。"""
    try:
        subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                        # 用 testsrc2（彩色测试图案）—— 纯色图只有 1KB，会被 prefilter 的
                        # min_bytes=8KB 门限剔掉，导致测试自己失败
                        "-f", "lavfi", "-i", f"testsrc2=s=640x360:d=1,format=rgb24",
                        "-frames:v", "1", str(path)], check=True,
                       capture_output=True, timeout=30)
        return path.is_file()
    except Exception:                                         # noqa: BLE001
        return False


class _Client:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def chat_image(self, *a, **kw):
        self.calls += 1
        return self.reply if isinstance(self.reply, str) else json.dumps(self.reply)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("封面设计师（cover_designer）—— 自检")
    print("=" * 66)

    from little_crew.review import cover_designer as CD

    print(); print("A. 粗筛与抽样")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        ok = []
        for i in range(6):
            p = d / f"s{i}.png"
            if _mk_image(p):
                ok.append((f"S{i}", p))
        imgs = {sid: str(p) for sid, p in ok}
        imgs["S_missing"] = str(d / "nope.png")
        tiny = d / "tiny.png"
        tiny.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
        imgs["S_tiny"] = str(tiny)
        valid, dropped = CD.prefilter(imgs, limit=3)
        check("A1 ★★★ 缺失/过小被剔除",
              {x["shotId"] for x in dropped} >= {"S_missing", "S_tiny"},
              str([x["shotId"] for x in dropped]))
        check("A2 ★★★ 上限生效（limit=3）", len(valid) == 3, str(len(valid)))
        check("A3 ★★★ 等距抽样覆盖首尾（不是只取前 3 个）",
              valid[0][0] == ok[0][0] and valid[-1][0] != ok[2][0], str([s for s, _ in valid]))

        print(); print("B. 拼图与选帧")
        sheet = CD.build_contact_sheet(valid, d / "sheet.jpg")
        check("B1 ★★★ 拼图生成成功且带列/行数",
              sheet.get("ok") and sheet.get("cols") == 2 and sheet.get("count") == 3,
              str(sheet)[:80])
        rep = CD.select_cover({s: str(p) for s, p in ok}, d / "out",
                              client=_Client({"pick": 2, "reason": "主体完整", "bad": []}))
        check("B2 ★★★ 选帧成功并记录 shotId",
              rep.get("ok") and rep.get("shotId") == ok[1][0], str(rep)[:90])
        cover = Path(str(rep.get("cover") or ""))
        check("B3 ★★★ 封面是**原分辨率**那张（不是拼图裁片）",
              cover.is_file() and cover.read_bytes() == ok[1][1].read_bytes(), "")
        check("B4 ★★★ 报告里带 judge 理由与候选清单",
              "主体完整" in str(rep.get("reason")) and len(rep.get("candidates") or []) == 6,
              str(rep.get("candidates"))[:60])

        print(); print("C. 失败语义（宁缺毋滥 / 不静默）")
        rep0 = CD.select_cover({s: str(p) for s, p in ok}, d / "o0",
                               client=_Client({"pick": 0, "reason": "都有硬伤"}))
        check("C1 ★★★ 模型「不选」-> ok=False（不随便挑一张）",
              rep0.get("ok") is False and "不选" in str(rep0.get("error")), str(rep0.get("error")))
        repx = CD.select_cover({s: str(p) for s, p in ok}, d / "o1",
                               client=_Client({"pick": 99}))
        check("C2 ★★★ 越界索引 -> ok=False", repx.get("ok") is False, str(repx.get("error")))
        repb = CD.select_cover({s: str(p) for s, p in ok}, d / "o2",
                               client=_Client("我觉得都不错"))
        check("C3 ★★★ 不给 JSON -> ok=False（不能当已选）",
              repb.get("ok") is False and "JSON" in str(repb.get("error")), str(repb.get("error")))

        print(); print("D. 生成模式")
        empty = CD.generate_cover([], d / "g")
        check("D1 ★★★ 没有参考图 -> 明确失败", empty.get("ok") is False
              and "参考图" in str(empty.get("error")), str(empty.get("error")))

        # 注入假 multipart 响应（避免网络）
        from little_crew import net as _net

        png1x1 = (b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        import base64 as _b64
        fake = json.dumps({"data": [{"b64_json": _b64.b64encode(png1x1).decode()}]}).encode()
        orig = _net.post_multipart
        calls = {"n": 0}

        def fake_post(url, fields, files, **kw):
            calls["n"] += 1
            assert url.endswith("/images/edits")
            assert "prompt" in fields and "image" in files
            return fake

        _net.post_multipart = fake_post
        try:
            rep_g = CD.generate_cover([ok[0][1]], d / "g2", character="测试角色")
        finally:
            _net.post_multipart = orig
        check("D2 ★★★ 生成模式落盘 cover_generated.png",
              rep_g.get("ok") and Path(str(rep_g.get("out"))).name == "cover_generated.png",
              str(rep_g.get("error")))
        check("D3 ★★★ 走的是 /images/edits 且带 prompt+image",
              calls["n"] == 1 and rep_g.get("bytes") == len(png1x1), str(calls))

        print(); print("E. 统一入口")
        rep_s = CD.build_cover([], {s: str(p) for s, p in ok}, d / "b1", mode="select",
                               client=_Client({"pick": 1}))
        check("E1 ★★ mode=select 走选帧", rep_s.get("mode") == "select", str(rep_s.get("mode")))
        rep_g2 = CD.build_cover([ok[0][1]], {}, d / "b2", mode="generate")
        check("E2 ★★ mode=generate 走生成", rep_g2.get("mode") == "generate",
              str(rep_g2.get("mode")))

    print(); print("F. 标题（后合成叠字 / 模型画 / 不加）")
    with tempfile.TemporaryDirectory() as td2:
        d2 = Path(td2)
        base = d2 / "base.png"
        _mk_image(base)
        titled = CD.add_title(base, "小剧组 · 第一集")
        check("F1 ★★★ 中文字体可发现（跨平台探测）", bool(CD.find_font()), CD.find_font())
        check("F2 ★★★ 叠标题成功且字号按画面自适应",
              titled.get("ok") and int(titled.get("size") or 0) > 0,
              str(titled.get("error")))
        check("F3 ★★★ 叠字后文件确实变了（不是空操作）",
              Path(str(titled.get("out") or "")).is_file()
              and Path(str(titled["out"])).read_bytes() != base.read_bytes(), "")
        long_title = CD.add_title(base, "第一集：那年夏天的最后一个黄昏", out_path=d2 / "t2.png")
        check("F4 ★★ 长标题自动缩小字号",
              long_title.get("ok") and long_title["size"] < titled["size"],
              f"{long_title.get('size')} vs {titled.get('size')}")
        r_none = CD.build_cover([], {"S0": str(base)}, d2 / "n", mode="select",
                                client=_Client({"pick": 1}), title="第一集",
                                title_mode="none")
        check("F5 ★★★ title_mode=none 不叠字",
              r_none.get("titleMode") == "none" and not r_none.get("title"), "")
        r_ov = CD.build_cover([], {"S0": str(base)}, d2 / "o", mode="select",
                              client=_Client({"pick": 1}), title="第一集",
                              title_mode="overlay")
        check("F6 ★★★ title_mode=overlay 自动叠字并记录",
              isinstance(r_ov.get("title"), dict) and r_ov["title"].get("ok"),
              str(r_ov.get("title"))[:80])

    n = sum(1 for _, ok_, _ in _r if ok_)
    print()
    for name, ok_, detail in _r:
        print(f" [{'v' if ok_ else 'X'}] {name}")
        if detail and not ok_:
            print(f"       {detail}")
    print("=" * 66)
    print(f"结果：{n}/{len(_r)} 通过")
    return 0 if n == len(_r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
