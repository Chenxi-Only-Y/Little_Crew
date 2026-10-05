"""组织映射（职业 → 部门/中文名）与注册表的一致性测试。

## 为什么需要这个文件

`app/core/org.py` 的 `ROLE_ORG` 决定**产出资产落到哪个部门目录**
（`data/outputs/{账号}/{部门}/{职业}/...`）。它曾经和注册表
`app/registry/roles.py` **对不上**：key 还是更早一版的职业名，
实测 16 个职业里有 8 个查不到，`dept_for_role()` 全部落到兜底「综合」——

    showrunner · script_editor · previs_artist · continuity
    voice_director · supervising_editor · workflow_architect · technical_director

后果是这些职业的产出被归到「综合」，没法按部门找。
两张表都是手写的，**迟早还会漂移**，所以这里双向锁住：
每改一次注册表，测试会直接指出哪几个 key 对不上。
"""

from __future__ import annotations

import pytest

from app.core.org import ROLE_ORG, dept_for_role, label_for_role


def _registry_keys() -> list[str]:
    from app.registry.roles import ROLES

    out: list[str] = []
    for r in ROLES:
        key = r["key"] if isinstance(r, dict) else getattr(r, "key", None)
        if key:
            out.append(key)
    return out


def _registry_dept_labels() -> set[str]:
    from app.registry.roles import DEPARTMENTS

    labels = set()
    for d in DEPARTMENTS:
        lab = d["label"] if isinstance(d, dict) else getattr(d, "label", None)
        if lab:
            labels.add(lab)
    return labels


def test_every_registry_role_is_mapped():
    """注册表里每个职业都必须在 ROLE_ORG 里查到。

    查不到就会落到兜底「综合」，产出被归错部门——这是实测踩过的缺陷。
    """
    missing = [k for k in _registry_keys() if k not in ROLE_ORG]
    assert not missing, (
        f"以下注册表职业未在 app/core/org.py:ROLE_ORG 中登记，"
        f"产出会被归到兜底「综合」：{missing}"
    )


def test_no_zombie_keys_in_role_org():
    """ROLE_ORG 里不能有注册表已删除的 key（僵尸条目）。"""
    reg = set(_registry_keys())
    zombies = [k for k in ROLE_ORG if k not in reg]
    assert not zombies, (
        f"app/core/org.py:ROLE_ORG 里有注册表已不存在的 key（僵尸条目）：{zombies}"
    )


def test_dept_names_match_registry_departments():
    """ROLE_ORG 的部门名必须是注册表里真实存在的部门名。

    否则产出会落到一个"注册表里没有的部门目录"，前端按部门聚合时找不到。
    """
    known = _registry_dept_labels()
    used = {v[0] for v in ROLE_ORG.values()}
    unknown = sorted(used - known)
    assert not unknown, (
        f"ROLE_ORG 用了注册表里不存在的部门名：{unknown}；"
        f"注册表的部门是：{sorted(known)}"
    )


def test_lookup_returns_registry_values():
    """抽查几个曾经查不到的 key——它们正是被归到「综合」的那批。"""
    expected = {
        "showrunner": "策划",
        "script_editor": "策划",
        "previs_artist": "白膜",
        "supervising_editor": "后期",
        "continuity": "动态",
        "voice_director": "动态",
        "workflow_architect": "基建",
        "technical_director": "基建",
    }
    for role, dept in expected.items():
        assert dept_for_role(role) == dept, f"{role} 应归「{dept}」"
        assert dept_for_role(role) != "综合", f"{role} 不该走兜底"


def test_unknown_role_falls_back_without_raising():
    """未知职业不能抛异常，返回兜底值即可。

    产出落盘是"最后一公里"，因为它失败而丢掉已经花过钱的生成结果，
    代价远大于目录名不好看。
    """
    assert dept_for_role("no_such_role_xyz") == "综合"
    assert label_for_role("no_such_role_xyz") == "其他"
    assert dept_for_role("") == "综合"
    assert dept_for_role(None) == "综合"  # type: ignore[arg-type]


@pytest.mark.parametrize("role", ["photographer", "editor", "character_designer"])
def test_known_roles_have_non_empty_labels(role: str):
    assert label_for_role(role)
    assert label_for_role(role) != "其他"
