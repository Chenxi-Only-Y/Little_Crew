"""交付元数据自检（2026-11-15）：AI 标识 / 署名 / 素材登记。

锁住五件事：
1 **素材登记**：模型许可来自授权目录；未知来源记 UNKNOWN（整体不通过）
2 **显式标识**：片尾卡 + 封面角标**真的生成文件**（不是只置布尔）
3 **隐式标识**：mp4 容器元数据**真的写进去**（ffprobe 可读回）
4 **诚实**：没有 mp4 / 注入失败 -> `implicit=False` 并记 errors
5 **合规对接**：`build_delivery_meta` 的产物喂给合规官后，交付腿能通过
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def _tiny_mp4(path: Path, ff: str) -> bool:
    try:
        p = subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error",
                            "-f", "lavfi", "-i", "color=c=black:s=64x48:d=0.4",
                            "-pix_fmt", "yuv420p", str(path)],
                           capture_output=True, text=True, timeout=60)
        return p.returncode == 0 and path.is_file() and path.stat().st_size > 0
    except Exception:                                         # noqa: BLE001
        return False


def _cover_jpg(path: Path) -> bool:
    try:
        from PIL import Image

        Image.new("RGB", (160, 100), (60, 40, 90)).save(str(path), "JPEG")
        return path.is_file()
    except Exception:                                         # noqa: BLE001
        return False


def _ffprobe(ff: str) -> str:
    p = Path(ff)
    cand = p.with_name("ffprobe" + p.suffix) if p.suffix else Path("ffprobe")
    if cand.is_file():
        return str(cand)
    return "ffprobe"


def _comment_of(mp4: Path, ff: str) -> str:
    try:
        p = subprocess.run([_ffprobe(ff), "-v", "quiet", "-show_entries",
                            "format_tags=comment", "-of", "json", str(mp4)],
                           capture_output=True, text=True, timeout=60,
                           encoding="utf-8", errors="replace")
        if p.returncode != 0:
            return ""
        obj = json.loads(p.stdout or "{}")
        tags = (obj.get("format") or {}).get("tags") or {}
        return str(tags.get("comment", "") or "")
    except Exception:                                         # noqa: BLE001
        return ""


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 68)
    print("交付元数据（delivery_meta）—— 自检")
    print("=" * 68)

    from little_crew.runtime import delivery_meta as dm

    ff = dm._ffmpeg()
    has_ff = bool(shutil.which(ff)) or Path(str(ff)).is_file()

    print(); print("A. 素材登记")
    src = dm.collect_sources(models=["z-image-turbo", "ghost-model"],
                             anchors={"CHAR-0007": "assets/x.png"},
                             fonts=["C:/Windows/Fonts/simhei.ttf"],
                             audio=["delivery/mix.wav"],
                             covers=["delivery/cover.jpg"])
    kinds = {s.get("kind") for s in src}
    check("A1 ★★★ 模型/参考图/字体/音轨/封面都登记",
          {"model", "reference", "font", "audio", "cover"} <= kinds, str(sorted(kinds)))
    z = next(s for s in src if s.get("name") == "z-image-turbo")
    check("A2 ★★★ 模型许可来自授权目录（非 UNKNOWN）",
          z.get("license") not in ("", "UNKNOWN"), str(z.get("license")))
    g = next(s for s in src if s.get("name") == "ghost-model")
    check("A3 ★★★ 未知模型 -> license=UNKNOWN（不编造）",
          g.get("license") == "UNKNOWN", "")

    print(); print("B. 显式标识（真的写文件）")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        endcard = d / "endcard.png"
        ok_card = dm.write_endcard(endcard, credits="本作品由 MiniMax H3 生成")
        check("B1 ★★★ 片尾卡生成成功且非空", ok_card and endcard.is_file()
              and endcard.stat().st_size > 0, str(endcard.stat().st_size if endcard.is_file() else 0))
        cover = d / "cover.jpg"
        _cover_jpg(cover)
        labeled = d / "cover_labeled.jpg"
        ok_lab = dm.label_cover(cover, labeled)
        check("B2 ★★★ 封面角标生成副本（原封面不动）",
              ok_lab and labeled.is_file() and cover.is_file(), "")

    print(); print("C. 隐式标识（mp4 容器元数据）")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        mp4 = d / "master.mp4"
        made = _tiny_mp4(mp4, ff) if has_ff else False
        if made:
            ok_inj = dm.inject_mp4_metadata(mp4, comment="AI-generated; test")
            check("C1 ★★★ 注入成功", ok_inj, "")
            cmt = _comment_of(mp4, ff)
            check("C2 ★★★ ffprobe 能读回 comment（真的写进容器）",
                  "AI-generated" in cmt, cmt[:80])
            meta = dm.build_delivery_meta(
                workdir=d, episode_id="E1", models=["minimax-h3"],
                attributions=["本作品由 MiniMax H3 生成"],
                anchors={"少女": str(d / "a.png")}, masters=[str(mp4)],
                covers=[str(d / "cover.jpg")])
            lab = meta.get("aiLabels") or {}
            check("C3 ★★★ 有 master -> implicit=True（实际注入过）",
                  bool(lab.get("implicit")), json.dumps(lab, ensure_ascii=False))
            check("C4 ★★★ 交付物落盘：ai_labels / credits / sources",
                  (d / "delivery" / "ai_labels.json").is_file()
                  and (d / "delivery" / "credits.txt").is_file()
                  and (d / "delivery" / "sources.json").is_file(), "")
            check("C5 ★★★ 署名写进 credits（授权要求文本）",
                  "MiniMax H3" in str(meta.get("credits")), str(meta.get("credits")))
            from little_crew.agent.compliance_audit import audit as _audit

            rep = _audit(models_used=["minimax-h3"],
                         delivery=dict(meta), commercial=True)
            check("C6 ★★★ 喂给合规官后交付腿通过（标识/署名/登记齐）",
                  rep["deliveryAudit"]["ok"], str(rep["deliveryAudit"]["missing"]))
        else:
            # 没有 ffmpeg 也要如实：implicit 必须为 False
            meta = dm.build_delivery_meta(workdir=d, episode_id="E1",
                                          models=["minimax-h3"], masters=[str(mp4)])
            check("C1 ★★ 无 ffmpeg -> implicit=False（不回填）",
                  (meta.get("aiLabels") or {}).get("implicit") is False, "")
            check("C2 ★★ 无 ffmpeg -> errors 里有记录",
                  bool(meta.get("errors")), str(meta.get("errors"))[:60])
            check("C3 ★★ 无 master 时显式标识仍可生成（片尾卡）",
                  (d / "delivery" / "ai_label_endcard.png").is_file(), "")
            check("C4 ★★ 素材清单/署名仍落盘",
                  (d / "delivery" / "sources.json").is_file()
                  and (d / "delivery" / "credits.txt").is_file(), "")
            check("C5 ★★ 交付腿因缺隐式标识不通过（诚实）",
                  not _audit(delivery=dict(meta))["deliveryAudit"]["ok"], "")
            check("C6 ★★ 未知模型让 sourceRegistryOk=False",
                  (dm.build_delivery_meta(workdir=d, episode_id="E1",
                                          models=["ghost-model"])
                   .get("sourceRegistryOk") is False), "")

    print(); print("D. 诚实边界")
    check("D1 ★★★ 未知来源 -> sourceRegistryOk=False",
          dm.build_delivery_meta(workdir=Path(tempfile.mkdtemp()),
                                 episode_id="E2",
                                 models=["ghost-model"]).get("sourceRegistryOk")
          is False, "")

    n = sum(1 for _, ok, _ in _r if ok)
    print()
    for name, ok, detail in _r:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 68)
    print(f"结果：{n}/{len(_r)} 通过")
    return 0 if n == len(_r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
