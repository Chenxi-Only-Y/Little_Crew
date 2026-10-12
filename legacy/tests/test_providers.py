"""Provider 注册表回归测试。

守住的规矩（来自 `docs/recon/07-risks-predictions.md §2` / `08-solutions.md §1`）：
  1. provider 清单是唯一数据源，且覆盖注册表声明的所有能力
  2. **本地 provider 的并发度必须是 1**（否则与服务端串行锁打架）
  3. 并发度解析取 min(期望值, provider 上限)
  4. 已登记未接入的 provider 不出现在可选清单里
"""

from __future__ import annotations

import pytest

from app.registry import api as registry
from app.services import providers as prov


# ------------------------------------------------------------------
# 与注册表的对应关系
# ------------------------------------------------------------------

def test_every_registry_capability_has_provider():
    """注册表里节点声明的每个能力，都必须至少有一个可用 provider。

    否则画布上会出现"有这个能力但没得选"的节点。
    """
    declared: set[str] = set()
    for node in registry.all_nodes():
        declared.update(node.capability)
    # 这些能力允许暂时没有 provider（见 providers.py 的 available=False 登记）
    optional = {"music"}
    missing = []
    for cap in declared - optional:
        if not prov.for_capability(cap):
            missing.append(cap)
    assert not missing, f"以下能力没有可用 provider：{missing}"


def test_provider_capabilities_are_known():
    known = {"llm", "image_gen", "image_edit", "video_gen", "tts", "music", "render_3d"}
    for p in prov.all_specs():
        assert p.capability in known, f"{p.key} 的能力 {p.capability} 不在已知集合内"


def test_provider_keys_unique_per_capability():
    seen = set()
    for p in prov.all_specs():
        k = (p.capability, p.key)
        assert k not in seen, f"能力 {p.capability} 下 provider {p.key} 重复"
        seen.add(k)


# ------------------------------------------------------------------
# 并发度（本地必须是 1）
# ------------------------------------------------------------------

def test_local_generative_providers_are_serial():
    """本地**生成类**（图像/视频/训练）provider 并发度必须为 1。

    本地推理服务端（local-gen）有一把全局串行锁，调用方并发只会堆积请求，
    还会让进度显示与事实不符。

    注意：本地 LLM（ollama/vllm）**不在此列**——它们是普通推理服务，能处理并发；
    "必须串行"这个约束来自 8GB 显存 + Disk Offload 的图像/视频模型，不是"本地"本身。
    """
    serial_caps = {"image_gen", "image_edit", "video_gen", "render_3d"}
    for p in prov.all_specs():
        if p.category == "local" and p.capability in serial_caps:
            assert p.max_concurrency == 1, (
                f"本地 provider {p.key}（{p.capability}）声明了并发 {p.max_concurrency}，应为 1"
            )


def test_local_llm_may_be_parallel():
    """本地 LLM 允许并发（不应被图像侧的串行约束误伤）。"""
    for key in ("ollama", "vllm"):
        spec = prov.get("llm", key)
        assert spec is not None, f"缺少本地 LLM provider：{key}"
        assert spec.max_concurrency >= 1


def test_cloud_providers_allow_parallel():
    for p in prov.all_specs():
        if p.category in ("cn_cloud", "overseas_cloud") and p.capability == "image_gen":
            assert p.max_concurrency > 1, f"云端 {p.key} 不应被限制为串行"


@pytest.mark.parametrize("capability,provider,expected", [
    ("image_gen", "local", 1),
    ("image_gen", "dashscope", 4),
    ("image_gen", "minimax", 4),
    ("video_gen", "local", 1),
])
def test_max_concurrency(capability, provider, expected):
    assert prov.max_concurrency(capability, provider) == expected


def test_max_concurrency_clamps_desired():
    """期望值高于 provider 上限时被夹住；低于上限时保留。"""
    assert prov.max_concurrency("image_gen", "local", 8) == 1
    assert prov.max_concurrency("image_gen", "dashscope", 2) == 2
    assert prov.max_concurrency("image_gen", "dashscope", 99) == 4


def test_max_concurrency_unknown_provider_is_conservative():
    """未知 provider 保守取 1（宁可串行，也不要打爆）。"""
    assert prov.max_concurrency("image_gen", "不存在的provider") == 1


# ------------------------------------------------------------------
# 可用性
# ------------------------------------------------------------------

def test_unavailable_excluded_by_default():
    available = {p.key for p in prov.for_capability("image_gen")}
    all_keys = {p.key for p in prov.for_capability("image_gen", include_unavailable=True)}
    assert "wavespeed" in all_keys
    assert "wavespeed" not in available


def test_available_flag_consistent():
    for cap in {p.capability for p in prov.all_specs()}:
        for p in prov.for_capability(cap):
            assert p.available, f"{p.key} 出现在可用清单里但 available=False"


def test_local_needs_no_api_key():
    """所有 local/free 类别的 provider 都不应要求 API Key。"""
    for p in prov.all_specs():
        if p.category in ("local", "free"):
            assert not p.needs_api_key, f"{p.key} 是 {p.category} 却要求 API Key"


def test_overseas_marked_needs_proxy():
    for p in prov.all_specs():
        if p.category == "overseas_cloud":
            assert p.needs_proxy, f"海外 provider {p.key} 未标记 needs_proxy"


# ------------------------------------------------------------------
# 下发载荷
# ------------------------------------------------------------------

def test_to_payload_serializable():
    import json

    payload = prov.to_payload()
    assert len(payload) == len(prov.all_specs())
    s = json.dumps(payload, ensure_ascii=False)
    assert len(s) > 500
    for item in payload:
        for key in ("key", "label", "capability", "category", "available",
                    "default_model", "known_models", "needs_api_key",
                    "needs_proxy", "max_concurrency", "note"):
            assert key in item


def test_local_image_provider_declares_known_models():
    spec = prov.get("image_gen", "local")
    assert spec is not None
    assert "z-image-turbo" in spec.known_models


# ------------------------------------------------------------------
# 提供者解析：显式模式，不做静默默认
# ------------------------------------------------------------------

@pytest.fixture
def clean_settings():
    """清掉运行时覆盖与 config 上的 provider，保证从"什么都未选"开始。"""
    from app.core.config import settings
    from app.services import runtime_settings as rs

    saved = {k: getattr(settings, k) for k in
             ("image_provider", "video_provider", "llm_provider")}
    rs.reset("_global")
    rs.set_user("_global")
    for k in saved:
        setattr(settings, k, "")
    yield rs
    rs.reset("_global")
    for k, v in saved.items():
        setattr(settings, k, v)


def test_unset_provider_returns_none_not_a_guess(clean_settings):
    """**什么都不选时必须返回 None**，而不是猜一个默认值。

    这是用户明确要求的：不要默认本地、也不要默认云端——
    默认本地会在没部署时直接失败，默认云端会在用户不知情时产生费用。
    """
    p, why = prov.resolve_provider("image_gen")
    assert p is None, f"未选择时不应猜出 {p}"
    assert "云端" in why and "本地" in why, f"提示应引导用户去选，实际：{why}"

    p2, why2 = prov.resolve_provider("video_gen")
    assert p2 is None
    assert "云端" in why2


def test_mode_cloud_picks_cloud_default(clean_settings):
    clean_settings.set_many("_global", {"image_provider_mode": "cloud"})
    p, why = prov.resolve_provider("image_gen")
    assert p == "dashscope", f"云端模式应取 dashscope，实际 {p}"
    assert "云端" in why


def test_mode_local_picks_local_default(clean_settings):
    """每个能力有**独立的模式键**（用户可分别选：图像本地、视频云端）。"""
    clean_settings.set_many("_global", {"image_provider_mode": "local"})
    p, _ = prov.resolve_provider("image_gen")
    assert p == "local"
    # 视频没设模式 → 仍应是"未选"，不应被图像的模式带着走
    p2, why2 = prov.resolve_provider("video_gen")
    assert p2 is None, f"视频未选模式却被解析成 {p2}"
    assert "video_provider_mode" in why2

    clean_settings.set_many("_global", {"video_provider_mode": "local"})
    p3, _ = prov.resolve_provider("video_gen")
    assert p3 == "local"

    clean_settings.set_many("_global", {"llm_provider_mode": "local"})
    p4, _ = prov.resolve_provider("llm")
    assert p4 == "ollama"


def test_mode_conflict_is_rejected(clean_settings):
    """模式与提供者矛盾时**明确报错**，而不是按提供者静默跑。

    这条测试原名 `test_explicit_provider_beats_mode`，锁的是"显式提供者
    压过模式"的旧行为。2026-10-05 用户报了那个行为的后果：

        "把下滑时的本地模型和云端模型选项分开，不要杂在一起，
         不然会出现上面选的本地，然后下面填 api 情况"

    —— 模式选「本地」、提供者却是云端（`minimax`），于是界面引导用户
    去填 API Key，组合自相矛盾。

    现在语义改为：`mode` 明确为 local/cloud 时，提供者必须与之一致；
    矛盾则返回 `None` 并说明怎么改。**最糟的不是报错，是静默按另一条路跑**
    （用户以为在跑本地零成本、实际发了云端请求花钱）。
    """
    clean_settings.set_many("_global", {"image_provider_mode": "local",
                                        "image_provider": "minimax"})
    p, why = prov.resolve_provider("image_gen")
    assert p is None, f"矛盾配置应拒绝，实际返回 {p}"
    # 文案用**友好名**（MiniMax）而非内部 key（minimax）——给用户看的
    assert "矛盾" in why and "MiniMax" in why, why
    # 报错文案要告诉用户怎么改
    assert "本地推理" in why or "云端 API" in why, why


def test_placeholder_follows_mode_instead_of_being_a_provider(clean_settings):
    """占位符 `(自动)` / `(按模式)` 表示"不覆盖、跟随模式"，**不是提供者**。

    这是一个**存在已久的静默 bug**：`resolve_provider` 原先写 `if v:`
    判断"有没有设过覆盖"，而 `"(自动)"` 是**非空字符串**，于是它被当成
    一个真提供者名返回。实测：

        没设覆盖   → ('deepseek', '来自「云端 API」模式')      ✅
        设了 (自动) → ('(自动)',  '来自「writer」的职业级设置') ❌

    也就是「(自动)」这个选项一直是坏的 —— 界面看着正常，
    真跑起来会拿 `(自动)` 当 provider 去调，必然失败。
    """
    clean_settings.set_many("_global", {"llm_provider_mode": "cloud"})
    base, _ = prov.resolve_provider("llm", "writer")

    for ph in ("(自动)", "(按模式)"):
        clean_settings.set_many("_global", {"llm_provider_writer": ph})
        got, why = prov.resolve_provider("llm", "writer")
        assert got == base, f"{ph} 应跟随模式得到 {base!r}，实际 {got!r}（{why}）"
        assert not prov.is_placeholder(got)

    # 视频的占位符同样
    clean_settings.set_many("_global", {"video_provider_mode": "local",
                                        "video_provider": "(按模式)"})
    got, _ = prov.resolve_provider("video_gen")
    assert got == "local", f"(按模式) 应跟随本地模式，实际 {got!r}"

    # 但**真提供者**仍然压过模式
    clean_settings.set_many("_global", {"llm_provider_mode": "cloud",
                                        "llm_provider_writer": "ollama"})
    got, why = prov.resolve_provider("llm", "writer")
    assert got is None, "模式=cloud 而显式选本地提供者 → 应报配置矛盾"
    assert "矛盾" in why


def test_is_placeholder_only_matches_placeholders():
    assert prov.is_placeholder("(自动)")
    assert prov.is_placeholder("(按模式)")
    assert prov.is_placeholder("  (自动)  ")  # 容忍空白
    assert not prov.is_placeholder("deepseek")
    assert not prov.is_placeholder("")
    assert not prov.is_placeholder(None)


def test_consistent_mode_and_provider_still_resolves(clean_settings):
    """模式与提供者一致时照常解析（别把正常路径也挡了）。"""
    clean_settings.set_many("_global", {"image_provider_mode": "cloud",
                                        "image_provider": "minimax"})
    p, why = prov.resolve_provider("image_gen")
    assert p == "minimax", f"云端模式 + 云端提供者应通过，实际 {p}"
    assert "全局设置" in why

    clean_settings.set_many("_global", {"image_provider_mode": "local",
                                        "image_provider": "local"})
    p2, _ = prov.resolve_provider("image_gen")
    assert p2 == "local", f"本地模式 + 本地提供者应通过，实际 {p2}"


def test_unset_mode_does_not_constrain_provider(clean_settings):
    """模式未选时不校验（用户没表达意图，以显式提供者为准）。"""
    clean_settings.set_many("_global", {"image_provider_mode": "unset",
                                        "image_provider": "minimax"})
    p, _ = prov.resolve_provider("image_gen")
    assert p == "minimax"


def test_providers_for_mode_splits_local_and_cloud(clean_settings):
    """按模式筛选提供者：local 只给本地/零成本、cloud 只给云端。"""
    local = {s.key for s in prov.providers_for_mode("llm", "local")}
    cloud = {s.key for s in prov.providers_for_mode("llm", "cloud")}
    allp = {s.key for s in prov.providers_for_mode("llm", "unset")}

    assert local == {"ollama", "vllm"}, local
    assert not (local & cloud), f"本地与云端不该有交集：{local & cloud}"
    assert allp == local | cloud, "unset 应给出全部"
    # 云端那侧要含几家已知的云端提供者
    assert {"deepseek", "openai", "openrouter"} <= cloud, cloud
    # 视频的 ffmpeg 与 tts 的 edge 都算"本地/零成本"
    assert "ffmpeg" in {s.key for s in prov.providers_for_mode("video_gen", "local")}
    assert "edge" in {s.key for s in prov.providers_for_mode("tts", "local")}


def test_role_provider_beats_global(clean_settings):
    """职业级覆盖优先于全局。"""
    clean_settings.set_many("_global", {"image_provider": "minimax",
                                        "image_provider_photographer": "local"})
    p, why = prov.resolve_provider("image_gen", "photographer")
    assert p == "local"
    assert "photographer" in why
    # 其它职业仍按全局
    p2, _ = prov.resolve_provider("image_gen", "character_designer")
    assert p2 == "minimax"


def test_mode_options_cover_all_capabilities(clean_settings):
    """每个需要选模式的能力都要能查到 cloud/local 两种默认。"""
    for cap in ("llm", "image_gen", "video_gen", "tts"):
        assert prov.mode_default_provider(cap, "cloud"), f"{cap} 缺云端默认"
        assert prov.mode_default_provider(cap, "local"), f"{cap} 缺本地默认"


def test_generation_settings_payload(clean_settings):
    items = prov.generation_settings()
    caps = {i["capability"] for i in items}
    assert {"llm", "image_gen", "video_gen"} <= caps
    for i in items:
        assert i["mode_key"] and i["provider_key"]
        assert "unset" in i["mode_options"]


def test_no_silent_default_in_config(clean_settings):
    """config 里不应再写死 provider（否则又会变成静默默认）。"""
    from app.core.config import Settings

    fresh = Settings(_env_file=None)
    assert fresh.image_provider == "", f"image_provider 不应有默认值：{fresh.image_provider}"
    assert fresh.video_provider == "", f"video_provider 不应有默认值：{fresh.video_provider}"
    assert fresh.llm_provider == "", f"llm_provider 不应有默认值：{fresh.llm_provider}"


def test_settings_whitelist_covers_registry_config_keys():
    """`put_settings` 的白名单必须包含注册表里的所有配置键。

    否则界面上能改的设置会被后端**静默丢弃**（已实际发生过：
    tts_provider / lora_train_provider / image_provider_image_edit 都曾不在白名单里）。
    """
    from app.registry import api as registry

    allowed = registry.allowed_settings()
    missing = [
        c.key
        for n in registry.all_nodes()
        for c in n.config
        if c.key not in allowed
    ]
    assert not missing, f"以下注册表配置键不在写入白名单里，会被静默丢弃：{missing}"


def test_settings_whitelist_covers_runtime_role_keys():
    """白名单还要覆盖 Agent **运行时真正读取**的 role 键。"""
    from app.registry import api as registry

    allowed = registry.allowed_settings()
    # 这些是各 Agent 源码里实际传的 role（见 app/agents/*）
    runtime_roles = ["writer", "critics", "screenwriter", "storyboarder",
                     "director", "photographer", "continuity",
                     "character_designer", "scene_designer"]
    missing = []
    for r in runtime_roles:
        for pfx in ("llm_provider", "image_provider"):
            k = f"{pfx}_{r}"
            if k not in allowed:
                missing.append(k)
    assert not missing, f"以下运行时 role 键不在白名单里：{missing}"


def test_settings_whitelist_covers_mode_keys():
    """所有"云端/本地"模式键也必须可写，否则用户选了保存不下来。"""
    from app.registry import api as registry

    allowed = registry.allowed_settings()
    missing = [k for k in prov.MODE_KEY_BY_CAPABILITY.values() if k not in allowed]
    assert not missing, f"以下模式键不在写入白名单里，用户选了会被丢弃：{missing}"


def test_resolver_mode_key_matches_registry_field():
    """解析器用的模式键必须与注册表里实际下发的字段键一致。

    这两处分别定义在 providers.py 与 roles.py，最容易各写各的——
    一旦不一致，用户在下拉里选的值会被保存到一个**运行时不读**的键上。
    """
    from app.registry.roles import MODEL_MODE_FIELDS

    field_keys = {f.key for f in MODEL_MODE_FIELDS.values()}
    resolver_keys = set(prov.MODE_KEY_BY_CAPABILITY.values())
    # image_edit 与 image_gen 共用同一个模式键，属预期
    missing = resolver_keys - field_keys
    assert not missing, (
        f"解析器读取的模式键 {missing} 在注册表字段里不存在，"
        f"用户在界面上的选择将不会被运行读取"
    )


# ------------------------------------------------------------------
# 按职业的 provider 覆盖必须生效
# ------------------------------------------------------------------

def test_per_role_image_provider_affects_concurrency():
    """`image_provider_photographer` 覆盖必须影响并发度。

    此前 `ImageGenerator._concurrency()` 依赖线程局部的 role，而该 role 只在
    `ImageService.generate()` 内部才被设置，导致按职业的 provider 配置被静默忽略。
    """
    from app.agents.image_generator import ImageGenerator
    from app.core.config import settings
    from app.services import runtime_settings as rs

    old_mock, old_provider = settings.mock_image, settings.image_provider
    rs.reset("_global")
    rs.set_user("_global")
    try:
        settings.mock_image = True
        settings.image_provider = "dashscope"
        assert ImageGenerator()._concurrency() == 4

        rs.set_many("_global", {"image_provider_photographer": "local"})
        assert ImageGenerator()._concurrency() == 1, "职业级 local 覆盖未生效"

        rs.set_many("_global", {"image_provider_photographer": "dashscope"})
        assert ImageGenerator()._concurrency() == 4
    finally:
        rs.reset("_global")
        settings.mock_image, settings.image_provider = old_mock, old_provider
