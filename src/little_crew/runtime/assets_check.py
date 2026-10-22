"""`runtime/assets.py` 自检 —— 纯文件读写，不联网。

## 守什么

| 段 | 守什么 |
|---|---|
| A 写入 | `assets/characters/<角色>/sheet.json` + `assets/manifest.json` |
| B 来源 | 每份资产记 `sourceEpisodeId` / `sourceSeed` / `writtenAt`（**可追溯**） |
| C 重名冲突 | ★ 同名不同 ID **不覆盖**，另起目录并标 `conflict` |
| D 读回 | `read_characters` 拿回全部定妆表，供下一集复用 |
| E 目录名安全 | 非法字符被清掉；中文原样保留；空名有兜底 |

## 为什么 C 段是重点

两个"少女"其实是两个人。**静默覆盖之后没人能发现** ——
而这种错会直接表现成"角色长得不一样了"，却查不到原因。
所以宁可多一个目录，也要让冲突**显性**。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from little_crew.runtime.assets import (
    ASSETS_DIR,
    MANIFEST_NAME,
    load_manifest,
    read_characters,
    safe_name,
    write_characters,
)

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


def _sheet(name: str, cid: str) -> dict:
    return {
        "kind": "character.sheet", "name": name, "characterId": cid,
        "appearance": {"hair_style": "高马尾", "outfit": "亚麻长裙"},
        "promptFragment": "白发高马尾的少女",
    }


def _write() -> None:
    print()
    print("A. 写入")
    with tempfile.TemporaryDirectory() as d:
        wd = Path(d)
        man = write_characters(wd, [_sheet("少女", "CHAR-069A"),
                                    _sheet("旅人", "CHAR-0B12")],
                               episode_id="E100", seed="abc123")
        root = wd / ASSETS_DIR
        check("A1 清单写在 assets/manifest.json",
              (root / MANIFEST_NAME).is_file(), str(root))
        check("A2 每个角色一个目录 + sheet.json",
              (root / "characters" / "少女" / "sheet.json").is_file()
              and (root / "characters" / "旅人" / "sheet.json").is_file(), "")
        check("A3 清单条数 = 2", len(man["characters"]) == 2,
              str(len(man.get("characters") or [])))
        e0 = man["characters"][0]
        check("A4 清单里路径是**相对 workdir 的 POSIX 路径**",
              e0["path"] == "assets/characters/少女/sheet.json", e0["path"])
        check("A5 没有图时不写假的 anchor（留 None）",
              e0["anchor"] is None, str(e0.get("anchor")))


def _provenance() -> None:
    print()
    print("B. 来源可追溯")
    with tempfile.TemporaryDirectory() as d:
        wd = Path(d)
        write_characters(wd, [_sheet("少女", "CHAR-069A")],
                         episode_id="E100", seed="abc123")
        sh = json.loads((wd / ASSETS_DIR / "characters" / "少女"
                         / "sheet.json").read_text(encoding="utf-8"))
        check("B1 ★ 记了来自哪一集", sh.get("sourceEpisodeId") == "E100",
              str(sh.get("sourceEpisodeId")))
        check("B2 ★ 记了 seed（复现用）", sh.get("sourceSeed") == "abc123",
              str(sh.get("sourceSeed")))
        check("B3 记了写入时间", bool(sh.get("writtenAt")), str(sh.get("writtenAt")))
        check("B4 原始字段没丢（appearance / promptFragment）",
              sh.get("appearance", {}).get("hair_style") == "高马尾"
              and bool(sh.get("promptFragment")), str(list(sh)))


def _conflict() -> None:
    print()
    print("C. ★ 同名不同 ID 不覆盖")
    with tempfile.TemporaryDirectory() as d:
        wd = Path(d)
        write_characters(wd, [_sheet("少女", "CHAR-069A")], episode_id="E100")
        man2 = write_characters(wd, [_sheet("少女", "CHAR-0B12")], episode_id="E101")
        names = sorted(p.name for p in (wd / ASSETS_DIR / "characters").iterdir())
        check("C1 ★★ 两个目录都在（没有覆盖）", len(names) == 2, str(names))
        check("C2 ★★ 新目录带 ID 后缀", any("@" in n for n in names), str(names))
        check("C3 ★★ 清单标了 conflict=True",
              man2["characters"][0]["conflict"] is True,
              str(man2["characters"][0].get("conflict")))
        old = json.loads((wd / ASSETS_DIR / "characters" / "少女" / "sheet.json")
                         .read_text(encoding="utf-8"))
        check("C4 ★★ 老的定妆表还是老 ID（没被改写）",
              old.get("characterId") == "CHAR-069A", str(old.get("characterId")))
        # 同一 ID 重写 -> 不产生冲突
        man3 = write_characters(wd, [_sheet("少女", "CHAR-069A")], episode_id="E102")
        check("C5 同一 ID 重写不报冲突", man3["characters"][0]["conflict"] is False,
              str(man3["characters"][0].get("conflict")))


def _readback() -> None:
    print()
    print("D. 读回（下一集复用）")
    with tempfile.TemporaryDirectory() as d:
        wd = Path(d)
        got0 = read_characters(wd)
        check("D1 没有资产时返回空 dict（不抛）", got0 == {}, str(got0))
        check("D2 load_manifest 没有文件时也给空清单",
              (load_manifest(wd).get("characters") == []), "崩了")
        write_characters(wd, [_sheet("少女", "CHAR-069A"),
                              _sheet("旅人", "CHAR-0B12")], episode_id="E100")
        got = read_characters(wd)
        check("D3 ★ 拿回 2 个角色", len(got) == 2, str(list(got)))
        check("D4 ★ 键是角色名", set(got) == {"少女", "旅人"}, str(list(got)))
        check("D5 ★ 定妆内容完整（可直接进提示词）",
              got["少女"].get("promptFragment") == "白发高马尾的少女",
              str(got["少女"].get("promptFragment")))
        check("D6 读回的也带来源", got["少女"].get("sourceEpisodeId") == "E100",
              str(got["少女"].get("sourceEpisodeId")))


def _safe() -> None:
    print()
    print("E. 目录名安全")
    check("E1 中文原样保留", safe_name("少女") == "少女", safe_name("少女"))
    check("E2 路径分隔符被清", "/" not in safe_name("a/b") and "\\" not in safe_name(r"a\b"),
          safe_name(r"a\b"))
    check("E3 通配符被清", "*" not in safe_name("a*b?c"), safe_name("a*b?c"))
    check("E4 空名有兜底", safe_name("") == "unnamed", safe_name(""))
    check("E5 全坏字符也有兜底", safe_name("///") == "unnamed", safe_name("///"))
    check("E6 过长被截断", len(safe_name("长" * 200)) <= 40,
          str(len(safe_name("长" * 200))))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")            # type: ignore[attr-defined]
    except Exception:                                        # noqa: BLE001
        pass
    print("=" * 74)
    print("资产库 —— 自检（纯文件）")
    print("=" * 74)
    _write()
    _provenance()
    _conflict()
    _readback()
    _safe()
    print()
    npass = sum(1 for _, ok, _ in _results if ok)
    for name, ok, detail in _results:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 74)
    print(f"结果：{npass}/{len(_results)} 通过")
    return 0 if npass == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
