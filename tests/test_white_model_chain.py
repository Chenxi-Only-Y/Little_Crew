"""白模生成链路：`white_model` 规格 / `white_model_prompt` 提示词 / 节点 LLM 配置。

## 为什么要有这个文件（2026-10-08 实测踩的两个坑）

用户说"测试下白模生成"，结果**功能 100% 不可用**，而且两个坑都不报错、只是坏掉：

### 坑一：`white_model_prompt` **模块导入就崩**

```
File "app/services/white_model_prompt.py", line 46, in <module>
    _JOINT_TABLE = "\n".join(... white_model.JOINT_LIMITS[name] ...)
KeyError: 'pelvis'
```

它遍历 `JOINT_PARENT`（19 项，含根关节 `pelvis`）去下标 `JOINT_LIMITS`
（手写表 18 项，**刻意不含根关节** —— 根关节没有父关节可绕之旋转，
不该有限位）。两张表键不一致，于是 import 就炸。
后果：`/api/previs/white-model` 一进来就 **500，0.0 秒**，白模完全不可用。

**为什么测试没抓到**：此前**没有任何测试 import 过 `white_model_prompt`**。
而 `white_model.validate` 自己用的是 `JOINT_LIMITS.get(joint, 默认)`
——有兜底，所以它那一侧一直是好的，两边行为不一致。

修法：把 `JOINT_LIMITS` 在**源头**归一成"`JOINT_PARENT` 里每个关节都有条目"，
根关节拿 `DEFAULT_JOINT_LIMIT`。这样所有消费者（含 `prompt` 的直接下标）都安全。

### 坑二：`previs_artist` 是**唯一没声明文字模型三键**的 LLM 节点

别的 LLM 节点（writer / critics / screenwriter / …）都声明了
`llm_provider_{role}` / `llm_model_{role}` / `llm_api_key_{role}`，
设置页里能填；`previs_artist` 只有 `previs_mode` / `previs_size`。

于是 `generate_spec(..., role="previs_artist")` 取不到专属密钥 →
回退全局 `llm_api_key`（`.env` 里是空的）→ 400：
"LLM 客户端未初始化（provider=deepseek, role=previs_artist）"。
**而且用户在设置页里根本找不到能填的地方。**
"""

from __future__ import annotations

import pytest


# ----------------------------------------------------------------------
# 坑一：两张关节表必须键一致（否则 prompt 模块 import 崩）
# ----------------------------------------------------------------------

def test_joint_limits_covers_every_joint_in_parent():
    """**核心**：`JOINT_LIMITS` 必须覆盖 `JOINT_PARENT` 的每一个关节。

    不覆盖的后果不是"限位缺失"，而是**模块导入直接 KeyError**（见文件头）。
    """
    from app.services.white_model import JOINT_LIMITS, JOINT_PARENT

    missing = sorted(set(JOINT_PARENT) - set(JOINT_LIMITS))
    assert not missing, (
        f"这些关节在 JOINT_PARENT 里但 JOINT_LIMITS 没有：{missing}。"
        f"任何遍历 JOINT_PARENT 去下标 JOINT_LIMITS 的代码都会 KeyError。")


def test_joint_limits_has_no_orphan_entries():
    """反向也要一致：`JOINT_LIMITS` 里不该有 `JOINT_PARENT` 没有的名字。"""
    from app.services.white_model import JOINT_LIMITS, JOINT_PARENT

    extra = sorted(set(JOINT_LIMITS) - set(JOINT_PARENT))
    assert not extra, f"JOINT_LIMITS 里有 JOINT_PARENT 没有的关节：{extra}"


def test_root_joint_gets_the_default_limit():
    """根关节（`pelvis`）没有父关节可绕之旋转，因此**没有专属限位**，
    用默认值兜底 —— 这是设计意图，不是漏写。"""
    from app.services.white_model import (
        DEFAULT_JOINT_LIMIT,
        JOINT_LIMITS,
        JOINT_PARENT,
    )

    roots = [j for j, p in JOINT_PARENT.items() if p is None]
    assert roots, "应该至少有一个根关节"
    for r in roots:
        assert JOINT_LIMITS[r] == DEFAULT_JOINT_LIMIT


def test_non_root_joints_keep_their_explicit_limits():
    """兜底不能把**有生理含义**的限位覆盖掉。抽查几个。

    ⚠️ 别把轴向记错：`[0]` 是绕 X（前后摆动）那一对 `(下界, 上界)`。
      · 膝盖 X 下界 = 0.0（只能向后弯，不能反折）
      · 肘部 X 下界 = **-2.6**（能大幅前摆），**上界 0.05**（几乎不能后折）
    """
    from app.services.white_model import JOINT_LIMITS

    assert JOINT_LIMITS["kne_l"][0][0] == 0.0
    assert JOINT_LIMITS["kne_r"][0][0] == 0.0
    assert JOINT_LIMITS["elb_l"][0] == (-2.6, 0.05)
    assert JOINT_LIMITS["elb_r"][0] == (-2.6, 0.05)
    # 头不能左右扭到 180°
    assert JOINT_LIMITS["head"][1] == (-1.2, 1.2)


def test_direct_subscript_is_safe_for_every_joint():
    """**直接下标**也不能炸 —— `white_model_prompt` 就是这么用的。

    这条是那个 500 的最小复现：它保证"遍历 JOINT_PARENT 直接取限位"可行。
    """
    from app.services.white_model import JOINT_LIMITS, JOINT_PARENT

    for name in JOINT_PARENT:
        lo_hi = JOINT_LIMITS[name]           # 直接下标，不带 .get
        assert len(lo_hi) == 3, f"{name} 的限位不是三个轴"
        for axis in lo_hi:
            assert len(axis) == 2 and axis[0] <= axis[1], f"{name} 的限位区间非法"


# ----------------------------------------------------------------------
# 坑一的表现层：`white_model_prompt` 必须能 import 且关节表完整
# ----------------------------------------------------------------------

def test_white_model_prompt_imports():
    """**最小回归**：这个模块曾经 import 就 KeyError。

    ⚠️ 断言"能导入"看着很傻，但这正是当时挂掉的地方 ——
    端点 500、0.0 秒，连 LLM 都没调到。
    """
    import app.services.white_model_prompt as wmp  # noqa: F401

    assert wmp._JOINT_TABLE


def test_joint_table_lists_every_joint():
    """提示词里的关节表要列全 —— 漏了模型会自己发明 `hand_l` 这种名字。"""
    from app.services import white_model
    from app.services import white_model_prompt as wmp

    for name in white_model.JOINT_PARENT:
        assert name in wmp._JOINT_TABLE, f"关节表里缺 {name}"


def test_joint_table_marks_the_root_joint():
    """根关节要标出来（提示词里写「（根）」），否则模型以为它也要转。"""
    from app.services import white_model_prompt as wmp

    assert "（根）" in wmp._JOINT_TABLE


def test_system_prompt_builds_without_error():
    """拼一次完整的 system 提示词 —— 覆盖 f-string 里所有占位。"""
    from app.services import white_model_prompt as wmp

    assert len(wmp._SYSTEM) > 500
    assert "JOINT" in wmp._SYSTEM.upper() or "关节" in wmp._SYSTEM


# ----------------------------------------------------------------------
# 坑二：凡是走 LLM 的节点都必须声明文字模型三键
# ----------------------------------------------------------------------

LLM_CONFIG_SUFFIXES = ("llm_provider", "llm_model", "llm_api_key")


def test_every_llm_capable_node_declares_the_three_keys():
    """**核心**：`capability` 含 `llm` 的节点必须能配自己的密钥。

    不声明的后果：`role=xxx` 取不到专属密钥 → 回退全局（可能为空）
    → 运行时报"LLM 客户端未初始化"，**而设置页里根本没有能填的地方**。
    `previs_artist` 就这么漏了（2026-10-08 实测）。

    ⚠️ 两个容易搞错的点：
      1. `capability` / `config` 在 **Node** 上（`all_nodes()`），不在 Role 上；
      2. 键名用的是**配置命名空间**，未必等于节点 key ——
         `technical_director` → `llm_api_key_director`。
         见 `CONFIG_NAMESPACE_BY_KEY`。
    """
    from app.registry.api import all_nodes
    from app.registry.roles import CONFIG_NAMESPACE_BY_KEY

    missing: list[str] = []
    for n in all_nodes():
        caps = n.capability or ()
        if "llm" not in caps:
            continue
        # ① 自己的命名空间（无映射时就是节点 key）
        ns = CONFIG_NAMESPACE_BY_KEY.get(n.key, n.key)
        keys = {c.key for c in (n.config or ())}
        cands = [
            any(k == f"{s}_{ns}" for k in keys) for s in LLM_CONFIG_SUFFIXES
        ]
        # ② 历史别名命名空间（如 technical_director 的 legacy 'director'）
        if not all(cands):
            for alt in ("writer", "critics", "director", "screenwriter"):
                if all(any(k == f"{s}_{alt}" for k in keys)
                       for s in LLM_CONFIG_SUFFIXES):
                    cands = [True] * 3
                    break
        # ③ 全局那套（showrunner 用 llm_provider_mode + 自己的 model/key）
        if not all(cands):
            cands = [any(k == s or k == f"{s}_{ns}" for k in keys)
                     for s in LLM_CONFIG_SUFFIXES]
        if not all(cands):
            missing.append(f"{n.key}(ns={ns})")
    assert not missing, (
        f"这些节点需要 LLM 但没声明 provider/model/api_key：{missing}。"
        f"用户在设置页会找不到填密钥的地方。")


def test_previs_artist_declares_llm_keys():
    """白模预演师必须能单独配模型 —— 白模规格就是让 LLM 生成 JSON 的。"""
    from app.registry.api import all_nodes

    node = next(n for n in all_nodes() if n.key == "previs_artist")
    keys = {c.key for c in (node.config or ())}
    for k in ("llm_provider_previs_artist", "llm_model_previs_artist",
              "llm_api_key_previs_artist"):
        assert k in keys, f"previs_artist 缺 {k}"


def test_previs_artist_llm_keys_are_settable():
    """声明了还要能**写进设置**（白名单由注册表派生，这条守它真的接上了）。"""
    from app.registry.api import allowed_settings

    s = allowed_settings()
    for k in ("llm_provider_previs_artist", "llm_model_previs_artist",
              "llm_api_key_previs_artist"):
        assert k in s, f"{k} 不在 allowed_settings（PUT /api/settings 会静默丢弃）"
