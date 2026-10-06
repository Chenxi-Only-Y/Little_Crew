"""注册表漂移守卫。

两类漂移都属于"**代码看着对、界面上却缺东西**"，很难靠人工发现：

1. **前端快照过期**：后端注册表改了（例如给 `video_provider` 加了
   `dashscope_video` / `seedance`），而 `web/src/registry.snapshot.json`
   没重新导出 → 界面上**依然选不到**，且所有检查都显示通过
   （`check:parity` 只比拓扑校验器，不看快照）。
2. **手写的提供者清单与真实入网清单不一致**：`video_provider` 原先手写
   `('(按模式)', 'local', 'minimax', 'ffmpeg')`，漏掉了两个已接好的适配器。

这里把两者都钉死：快照必须能重新导出并逐字节一致；provider 选项必须**派生**。
"""

import hashlib
import pathlib

import pytest

from app.registry import api as reg
from app.services import providers as prov

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SNAPSHOT = _ROOT / "web" / "src" / "registry.snapshot.json"


def _field_options(node_key: str, field_key: str) -> list[str]:
    for n in reg.snapshot()["nodes"]:
        if n["key"] != node_key:
            continue
        for f in n["config"]:
            if f["key"] == field_key:
                return list(f["options"])
    raise AssertionError(f"找不到 {node_key}.{field_key}")


# ------------------------------------------------------------------
# ① 快照时效性
# ------------------------------------------------------------------

def test_frontend_snapshot_matches_backend_registry():
    """前端快照必须与后端注册表一致（逐字节）。

    导出是确定性的（无时间戳等易变字段），所以直接比字符串。
    两边共用 `export_registry.snapshot_json()`，避免"各写一份序列化逻辑"
    导致守卫本身失真（那会让它一直通过、掩盖真正的漂移）。
    """
    from scripts.export_registry import snapshot_json

    fresh = snapshot_json()
    committed = _SNAPSHOT.read_text(encoding="utf-8")
    assert committed == fresh, (
        "前端注册表快照已过期：后端注册表变了、快照没跟上。\n"
        "这会让『后端支持但界面选不到』——静默割裂。\n"
        "修法：python scripts/export_registry.py"
    )


def test_snapshot_file_is_valid_json():
    import json

    assert _SNAPSHOT.exists(), f"缺少前端快照：{_SNAPSHOT}"
    json.loads(_SNAPSHOT.read_text(encoding="utf-8"))  # 解析失败即报错


# ------------------------------------------------------------------
# ② provider 选项必须派生，不能手写
# ------------------------------------------------------------------

@pytest.mark.parametrize("node_key,field_key,capability", [
    ("editor", "video_provider", "video_gen"),
    ("voice_director", "tts_provider", "tts"),
])
def test_provider_options_are_derived(node_key, field_key, capability):
    """provider 下拉的选项必须包含该能力**当前已登记的全部提供者**。

    原先手写清单会漂移：`video_provider` 漏了 `dashscope_video` 与 `seedance`
    （两者都已接好），用户根本选不到。
    """
    options = _field_options(node_key, field_key)
    expected = [p.key for p in prov.for_capability(capability, include_unavailable=True)]
    missing = [k for k in expected if k not in options]
    assert not missing, (
        f"{field_key} 的选项缺少已登记的提供者：{missing}\n"
        f"实际选项：{options}\n"
        f"（该字段应设 options_from=\"{capability}\" 由注册表派生，而不是手写清单）"
    )


def test_video_provider_lists_adapter_providers():
    """具体钉住这次修的问题：适配器里的视频提供者必须在界面上可选。"""
    from app.services.video_adapters import VIDEO_PROVIDERS

    options = _field_options("editor", "video_provider")
    for vp in VIDEO_PROVIDERS:
        assert vp.key in options, (
            f"适配器已支持 {vp.key!r}，但 video_provider 的选项里没有它：{options}"
        )


def test_placeholder_option_is_preserved():
    """`(按模式)` 这类"不指定"的占位项必须保留。

    它是"用上面选的模式默认"的语义；派生时若把它冲掉，
    用户就没法表达"按模式"了。
    """
    # 只有这两个节点有"带占位符的提供者字段"（`character_designer` 那个
    # `lora_train_provider` 已随「LoRA 训练」一起删除）。
    for node_key, field_key in (("editor", "video_provider"),
                                ("voice_director", "tts_provider")):
        options = _field_options(node_key, field_key)
        assert any(o.startswith("(") for o in options), (
            f"{field_key} 的选项里没有占位项（如 `(按模式)`）：{options}"
        )


# ------------------------------------------------------------------
# ③ "必选"提示必须与实际是否接入一致
# ------------------------------------------------------------------

def test_generation_settings_report_implemented_flag():
    """`/api/providers/modes` 必须告诉前端每个能力**是否已接入**。

    否则前端会把未接入的能力也标成"（必选）"并警告"不选会报错"
    ——而它们在代码里**没有任何调用点**，不选也不会报错。
    误导性的必选提示比不提示更糟：用户会去填一堆无用的配置。
    """
    rows = prov.generation_settings()
    by_cap = {r["capability"]: r for r in rows}
    assert all("implemented" in r for r in rows), "缺少 implemented 字段"

    for cap in ("llm", "image_gen", "video_gen"):
        assert by_cap[cap]["implemented"] is True, f"{cap} 是已接入的，应为 True"
    for cap in ("tts",):
        assert by_cap[cap]["implemented"] is False, (
            f"{cap} 当前无调用点，implemented 应为 False（否则前端会误导用户）"
        )
