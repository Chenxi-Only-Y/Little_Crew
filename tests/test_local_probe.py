"""本机能力探测（`local_probe`）的测试。

## 这个模块最容易出的错

不是"探测不准"，而是**误判成可用** —— 那样界面会列出一个选了必然失败的
选项，而这正是要消灭的问题。所以测试集中在**保守性**上：

  · 端口有人占但**不是那个服务** → 必须判不可用
    （最典型的反例：vLLM 默认端口 8000 与本项目后端撞车）
  · 响应不是 JSON / 结构不对 → 必须判不可用
  · 探测本身抛异常 → 必须判不可用（不许把异常抛给调用方）
  · 没有探测方式的本地提供者 → 必须判不可用（证明不了就别列）

另外守住"哪些提供者不需要探测"的白名单 —— 漏一个会让它永远不显示，
多一个会让一个没验证过的东西永远显示。
"""

from __future__ import annotations

import pytest

from app.services import local_probe as lp


@pytest.fixture(autouse=True)
def _clean_cache():
    """每个用例都从**没有缓存**开始，免得上一个用例的结果串味。"""
    lp._cache.clear()
    lp._cache_at = 0.0
    yield
    lp._cache.clear()
    lp._cache_at = 0.0


# ------------------------------------------------------------------
# 云端 vs 本地
# ------------------------------------------------------------------

def test_cloud_providers_are_always_ready():
    """云端恒为 True：能不能用取决于有没有 Key，那不是"本机能力"。"""
    for cap, key in (("llm", "deepseek"), ("image_gen", "dashscope"),
                     ("video_gen", "seedance"), ("tts", "elevenlabs")):
        assert lp.is_ready(cap, key, category="cn_cloud") is True
        assert lp.is_ready(cap, key, category="overseas_cloud") is True


def test_local_provider_without_probe_is_not_ready(monkeypatch):
    """**没有探测方式**的本地提供者判不可用（证明不了就别列）。

    这是刻意的保守：少列一个能用的只是让用户手动找，
    列一个用不了的会让他配半天然后失败。
    """
    monkeypatch.setattr(lp, "_cache", {"llm:ollama": True})
    monkeypatch.setattr(lp, "_cache_at", 1e18)   # 永不过期
    # `local` 在 image_gen 下有探测；而一个**虚构**的 key 没有
    assert lp.is_ready("image_gen", "完全虚构的提供者", category="local") is False


def test_render_3d_needs_no_probe():
    """`render_3d` 的渲染在**前端**（Three.js），不该被探测挡住。

    它没有外部服务或二进制依赖（体块场景由浏览器渲染，见 `web/src/previs/`），
    把它也走探测会让一个**本来就能用**的功能在界面上消失。

    （这条测试原先写"是纯 Python（PIL）" —— 那是过时的描述：
     3D 预演做出来后，后端已无 PIL 绘制白模的代码。）
    """
    assert ("render_3d", "local") in lp.NO_PROBE_NEEDED
    assert lp.is_ready("render_3d", "local", category="local") is True


def test_every_probe_key_is_a_real_local_provider():
    """探测表的键必须都是注册表里**真实存在**的本地/免费提供者。

    写错 key（比如能力名拼错）会让那个提供者永远显示"不可用"，
    而代码不会报任何错 —— 很难发现。
    """
    from app.services.providers import PROVIDERS

    real = {(p.capability, p.key) for p in PROVIDERS if p.category in ("local", "free")}
    unknown = set(lp.PROBES) - real
    assert not unknown, f"探测表里有注册表中不存在的键：{unknown}"

    # 反过来：每个本地/免费提供者要么有探测、要么在白名单里，
    # 否则它会永远不显示（也是静默的）
    uncovered = real - set(lp.PROBES) - lp.NO_PROBE_NEEDED
    assert not uncovered, (
        f"这些本地提供者既没有探测函数、也不在 NO_PROBE_NEEDED 白名单里，"
        f"会永远不显示：{uncovered}"
    )


# ------------------------------------------------------------------
# 保守性：宁可漏判，不要误判
# ------------------------------------------------------------------

def test_http_probe_rejects_wrong_service(monkeypatch):
    """端口有人占但响应**不是那个服务** → 必须判不可用。

    最典型的反例：vLLM 默认端口 **8000** 与本项目后端撞车。只看
    "端口有人占"会把我们自己的后端当成 vLLM，界面上多出一个
    选了必然失败的选项。
    """
    import json as _json
    import io as _io

    class Resp(_io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    # 我们自己后端在 /v1/models 上不会返回 OpenAI 的结构
    def fake_urlopen(req, timeout=None):
        return Resp(b'{"detail":"Not Found"}')

    monkeypatch.setattr(lp.urllib.request, "urlopen", fake_urlopen)
    assert lp._probe_vllm() is False, "响应结构不对时不该判可用"

    # 真的 OpenAI 结构才算
    def fake_ok(req, timeout=None):
        return Resp(_json.dumps({"object": "list", "data": [{"id": "x"}]}).encode())

    monkeypatch.setattr(lp.urllib.request, "urlopen", fake_ok)
    assert lp._probe_vllm() is True


def test_http_probe_rejects_non_json(monkeypatch):
    """返回 HTML（比如某个 web 服务占了端口）→ 不可用。"""
    import io as _io

    class Resp(_io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(lp.urllib.request, "urlopen",
                        lambda req, timeout=None: Resp(b"<html>hello</html>"))
    assert lp._probe_ollama() is False


def test_http_probe_rejects_connection_error(monkeypatch):
    """连不上 → 不可用（而不是把异常抛出去）。"""
    def boom(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(lp.urllib.request, "urlopen", boom)
    assert lp._probe_ollama() is False
    assert lp._probe_local_gen() is False


def test_probe_exception_is_swallowed(monkeypatch):
    """探测函数抛异常 → `refresh` 里那一条判不可用，**不影响其它条**。"""
    def boom():
        raise RuntimeError("探测自己炸了")

    monkeypatch.setitem(lp.PROBES, ("llm", "ollama"), boom)
    result = lp.refresh(force=True)
    assert result["llm:ollama"] is False
    # 其它探测正常出结果（有键在，不要求具体真假 —— 取决于本机）
    assert "video_gen:ffmpeg" in result


def test_cmd_probe_requires_zero_exit(monkeypatch):
    """命令探测必须看**退出码**，不能只看"文件存在"。

    架构不对的二进制、被截断的下载都会"存在但跑不了"。
    """
    class R:
        def __init__(self, rc):
            self.returncode = rc
            self.stdout = ""
            self.stderr = ""

    monkeypatch.setattr(lp.subprocess, "run", lambda *a, **kw: R(0))
    assert lp._cmd_ok(["ffmpeg", "-version"]) is True

    monkeypatch.setattr(lp.subprocess, "run", lambda *a, **kw: R(1))
    assert lp._cmd_ok(["ffmpeg", "-version"]) is False

    def boom(*a, **kw):
        raise FileNotFoundError("no such exe")

    monkeypatch.setattr(lp.subprocess, "run", boom)
    assert lp._cmd_ok(["ffmpeg", "-version"]) is False


# ------------------------------------------------------------------
# 缓存
# ------------------------------------------------------------------

def test_cache_avoids_repeated_probing(monkeypatch):
    """缓存期内不重复探测 —— 探测在**界面渲染路径**上，不能每次都跑。"""
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        return True

    monkeypatch.setitem(lp.PROBES, ("llm", "ollama"), counting)
    lp.refresh(force=True)
    first = calls["n"]
    assert first >= 1

    for _ in range(5):
        lp.refresh(force=False)
    assert calls["n"] == first, "缓存期内不该再探测"


def test_cache_expires(monkeypatch):
    """缓存过期后会重新探测（用户刚启动 ollama 时最多等一个 TTL）。"""
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        return True

    monkeypatch.setitem(lp.PROBES, ("llm", "ollama"), counting)
    lp.refresh(force=True)
    n1 = calls["n"]

    # 把缓存时间戳拨到很久以前
    lp._cache_at = 0.0
    lp.refresh(force=False)
    assert calls["n"] > n1, "缓存过期后应重新探测"


def test_payload_exposes_runtime_ready():
    """`/api/providers` 的字段里必须真的带上 `runtime_ready`。

    前端靠它过滤；漏了这个字段前端只能按 `undefined` 处理（当作可用），
    于是"直接不列"这件事**完全没生效**。
    """
    from app.services.providers import PROVIDERS, provider_payload

    d = provider_payload(PROVIDERS[0])
    assert "runtime_ready" in d
    assert isinstance(d["runtime_ready"], bool)
