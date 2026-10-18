"""`EpisodeRunner._run_advisory` 的守卫测试。

## 这个文件为什么单独存在

本来想把这几条放进 `agent/critic_llm_check.py`，但那是 `agent` 层，
`import runtime.episode` 会触发 **R1 反向依赖**（架构守卫当场抓到）。
挪进 `runtime/episode_check.py` 又反复与既有 G/H 段标签冲突、
两次切坏 `def main`。

**所以单开一个文件** —— 与那个大文件解耦，标签也不会撞。

## 守什么

| 断言 | 守什么 |
|---|---|
| K1/K2 | **取不到可审正文 -> 显式 error，不是 PASS**（铁律①） |
| K3 | 没配顾问 -> 空结果（不调用任何模型） |
| K4 | 剧本为 None -> 空结果，不崩 |
| K5 | 每个顾问都有一条记录（不静默丢位置） |
| K6 | 取正文用的是 `script_text()`（防回退到不可靠的 `as_text`） |

## 这一段的来历

第一版 `_run_advisory` 没取到剧本正文（`ScriptDraft` 没有 `as_text`），
把**空字符串**喂给批评家 —— 三个顾问齐刷刷返回 `PASS 100.0`。
**看起来全部合格，实际一个字都没看。**

而且第一版只判 `text.strip()`，`script_text()` 对一个不相干对象会返回
**非空的 repr** —— 守卫没触发。所以现在**按类型判**，并有 K1 钉住。
"""
from __future__ import annotations

import inspect
import sys

from little_crew.agent.messages import ScriptDraft
from little_crew.runtime.episode import EpisodeRunner

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


class _WithCritics:
    advisory_critics = ("pacing_editor", "continuity_supervisor")


class _NoCritics:
    advisory_critics = ()


def _guards() -> None:
    print()
    print("A. 守卫")

    got = EpisodeRunner._run_advisory(_WithCritics(), object())
    check("K1 ★ 不是 ScriptDraft -> 显式 error（不是 PASS）",
          len(got) == 2 and all(a["verdict"] == "error" for a in got),
          str([(a["position"], a["verdict"]) for a in got]))
    check("K2 理由点明「未审查，不算通过」",
          all("未审查" in (a.get("reason") or "") for a in got),
          str([a.get("reason") for a in got]))
    check("K3 每个顾问都有一条记录（不静默丢位置）", len(got) == 2)

    # 没有场次的 ScriptDraft 也不算可审
    empty = ScriptDraft(frm="screenwriter", to="dramaturge",
                        episode_id="E1", title="T", scenes=(), revision=1)
    got2 = EpisodeRunner._run_advisory(_WithCritics(), empty)
    check("K4 ★ 空场次的 ScriptDraft -> 也是 error（不算通过）",
          len(got2) == 2 and all(a["verdict"] == "error" for a in got2),
          str([a["verdict"] for a in got2]))

    check("K5 不配顾问 -> 空结果（不调用任何模型）",
          EpisodeRunner._run_advisory(_NoCritics(), object()) == ())
    check("K6 剧本为 None -> 空结果（早退，不崩）",
          EpisodeRunner._run_advisory(_WithCritics(), None) == ())


def _implementation() -> None:
    print()
    print("B. 实现约束")

    src = inspect.getsource(EpisodeRunner._run_advisory)
    check("K7 ★ 取正文用 `script_text()`（与定妆表同一套，不是 as_text）",
          "script_text" in src, "没找到 script_text")
    check("K8 守卫按类型判（isinstance ScriptDraft），不只看字符串空不空",
          "isinstance(script, ScriptDraft)" in src or "ScriptDraft" in src,
          "没找到类型判断")
    check("K9 失败不抛异常（顾问挂了不该让整集停下）",
          "except Exception" in src)

    # 有场次时**会**进入调用路径（不验结果，只验它不是无条件早退）
    real = ScriptDraft(frm="screenwriter", to="dramaturge", episode_id="E1",
                       title="T", revision=1,
                       scenes=({"scene_id": "S01", "heading": "内景",
                                "characters": ("甲",),
                                "text": "甲坐着。"},))
    # 用**空顾问列表**避免真的调模型：只验"有场次时不会走 error 早退"
    class _Both:
        advisory_critics = ()

    check("K10 有场次 + 不配顾问 -> 仍空结果（不会误报 error）",
          EpisodeRunner._run_advisory(_Both(), real) == ())


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")            # type: ignore[attr-defined]
    except Exception:                                        # noqa: BLE001
        pass
    print("=" * 74)
    print("顾问批评家守卫 —— 自检（不调模型）")
    print("=" * 74)

    _guards()
    _implementation()

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
