"""单例配置跟随 + 跨线程上下文 的回归测试。

对应 `docs/recon/08-solutions.md §2` 与 `01-usage.md §6.2`。

覆盖两个已实测确认的缺陷：
  1. 服务类在 `__init__` 里快照 `self.mock`，导致运行中改配置不生效
     （"设置页保存立即生效"是假的）
  2. `threading.local` 不跨线程池继承，导致并发出图时产物落到
     `_global/no-session-<shot_id>/`
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core.config import settings
from app.services import providers as prov
from app.services import runtime_settings as rs


@pytest.fixture(autouse=True)
def _clean_state():
    """每个用例前后清理覆盖与上下文，避免相互污染。"""
    prov.clear_overrides()
    rs.set_context(None, None)
    yield
    prov.clear_overrides()
    rs.set_context(None, None)


# ------------------------------------------------------------------
# 1) 配置不再被快照
# ------------------------------------------------------------------

def test_is_mock_follows_config_change():
    """改 settings 后，`is_mock` 必须立即跟随（不做快照）。"""
    old = settings.mock_image
    try:
        settings.mock_image = True
        assert prov.is_mock("image") is True
        settings.mock_image = False
        assert prov.is_mock("image") is False, "配置改动未生效——存在快照"
    finally:
        settings.mock_image = old


def test_is_mock_runtime_setting_wins():
    """运行时设置（设置页保存的）优先于 config 默认。"""
    old = settings.mock_image
    try:
        settings.mock_image = True
        rs.set_many("_global", {"mock_image": False})
        rs.set_user("_global")
        assert prov.is_mock("image") is False, "运行时设置未覆盖 config 默认"
    finally:
        rs.reset("_global")
        settings.mock_image = old


def test_is_mock_explicit_override_wins():
    """显式覆盖优先于一切（供测试注入）。"""
    old = settings.mock_image
    try:
        settings.mock_image = True
        prov.set_override("image", False)
        assert prov.is_mock("image") is False
        prov.set_override("image", None)
        assert prov.is_mock("image") is True
    finally:
        settings.mock_image = old


@pytest.mark.parametrize("capability,service_factory,attr", [
    ("llm", "app.services.llm:get_llm", "mock"),
    ("image", "app.services.image:get_image_service", "mock"),
    ("video", "app.services.video:get_video_service", "mock"),
    ("quality", "app.services.quality:get_quality_service", "mock"),
])
def test_service_mock_is_not_frozen(capability, service_factory, attr):
    """服务实例的 `.mock` 必须实时反映配置，而不是创建时的快照。"""
    import importlib

    mod_name, fn_name = service_factory.split(":")
    svc = getattr(importlib.import_module(mod_name), fn_name)()
    setting_key = {"llm": "mock_llm", "image": "mock_image",
                   "video": "mock_video", "quality": "mock_quality"}[capability]
    old = getattr(settings, setting_key)
    try:
        setattr(settings, setting_key, True)
        assert getattr(svc, attr) is True
        setattr(settings, setting_key, False)
        assert getattr(svc, attr) is False, f"{capability} 服务的 mock 被快照了"
    finally:
        setattr(settings, setting_key, old)


# ------------------------------------------------------------------
# 2) 上下文跨线程传递
# ------------------------------------------------------------------

def test_capture_restore_roundtrip():
    rs.set_context("alice", "sess-1")
    ctx = rs.capture()
    rs.set_context("bob", "sess-2")
    assert rs.user() == "bob"
    rs.restore(ctx)
    assert rs.user() == "alice"
    assert rs.run_session() == "sess-1"


def test_restore_none_is_noop():
    rs.set_context("alice", "sess-1")
    rs.restore(None)
    assert rs.user() == "alice"


def test_threadlocal_is_lost_without_restore():
    """记录事实：不显式恢复时，线程池里上下文确实会丢失。

    这条测试是"为什么需要 capture/restore"的活文档；若将来 Python 或
    实现改为自动继承，它会失败，那时就可以删掉 restore 逻辑。
    """
    rs.set_context("alice", "sess-1")

    def worker():
        return rs.user(), rs.run_session()

    with ThreadPoolExecutor(max_workers=1) as pool:
        user, sess = pool.submit(worker).result()
    assert user != "alice", "上下文意外被继承——可考虑移除 restore 逻辑"


def test_restore_inside_thread_pool_works():
    """显式恢复后，worker 内能读到正确上下文（产物归档依赖它）。"""
    rs.set_context("alice", "sess-1")
    ctx = rs.capture()

    def worker():
        rs.restore(ctx)
        return rs.user(), rs.run_session()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(worker).result() for _ in range(4)]
    for user, sess in results:
        assert user == "alice"
        assert sess == "sess-1"


# ------------------------------------------------------------------
# 3) 产物归档路径（端到端，最贴近 §6.2 的现象）
# ------------------------------------------------------------------

def test_images_archived_under_session(tmp_path, monkeypatch):
    """并发路径下，产物必须归档到 `<user>/<部门>/<职业>/<session>/`。

    对应 01-usage §6.2：此前会落到 `_global/.../no-session-<shot_id>/`。
    """
    import shutil

    from app.workflows.main_workflow import run_once

    out = tmp_path / "outputs"
    monkeypatch.setattr(settings, "output_dir", str(out))
    monkeypatch.setattr(settings, "mock_image", True)
    monkeypatch.setattr(settings, "mock_llm", True)
    monkeypatch.setattr(settings, "mock_quality", True)
    monkeypatch.setattr(settings, "mock_critics", True)
    monkeypatch.setattr(settings, "image_provider", "dashscope")  # 云端 → 并发>1
    # 必须关掉 HITL：否则流程停在第一个确认点（interrupt），根本走不到出图
    monkeypatch.setattr(settings, "hitl", False)

    rs.set_context("tester", "sess-arch")
    try:
        res = run_once({"user_input": "归档路径测试", "session_id": "sess-arch"})
    finally:
        rs.set_context(None, None)

    images = (res.get("result") or {}).get("images") or []
    assert images, "没有生成图片，无法验证归档"

    pngs = list(out.rglob("*.png"))
    assert pngs, "产物未落盘"
    bad = [p for p in pngs if "sess-arch" not in p.as_posix()]
    assert not bad, f"以下产物未按会话归档（线程池上下文丢失）：{[p.name for p in bad]}"
    assert all("tester" in p.as_posix() for p in pngs), "未按用户归档"
    shutil.rmtree(out, ignore_errors=True)


# ------------------------------------------------------------------
# 4) 显式 RunContext（随 State 传递）
# ------------------------------------------------------------------

def test_run_context_dict_roundtrip():
    from app.core.context import RunContext

    ctx = RunContext(user_id="alice", session_id="s1", role="photographer")
    assert RunContext.from_dict(ctx.as_dict()) == ctx


def test_run_context_from_state_variants():
    from app.core.context import RunContext

    ctx = RunContext(user_id="a", session_id="s", role="r")
    assert RunContext.from_state({"run_context": ctx.as_dict()}) == ctx
    # 没有 run_context 时用 session_id 兜底
    assert RunContext.from_state({"session_id": "only"}) == RunContext(session_id="only")
    assert RunContext.from_state(None) == RunContext()
    assert RunContext.from_state({}) == RunContext()


def test_bind_sets_and_restores():
    from app.core.context import RunContext, bind

    rs.set_context("outer", "sess-outer")
    with bind(RunContext(user_id="alice", session_id="s1")):
        assert rs.user() == "alice"
        assert rs.run_session() == "s1"
    assert rs.user() == "outer", "bind 退出后未恢复原上下文"
    assert rs.run_session() == "sess-outer"


def test_bind_none_is_noop():
    from app.core.context import bind

    rs.set_context("outer", "sess-outer")
    with bind(None):
        pass
    assert rs.user() == "outer"


def test_run_context_from_state_drives_archiving(tmp_path, monkeypatch):
    """核心：**不调用 set_context**，仅靠 State 里的 run_context 完成归档。

    这是"显式上下文替代 thread-local"的验收标准。
    """
    import shutil

    from app.core.context import RunContext
    from app.workflows.main_workflow import run_once

    out = tmp_path / "outputs"
    monkeypatch.setattr(settings, "output_dir", str(out))
    monkeypatch.setattr(settings, "mock_image", True)
    monkeypatch.setattr(settings, "mock_llm", True)
    monkeypatch.setattr(settings, "mock_quality", True)
    monkeypatch.setattr(settings, "mock_critics", True)
    monkeypatch.setattr(settings, "image_provider", "dashscope")
    monkeypatch.setattr(settings, "hitl", False)

    rs.set_context(None, None)          # 关键：外部不设上下文
    res = run_once({
        "user_input": "显式上下文归档",
        "session_id": "sess-ctx-e2e",
        "run_context": RunContext(user_id="bob", session_id="sess-ctx-e2e").as_dict(),
    })

    images = (res.get("result") or {}).get("images") or []
    assert images, "没有生成图片"

    pngs = list(out.rglob("*.png"))
    assert pngs
    bad = [p for p in pngs if not (p.as_posix().find("bob") >= 0 and "sess-ctx-e2e" in p.as_posix())]
    assert not bad, f"未按 State 中的 run_context 归档：{[p.name for p in bad]}"
    shutil.rmtree(out, ignore_errors=True)


# ------------------------------------------------------------------
# 5) 运行时设置的写入必须是**合并**，不能整体替换
# ------------------------------------------------------------------

def test_set_many_merges_instead_of_replacing():
    """保存单个键**不得**清掉该用户已有的其它设置。

    这是一个**实际发生过的数据丢失**：属性面板只保存一个字段
    （如职业级 `image_provider_photographer`）时，整体替换会清空其余全部设置，
    于是前面存好的"云端/本地模式"全没了——表现为运行时报"未选择提供者"。
    """
    uid = "merge_test_user"
    rs.reset(uid)
    try:
        rs.set_many(uid, {"image_provider_mode": "local", "video_provider_mode": "cloud"})
        # 再单独保存一个字段
        rs.set_many(uid, {"image_provider_photographer": "dashscope"})

        snap = rs.snapshot(uid)
        assert snap.get("image_provider_mode") == "local", "已有设置被后续单字段写入清掉了"
        assert snap.get("video_provider_mode") == "cloud"
        assert snap.get("image_provider_photographer") == "dashscope"
    finally:
        rs.reset(uid)


def test_set_many_can_replace_explicitly():
    """显式 merge=False 时仍是整体替换（保留该能力以备需要）。"""
    uid = "replace_test_user"
    rs.reset(uid)
    try:
        rs.set_many(uid, {"a": "1", "b": "2"})
        rs.set_many(uid, {"c": "3"}, merge=False)
        snap = rs.snapshot(uid)
        assert snap.get("c") == "3"
        assert "a" not in snap and "b" not in snap, "merge=False 应整体替换"
    finally:
        rs.reset(uid)


def test_empty_string_clears_override():
    """空串 = 清除该覆盖（让解析回退到模式/默认）。"""
    uid = "clear_test_user"
    rs.reset(uid)
    try:
        rs.set_many(uid, {"image_provider_photographer": "dashscope"})
        assert rs.snapshot(uid).get("image_provider_photographer") == "dashscope"
        rs.set_many(uid, {"image_provider_photographer": ""})
        assert not rs.snapshot(uid).get("image_provider_photographer"), "空串应清除覆盖"
    finally:
        rs.reset(uid)
