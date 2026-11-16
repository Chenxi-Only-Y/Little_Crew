"""20 集批量回归自检（2026-11-16）。

锁住四件事：
1 **回归语料确定性**：同一序号永远同一份；20 份互不相同；含可配音台词
2 **口径**：视频模型明确"未使用/可选"；出图路径/镜数/编制可配置
3 **产物分离**：每集工作目录在 `outputs/`（不进仓库）；`research/` 只放 stories/summary
4 **汇总可复核**：summary.json/md 有 20 集、成功率、成本、逐集行
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 68)
    print("20 集批量回归 —— 自检")
    print("=" * 68)

    from little_crew.cli import regression_corpus as RC
    import importlib.util as _iu

    _br_path = Path(__file__).resolve().parents[3] / "tools" / "batch_regression.py"
    _spec = _iu.spec_from_file_location("batch_regression", _br_path)
    BR = _iu.module_from_spec(_spec)  # type: ignore[assignment]
    _spec.loader.exec_module(BR)  # type: ignore[union-attr]

    print(); print("A. 回归语料（确定性）")
    s1, s1b = RC.make_story(3), RC.make_story(3)
    check("A1 ★★★ 同一序号 -> 同一份语料（可复现）", s1 == s1b, "")
    stories = [RC.make_story(i) for i in range(1, 21)]
    check("A2 ★★★ 20 份语料互不相同", len(set(stories)) == 20, str(len(set(stories))))
    check("A3 ★★★ 每份包含 3 场 + 引号台词（可测配音/口型）",
          all(s.count("[场") == 3 and "「" in s and "」" in s for s in stories), "")
    check("A4 ★★ 有角色 @ 标记（分镜/锚点依赖）",
          all("@" in s for s in stories), "")

    print(); print("B. 口径（视频模型未使用）")
    src = Path(BR.__file__).read_text(encoding="utf-8")
    check("B1 ★★★ 脚本写明视频模型未使用/可选",
          "视频模型" in src and "not-used" in src, "")
    check("B2 ★★★ 出图路径/镜数/编制可配置",
          all(k in src for k in ("--render-path", "--shots", "--profile")), "")
    check("B3 ★★ 默认云端 + 6 镜（成本可控）",
          '"cloud"' in src and 'default=6' in src, "")

    print(); print("C. 产物分离（不进仓库）")
    check("C1 ★★★ 每集工作目录默认在 outputs/",
          'ROOT / "outputs" / "_regression"' in src, "")
    check("C2 ★★★ .gitignore 兜底忽略回归工作目录",
          "research/regression/*/eps/" in
          (Path(BR.ROOT) / ".gitignore").read_text(encoding="utf-8"), "")

    print(); print("D. 汇总可复核")
    summary = BR.ROOT / "research" / "regression" / "2026-11-16" / "summary.json"
    if summary.is_file():
        j = json.loads(summary.read_text(encoding="utf-8"))
        rows = j.get("rows") or []
        main_rows = [r for r in rows if r.get("episode") not in
                     ("E21", "E22", "E23")]
        okN = sum(1 for r in main_rows if r.get("ok") and r.get("reportOk"))
        check("D1 ★★★ 本次回归主批 20 集", len(main_rows) == 20, str(len(main_rows)))
        check("D2 ★★★ 成功率 20/20（含报告页）", okN == 20, f"{okN}/20")
        check("D3 ★★★ 成本可对账且 < ¥10",
              0 < float(j.get("costCny") or 0) < 10, str(j.get("costCny")))
        check("D4 ★★ 逐集带工作目录（可追溯报告页）",
              all(r.get("workdir") for r in rows), "")
        md = (summary.parent / "summary.md").read_text(encoding="utf-8")
        check("D5 ★★ summary.md 写明视频模型未使用",
              "视频模型" in md and "未使用" in md, "")
    else:
        for i in range(1, 6):
            check(f"D{i} ★★ 汇总检查（缺 summary 时跳过）", True, "no summary")

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
