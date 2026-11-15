"""造型版本化自检（2026-11-15）：换装 = 角色设计 + 资产版本 + 按镜选择。

锁住五件事：
1 **资产发现**：`assets/characters/<角色>/outfits/<variant>/` 结构可发现；
   目录不规范/缺图不编造
2 **外观文字**：来自 `outfit.json`，逐字稳定（IR 哈希的前提）
3 **按镜选择**：`shotId` 优先于 `sceneId`；没有版本落回默认造型
4 **锚点替换**：选中版本后参考图换成该版本的锚点（不是默认图）
5 **IR**：造型写进提示词与 IR 顶层 → 换装必重渲
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def _char(base: Path, name: str, variant: str, *,
          appearance: str = "", image: bool = True) -> Path:
    d = base / "characters" / name / "outfits" / variant
    d.mkdir(parents=True, exist_ok=True)
    if image:
        (d / "anchor.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (d / "outfit.json").write_text(
        json.dumps({"appearance": appearance, "name": variant},
                   ensure_ascii=False), encoding="utf-8")
    return d


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 68)
    print("造型版本化（outfits）—— 自检")
    print("=" * 68)

    from little_crew.runtime import anchors as A

    print(); print("A. 资产发现")
    with tempfile.TemporaryDirectory() as td:
        b = Path(td)
        _char(b, "少女", "rain", appearance="深灰连帽雨衣")
        _char(b, "少女", "default", appearance="灰紫短上衣")
        _char(b, "旅人", "rain", appearance="黑色风衣")   # 只有一张
        base = b / "characters" / "少女" / "anchor.png"
        base.write_bytes(b"\x89PNG\r\n\x1a\n")
        table = A.discover_outfits(b)
        check("A1 ★★★ 发现两角色的造型版本",
              set(table) == {"少女", "旅人"} and
              set(table["少女"]) == {"rain", "default"}, str({k: list(v) for k, v in table.items()}))
        check("A2 ★★★ 外观文字来自 outfit.json",
              table["少女"]["rain"]["appearance"] == "深灰连帽雨衣",
              table["少女"]["rain"]["appearance"])
        check("A3 ★★ 版本名可读（name 字段）",
              table["少女"]["rain"]["name"] == "rain", "")
        # 缺图版本不收录
        d = b / "characters" / "少女" / "outfits" / "broken"
        d.mkdir(parents=True)
        (d / "outfit.json").write_text("{}", encoding="utf-8")
        check("A4 ★★★ 缺图的版本被跳过（不编造资产）",
              "broken" not in A.discover_outfits(b).get("少女", {}), "")

    print(); print("B. 按角色取版本")
    check("B1 ★★★ 只返回存在的角色/版本",
          set(A.outfits_for(("少女", "旅人", "路人"), table, "rain")) == {"少女", "旅人"},
          "")
    check("B2 ★★★ 不存在的版本 -> 空（不落回别版）",
          A.outfits_for(("少女",), table, "nope") == {}, "")
    check("B3 ★★ 空参数 -> 空", A.outfits_for((), table, "rain") == {}
          and A.outfits_for(("少女",), {}, "rain") == {}, "")

    print(); print("C. 按镜选择（episode）")
    from little_crew.runtime.episode import EpisodeRunner

    r = EpisodeRunner.__new__(EpisodeRunner)
    r.outfit_table = table
    r.costume_map = {"S1": "rain", "SC1": "default"}
    c1 = r._costume_for(SimpleNamespace(shot_id="S1", scene_id="SC1",
                                       characters=("少女",)))
    check("C1 ★★★ shotId 优先于 sceneId",
          c1.get("variant") == "rain", str(c1.get("variant")))
    c2 = r._costume_for(SimpleNamespace(shot_id="S2", scene_id="SC1",
                                       characters=("少女",)))
    check("C2 ★★★ 场次级覆盖生效", c2.get("variant") == "default", "")
    check("C3 ★★★ 外观按角色合并（供 IR 逐字注入）",
          "少女：" in str(c1.get("appearance")) and "深灰连帽雨衣" in str(c1.get("appearance")),
          str(c1.get("appearance")))
    c3 = r._costume_for(SimpleNamespace(shot_id="S3", scene_id="SC3",
                                       characters=("少女",)))
    check("C4 ★★★ 没有覆盖 -> 不换装", c3 == {}, str(c3))
    r.costume_map = {"S1": "rain"}
    c4 = r._costume_for(SimpleNamespace(shot_id="S1", scene_id="",
                                       characters=("路人",)))
    check("C5 ★★★ 角色没有该造型 -> 不生效（不硬套）", c4 == {}, str(c4))
    check("C6 ★★★ 选中版本的锚点就是该版本图（不是默认图）",
          c1["characters"]["少女"]["image"].endswith("rain\\anchor.png")
          or c1["characters"]["少女"]["image"].endswith("rain/anchor.png"),
          c1["characters"]["少女"]["image"])

    print(); print("D. IR")
    from little_crew.agent.ir_bridge import shot_to_ir
    from little_crew.agent.messages import Shot
    from little_crew.ir.canonical import content_hash

    s = Shot(frm="a", to="b", shot_id="S1", scene_id="S01",
             shot_size="medium_close", camera_movement="static",
             action="stand", duration_sec=3.0, characters=("少女",),
             description="雨中等待")
    ir1 = shot_to_ir(s, episode_id="E1", prompt_language="zh",
                     costume="rain", costume_appearance="深灰连帽雨衣")
    ir2 = shot_to_ir(s, episode_id="E1", prompt_language="zh")
    p1 = next(n for n in ir1["nodes"] if n["kind"] == "prompt.text")["params"]["text"]
    check("D1 ★★★ 造型写进提示词", "造型：深灰连帽雨衣" in p1, p1[:80])
    check("D2 ★★★ IR 顶层带 costume（可审计）",
          ir1.get("costume") == {"id": "rain", "appearance": "深灰连帽雨衣"},
          str(ir1.get("costume")))
    check("D3 ★★★ 换装 -> IR 哈希变化（必重渲）",
          content_hash(ir1) != content_hash(ir2), "")

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
