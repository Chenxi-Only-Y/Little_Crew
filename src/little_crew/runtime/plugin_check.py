"""插件注册表自检（2026-11-17）：**新职业/功能插件化**。

- 清单/注册/卸载/查能力
- 缺插件给出明确错误，不影响默认 19 职位
- showrunner 插件：load → 提供 `series.plan`；卸载 → 能力消失
"""
from __future__ import annotations

import sys

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


class _FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.prompts = []

    def chat_json(self, prompt, *, system="", what=""):
        self.prompts.append(prompt)
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("插件化：注册/卸载/能力 —— 自检")
    print("=" * 66)

    from little_crew.agent.roster import DEFAULT_ROSTER
    from little_crew.plugin import (PluginError, PluginManifest,
                                    get_registry, load_plugin, load_plugins)

    print(); print("A. 清单校验")
    check("A1 ★★★ 缺 name -> 拒绝",
          _raises(lambda: PluginManifest(name="", provides={"x": lambda: 1})), "")
    check("A2 ★★★ 没有能力 -> 拒绝",
          _raises(lambda: PluginManifest(name="p", provides={})), "")
    check("A3 ★★ 能力不可调用 -> 拒绝",
          _raises(lambda: PluginManifest(name="p", provides={"x": 1})), "")

    reg = get_registry()
    print(); print("B. 注册/卸载/查询")
    m = PluginManifest(name="test_plugin", version="1.0",
                       provides={"demo.run": lambda x=1: x + 1})
    reg.register(m)
    check("B1 ★★★ 注册后可查", reg.get("test_plugin") is m, "")
    check("B2 ★★★ 能力映射到插件", reg.capabilities().get("demo.run") == "test_plugin", "")
    check("B3 ★★★ provider 可取到函数并调用",
          reg.provider("demo.run")(2) == 3, "")
    check("B4 ★★★ 重复注册 -> 拒绝", _raises(lambda: reg.register(m)), "")
    check("B5 ★★ 卸载后能力消失",
          reg.unload("test_plugin") is True and reg.get("test_plugin") is None
          and "demo.run" not in _cap(reg), "")
    check("B6 ★★★ 缺能力 -> 明确错误",
          _raises(lambda: reg.provider("no.such")), "")
    check("B7 ★★ 卸载不存在的插件 -> False", reg.unload("no.such") is False, "")

    print(); print("C. showrunner 插件（新职业不进默认名册）")
    p = load_plugin("little_crew.plugins.series_showrunner")
    check("C1 ★★★ 插件加载成功且提供 series.plan",
          p.name == "series_showrunner" and "series.plan" in p.provides, str(p.name))
    check("C2 ★★★ 默认 19 职位名册不变（插件不挤占）",
          len(DEFAULT_ROSTER) == 19, str(len(DEFAULT_ROSTER)))
    outline = {"seriesId": "T", "logline": "末班地铁",
               "volumes": [{"name": "第一卷"}],
               "chapters": [{"chapterId": f"CH0{i}", "name": f"第{i}章",
                             "synopsis": f"梗概{i}"} for i in (1, 2, 3)]}
    fn = get_registry().provider("series.plan")
    got = fn(outline, client=None)
    check("C3 ★★★ 无 LLM -> 结构化占位（人工可补）",
          got["ok"] is True and len(got["plan"]["chapters"]) == 3, str(got["errors"]))
    check("C4 ★★ 占位 plan 通过契约校验", got["errors"] == [], str(got["errors"]))
    fake = _FakeClient({"schema": "little_crew.chapter_plan/v1",
                        "logline": "末班地铁",
                        "chapters": [{
                            "chapterId": "CH01", "name": "末班车",
                            "card": {"goal": "等车", "conflict": "车没了", "hook": "他来"},
                            "entry": {"placeKey": "站台", "time": "夜",
                                      "characters": ["少女"], "props": [], "openThreads": []},
                            "exit": {"placeKey": "站台", "time": "夜",
                                     "characters": ["少女", "旅人"], "props": [],
                                     "openThreads": ["他是谁"]},
                            "dependsOn": []}]})
    got2 = fn({"seriesId": "T", "logline": "末班地铁",
               "chapters": [{"chapterId": "CH01", "name": "末班车", "synopsis": "等车"}]},
              client=fake)
    check("C5 ★★★ LLM 正常 -> source=llm-showrunner",
          got2["source"] == "llm-showrunner" and got2["ok"] is True, str(got2.get("error")))
    got3 = fn(outline, client=_FakeClient(RuntimeError("boom")))
    check("C6 ★★★ LLM 挂 -> 退回占位并记录错误",
          got3["source"] == "fallback" and "boom" in str(got3.get("error")), "")
    check("C7 ★★ 总导演 prompt 明确不写正文",
          any("只规划，不写正文" in x for x in fake.prompts), "")
    reg.unload("series_showrunner")
    check("C8 ★★★ 卸载 showrunner 后能力消失（默认 19 职位不受影响）",
          "series.plan" not in _cap(reg) and len(DEFAULT_ROSTER) == 19, "")

    print(); print("D. 插件依赖（requires）：拓扑加载 / 缺依赖拒绝 / 卸载保护")
    from little_crew.plugin import PluginManifest as PM

    def _mk(name, cap, requires=()):
        return PM(name=name, provides={cap: (lambda: name)},
                  requires=tuple(requires))

    check("D1 ★★ requires 空值 -> 拒绝",
          _raises(lambda: PM(name="x", provides={"a": lambda: 1}, requires=("",))), "")
    a = _mk("dep_a", "cap.a")
    b = _mk("dep_b", "cap.b", requires=("cap.a",))
    missing = _mk("dep_missing", "cap.m", requires=("cap.nope",))
    check("D2 ★★★ 依赖未满足 -> 加载拒绝且不注册",
          _raises(lambda: load_plugin(_ModuleWith(missing)))
          and reg.get("dep_missing") is None, "")
    # 直接走注册表语义：先注册 A，再 load B（模拟依赖顺序）
    reg.register(a)
    got = load_plugin(_ModuleWith(b))
    check("D3 ★★★ 依赖已满足 -> 加载成功", got.name == "dep_b", str(got.name))
    check("D4 ★★★ 卸载被依赖的插件 -> 阻止（有插件依赖它）",
          _raises(lambda: reg.unload("dep_a")), "")
    reg.unload("dep_b")
    check("D5 ★★ 依赖方卸载后，被依赖方可卸载", reg.unload("dep_a") is True, "")
    # 缺依赖：直接 load 一个依赖未知能力的插件
    c = _mk("dep_c", "cap.c", requires=("cap.missing",))
    try:
        load_plugin(_ModuleWith(c))
        ok = False
    except Exception:
        ok = True
    check("D6 ★★★ 缺依赖 -> 加载失败且不注册",
          ok and reg.get("dep_c") is None, "")
    core = _mk("dep_core", "cap.core", requires=("llm.client",))
    check("D7 ★★ 依赖核心能力（llm.client）允许",
          load_plugin(_ModuleWith(core)).name == "dep_core", "")
    reg.unload("dep_core")

    n = sum(1 for _, ok, _ in _r if ok)
    print()
    for name, ok, detail in _r:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 66)
    print(f"结果：{n}/{len(_r)} 通过")
    return 0 if n == len(_r) else 1


class _ModuleWith:
    """把 manifest 伪装成模块，供 load_plugin 测试。"""

    def __init__(self, manifest):
        self.PLUGIN = manifest
        self.__name__ = f"fake_{manifest.name}"


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except Exception:                                        # noqa: BLE001
        return True


def _cap(reg) -> dict:
    try:
        return reg.capabilities()
    except Exception:                                        # noqa: BLE001
        return {}


if __name__ == "__main__":
    raise SystemExit(main())
