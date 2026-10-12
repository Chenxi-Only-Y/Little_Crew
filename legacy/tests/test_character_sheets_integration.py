"""角色设定表**接进流程**后的行为（用户 2026-10-07 选定「16:9 单图」路线）。

## 路线与取舍（实测过两条，各有致命短板）

| 方案 | 配饰 | 一致性 |
|---|---|---|
| **16:9 单图**（本次采用） | ❌ 随机丢（一张图塞不下"大脸+三套精细服装"） | ✅ **同图生成，天然一致** |
| 4 格独立 + 拼接 | ✅ 全保住 | ❌ 四张独立采样，**脸会漂** |

用户选了「要一致性，接受配饰丢」。所以：

  · `character_sheets` —— 16:9 设定表，**给人看的展示交付物**
  · `character_reference_images` —— 逐视角独立图，**下游锚定仍用这个**

这两者**不能混**：CLIP 把整图编成**一个**向量，设定表拿去做锚定会导致
"画面被脸占据 / 生成多个副本 / 角度串味"。
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def mock_env(monkeypatch, tmp_path):
    """把产出与图像都指到临时目录 / mock，不碰真实数据。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "output_dir", str(tmp_path / "outputs"))
    monkeypatch.setattr(settings, "mock_image", True)
    monkeypatch.setattr(settings, "character_sheet_enabled", True)
    return settings


def _state(name="小墨"):
    from app.core.context import RunContext

    return {
        "session_id": "sess-sheet",
        "run_context": RunContext(user_id="tester", session_id="sess-sheet",
                                  project="P").as_dict(),
        "story_outline": {"characters": [{"name": name, "description": "一只猫"}]},
    }


def test_state_declares_character_sheets():
    """⚠️ 这是**最要紧**的一条。

    LangGraph 会**静默丢弃**未声明的 state 通道（`_algo.py` 里
    "wrote to unknown channel …, ignoring it"）。少声明一个字段
    = 节点产出凭空消失，而且**不报错**。项目里被这条坑过。
    """
    from app.core.state import CrewState

    assert "character_sheets" in CrewState.__annotations__


def test_aggregator_passes_character_sheets_through():
    """`aggregator` 有**结果白名单** —— 漏了它，设定表到不了前端。"""
    import inspect

    from app.agents import aggregator

    src = inspect.getsource(aggregator)
    assert '"character_sheets"' in src, "聚合器的白名单里没有 character_sheets"


def test_sheet_is_generated_when_enabled(mock_env):
    from app.agents.stage2.character_designer import CharacterDesigner
    from app.services import runtime_settings as rs

    rs.set_context("tester", "sess-sheet")
    try:
        out = CharacterDesigner().run(_state())
    finally:
        rs.set_context(None, None)

    assert "character_sheets" in out, "节点没返回 character_sheets"
    sheets = out["character_sheets"]
    assert isinstance(sheets, dict)
    assert "小墨" in sheets, f"没给角色生成设定表：{sheets}"
    assert sheets["小墨"], "设定表路径为空"


def test_sheet_can_be_disabled(mock_env):
    """关掉开关就不该生成（省时间/省钱）。"""
    mock_env.character_sheet_enabled = False

    from app.agents.stage2.character_designer import CharacterDesigner
    from app.services import runtime_settings as rs

    rs.set_context("tester", "sess-sheet")
    try:
        out = CharacterDesigner().run(_state())
    finally:
        rs.set_context(None, None)

    assert out["character_sheets"] == {}, "关掉了却还生成"


def test_downstream_reference_is_still_the_per_view_image(mock_env):
    """**核心**：设定表不能顶掉逐视角参考图。

    下游锚定必须继续用单视角独立图（CLIP 拿整张设定表会混特征）。
    """
    from app.agents.stage2.character_designer import CharacterDesigner
    from app.services import runtime_settings as rs

    rs.set_context("tester", "sess-sheet")
    try:
        out = CharacterDesigner().run(_state())
    finally:
        rs.set_context(None, None)

    refs = out["character_reference_images"]
    sheets = out["character_sheets"]
    assert refs.get("小墨"), "逐视角参考图没了"
    # 参考图指向的**不是**设定表
    assert refs["小墨"] not in set(sheets.values()), (
        "参考图指向了设定表 —— 下游会拿到混合特征")


def test_sheet_failure_does_not_break_the_node(mock_env, monkeypatch):
    """设定表是**附加交付物**：它挂了不能拖垮整个角色设计。"""
    from app.agents.stage2 import character_designer as cd
    from app.services import runtime_settings as rs

    class _Boom:
        def generate(self, *_a, **_k):
            raise RuntimeError("模拟出图失败")

    monkeypatch.setattr(cd, "get_image_service", lambda: _Boom())

    rs.set_context("tester", "sess-sheet")
    try:
        out = cd.CharacterDesigner().run(_state())
    finally:
        rs.set_context(None, None)

    assert out["character_sheets"] == {}, "失败时应返回空 dict，不是抛异常"
    assert "character_profiles" in out, "整个节点被拖垮了"


def test_sheet_size_settings_are_used(mock_env, monkeypatch):
    """尺寸走配置项（默认 1920×1080）。"""
    from app.core.config import settings

    assert settings.character_sheet_width == 1920
    assert settings.character_sheet_height == 1080
    assert settings.character_sheet_enabled is True


def test_settings_keys_are_settable():
    """⚠️ 又是"被读但写不进"那个坑。

    `character_designer` 运行时读这三个键；不在 `allowed_settings()` 里
    就会出现"设置页能改、保存却被静默丢弃"。
    （加它们时被 `test_runtime_read_keys_are_writable` 当场抓出。）
    """
    from app.registry.api import allowed_settings

    keys = allowed_settings()
    for k in ("character_sheet_enabled", "character_sheet_width",
              "character_sheet_height"):
        assert k in keys, f"{k} 写不进设置（PUT 会静默丢弃）"
