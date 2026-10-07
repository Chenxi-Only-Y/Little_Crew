"""编排能力（分段执行 + 确认点开关）的回归测试。

## 背景

`start_stage` / `end_stage` / `enabled_checkpoints` 是**文档化的功能**：

    docs/recon/03-before-after.md   "编排能力 = start_stage+end_stage 单段"
    docs/recon/04-frontend-rewrite.md §5.3
        start_stage        = 「从此节点开始」（上游置灰复用产物）
        end_stage          = 「到此节点结束」（下游置灰不执行）
        enabled_checkpoints = 节点上的 ⚑ 开关
    docs/recon/01-usage.md:88       "带 seed + start_stage=storyboarder 启动"（实测过）

但 `b9bb352`（修身份越权那次的**安全加固**）引入的 `_ALLOWED_SEED_KEYS`
白名单**漏了这三个键** → 传了被静默丢弃 + 记一条 warning →
用户侧表现是"设了没反应"。2026-10-07 节点排查发现并修掉。

这个文件守住三件事：
  1. 编排三键**必须能通过白名单**（安全键仍要被拒）；
  2. 区间跳过与确认点开关的**语义**（三态）；
  3. 生产路径（`start_run` → 图初始 state）里**真的生效**。
"""

from __future__ import annotations

import pytest


# ----------------------------------------------------------------------
# 1. 白名单：编排三键要过，敏感键要拦
# ----------------------------------------------------------------------

def test_orchestration_keys_pass_the_whitelist():
    """编排三键不能被白名单拦掉（拦了功能就是死的）。"""
    from app.services.task_runner import _sanitize_seed

    seed = {"start_stage": "storyboarder", "end_stage": "photographer",
            "enabled_checkpoints": ["images"]}
    out = _sanitize_seed(seed)
    assert out == seed, f"编排键被丢弃了：传入 {seed}，只留 {out}"


def test_whitelist_still_rejects_identity_and_dangerous_keys():
    """**安全不能因此被削弱** —— 身份键与会让服务端取远程内容的键仍要拒。"""
    from app.services.task_runner import _sanitize_seed

    out = _sanitize_seed({
        "run_context": {"user_id": "受害者"},       # 身份越权
        "images": [{"evil": 1}],                    # 可让服务端下载任意地址
        "character_reference_images": {"x": "http://evil"},
        "script": {"evil": 1},
        "start_stage": "storyboarder",              # 这个该留
    })
    assert "run_context" not in out, "身份键必须仍被拒绝（否则可越权）"
    assert "images" not in out
    assert "character_reference_images" not in out
    assert "script" not in out
    assert out.get("start_stage") == "storyboarder"


# ----------------------------------------------------------------------
# 2. 区间跳过（_skipped_by_scope）
# ----------------------------------------------------------------------

@pytest.mark.parametrize("state,stage,expect", [
    # end_stage：比它靠后的环节都跳过；它自己和更早的不跳过
    ({"end_stage": "storyboarder"}, "writer", False),
    ({"end_stage": "storyboarder"}, "storyboarder", False),
    ({"end_stage": "storyboarder"}, "previs_artist", True),
    ({"end_stage": "storyboarder"}, "photographer", True),
    # start_stage：比它靠前的环节都跳过；它自己和更后的不跳过
    ({"start_stage": "storyboarder"}, "writer", True),
    ({"start_stage": "storyboarder"}, "storyboarder", False),
    ({"start_stage": "storyboarder"}, "director", False),
    # 都没设 → 不跳过
    ({}, "writer", False),
    ({}, "photographer", False),
])
def test_scope_range(state, stage, expect):
    from app.workflows.main_workflow import _skipped_by_scope

    assert _skipped_by_scope(state, stage) is expect


def test_scope_ignores_unknown_stage_names():
    """不在 `_STAGE_ORDER` 里的名字**一律不跳过**（保守，避免误杀）。"""
    from app.workflows.main_workflow import _skipped_by_scope

    assert _skipped_by_scope({"end_stage": "storyboarder"}, "不存在的环节") is False


# ----------------------------------------------------------------------
# 3. 确认点开关（_checkpoint_on）—— 三态语义
# ----------------------------------------------------------------------

def test_checkpoints_default_all_on():
    """未设置 = 全开（向后兼容；也是 Web 的现状）。"""
    from app.workflows.main_workflow import _checkpoint_on

    assert _checkpoint_on({}, "images") is True
    assert _checkpoint_on({}, "quality") is True


def test_empty_checkpoint_list_means_all_off():
    """空列表 = 全关（用户明确"一个都不要停"）。"""
    from app.workflows.main_workflow import _checkpoint_on

    assert _checkpoint_on({"enabled_checkpoints": []}, "images") is False
    assert _checkpoint_on({"enabled_checkpoints": []}, "quality") is False


def test_checkpoint_list_selects_only_those():
    """非空列表 = 只开勾选的那些。"""
    from app.workflows.main_workflow import _checkpoint_on

    state = {"enabled_checkpoints": ["images", "script"]}
    assert _checkpoint_on(state, "images") is True
    assert _checkpoint_on(state, "script") is True
    assert _checkpoint_on(state, "quality") is False


# ----------------------------------------------------------------------
# 4. 生产路径：seed → 图初始 state（这条才是端到端的保证）
# ----------------------------------------------------------------------

def test_start_run_puts_orchestration_into_initial_state(monkeypatch):
    """`start_run(seed=…)` 必须把编排三键真的写进图初始 state。

    症状（修复前）：白名单丢键 → `_skipped_by_scope` 恒 False、
    `_checkpoint_on` 恒 True —— 设了"只勾图片检查"照样每步都停。
    """
    import app.services.task_runner as tr

    captured = {}

    def fake_run_stream(job_input, session_id, user_id, user_input, project="",
                        command=None):
        captured["state"] = job_input

    monkeypatch.setattr(tr, "_run_stream", fake_run_stream)

    seed = {"start_stage": "storyboarder", "end_stage": "photographer",
            "enabled_checkpoints": ["images"]}
    tr.start_run("probe-orch-1", "一句话", "tester", seed=seed)

    state = captured.get("state")
    assert state is not None, "没抓到初始 state"
    assert state.get("start_stage") == "storyboarder"
    assert state.get("end_stage") == "photographer"
    assert state.get("enabled_checkpoints") == ["images"]

    # 而且**真的影响判断**
    from app.workflows.main_workflow import _checkpoint_on, _skipped_by_scope

    assert _skipped_by_scope(state, "writer") is True       # start 之前 → 跳过
    assert _skipped_by_scope(state, "director") is False    # 在 photographer 之前
    assert _skipped_by_scope(state, "quality_assessor") is True   # end 之后 → 跳过
    assert _checkpoint_on(state, "images") is True
    assert _checkpoint_on(state, "quality") is False        # 没勾 → 不停


def test_sanitized_seed_reaches_state_through_start_run(monkeypatch):
    """经 `start_run` 的 seed 是**清洗过**的（敏感键不能进 state）。"""
    import app.services.task_runner as tr

    captured = {}

    def fake_run_stream(job_input, session_id, user_id, user_input, project="",
                        command=None):
        captured["state"] = job_input

    monkeypatch.setattr(tr, "_run_stream", fake_run_stream)
    tr.start_run("probe-orch-2", "一句话", "tester",
                 seed={"run_context": {"user_id": "受害者"},
                       "start_stage": "storyboarder"})

    state = captured["state"]
    assert state.get("start_stage") == "storyboarder"
    # run_context 由 task_runner 自己写（真实身份），**不能**来自 seed
    assert state.get("run_context", {}).get("user_id") != "受害者"
