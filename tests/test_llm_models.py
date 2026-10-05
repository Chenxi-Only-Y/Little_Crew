"""`llm.fetch_models` 的回归测试 —— 列出提供者的可用模型。

## 这个能力解决什么问题（用户提问）

用户选了「本地 vLLM」，界面里"模型"一栏是**空的**，问：
"这种情况怎么知道自己模型"。

确实无从知道：注册表里的 `known_models` 是**云端模型名**（DeepSeek /
OpenAI 那类），而**本地装了什么只有本地服务知道**。让人去翻
`ollama list` 或 vLLM 启动日志，是把系统的信息缺口转嫁给用户。

用户的补充要求："如果是 api 就检测厂商模型" ——
所以策略是：**优先问厂商自己**（`GET {base_url}/models`，厂商上架什么
就有什么），**问不到才退回**已人工核对的 `known_models`（并标明来源）。

用真的 HTTP 服务测（`http.server` 起在随机端口），不 mock：
解析不同厂商的响应 shape 正是这个函数最容易出错的地方。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.services import llm as llm_mod
from app.services.llm import _fallback_known, _maybe_fallback, fetch_models


# ------------------------------------------------------------------
# 一个可编程的假提供者：按路径返回不同 shape
# ------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, object]] = {}
    seen_auth: list[str] = []

    def do_GET(self):  # noqa: N802
        type(self).seen_auth.append(self.headers.get("Authorization") or "")
        status, body = type(self).routes.get(self.path, (404, {"error": "nope"}))
        payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):  # 静音
        return


@pytest.fixture
def fake_provider():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    _Handler.routes = {}
    _Handler.seen_auth = []
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        yield base, _Handler
    finally:
        srv.shutdown()
        srv.server_close()


# ------------------------------------------------------------------
# 问到了：各家的响应 shape 都要认
# ------------------------------------------------------------------

def test_openai_shape(fake_provider):
    """OpenAI / vLLM / 多数网关：{"data": [{"id": ...}]}"""
    base, _ = fake_provider
    _Handler.routes = {"/v1/models": (200, {"data": [{"id": "gpt-6.1-sol"},
                                                      {"id": "gpt-6-luna"}]})}
    r = fetch_models("openai", base + "/v1", "sk-test")
    assert r["ok"] and r["models"] == ["gpt-6.1-sol", "gpt-6-luna"], r
    assert r["source"] == "provider"


def test_ollama_native_shape(fake_provider):
    """Ollama 原生 /api/tags：{"models": [{"name": ...}]}

    这是**本机**场景的主路径。Ollama 的 OpenAI 兼容层各版本对
    `/v1/models` 支持不一致，所以 `/v1/models` 拿不到时会去试 `/api/tags`。
    """
    base, _ = fake_provider
    # 只提供原生端点，/v1/models 返回 404 —— 必须能靠兜底路径拿到
    _Handler.routes = {
        "/v1/models": (404, {"error": "not found"}),
        "/api/tags": (200, {"models": [{"name": "bge-m3:latest"},
                                       {"name": "qwen3:8b"}]}),
    }
    r = fetch_models("ollama", base + "/v1", "")
    assert r["ok"], r
    assert r["models"] == ["bge-m3:latest", "qwen3:8b"], r


def test_dedupes_and_keeps_order(fake_provider):
    base, _ = fake_provider
    _Handler.routes = {"/v1/models": (200, {"data": [{"id": "a"}, {"id": "b"},
                                                      {"id": "a"}, {"id": "c"}]})}
    r = fetch_models("ollama", base + "/v1", "")
    assert r["models"] == ["a", "b", "c"], r


def test_api_key_is_sent(fake_provider):
    """云端要带 Bearer —— 不带就是 401。"""
    base, h = fake_provider
    _Handler.routes = {"/v1/models": (200, {"data": [{"id": "x"}]})}
    fetch_models("openai", base + "/v1", "sk-abc")
    assert any("Bearer sk-abc" == a for a in h.seen_auth), h.seen_auth


# ------------------------------------------------------------------
# 问不到：云端回退到已核对清单，本机如实报错
# ------------------------------------------------------------------

def test_cloud_falls_back_to_known_models(fake_provider):
    """厂商没有 /models 端点 → 用注册表里已核对的清单（标明来源）。"""
    base, _ = fake_provider
    _Handler.routes = {"/v1/models": (404, {"error": "no such endpoint"})}
    r = fetch_models("deepseek", base + "/v1", "sk-x")
    assert r["ok"], r
    assert r["source"] == "known_models"
    assert "deepseek-flash" in r["models"]
    # 来源要说明白，别让用户以为这是实时查到的
    assert "核对" in r["detail"] or "清单" in r["detail"]


def test_local_does_NOT_fall_back(fake_provider):
    """**本机不回退** —— 这是刻意的。

    本机装了什么只有本机知道。拿云端模型名去糊弄用户，会让他在自己的
    Ollama 上选一个根本不存在的模型，**比直接报错更糟**。
    """
    base, _ = fake_provider
    _Handler.routes = {"/v1/models": (404, {}), "/api/tags": (404, {})}
    r = fetch_models("ollama", base + "/v1", "")
    assert not r["ok"], r
    assert r["models"] == []
    assert r["source"] == "provider"


def test_local_down_gives_actionable_hint():
    """本机服务没在跑时，提示要能直接照做，而不是丢一段英文异常。

    用一个**确定没人监听**的端口（1）来触发连接失败。
    """
    r = fetch_models("ollama", "http://127.0.0.1:1/v1", "", timeout=2.0)
    assert not r["ok"], r
    assert r["error_type"] == "network", r
    low = r["detail"]
    assert ("没在运行" in low) or ("ollama serve" in low), low
    # 别把内部地址/堆栈糊用户一脸之外还得给动作
    assert "启动" in low or "serve" in low, low


def test_empty_list_is_ok_with_hint(fake_provider):
    """返回空列表不是错误 —— 本地还没 pull、云端没开通模型都会这样。"""
    base, _ = fake_provider
    _Handler.routes = {"/v1/models": (200, {"data": []}),
                       "/api/tags": (200, {"models": []})}
    r = fetch_models("ollama", base + "/v1", "")
    assert r["ok"], r
    assert r["models"] == []
    assert "pull" in r["detail"] or "模型" in r["detail"]


def test_non_json_response_does_not_raise(fake_provider):
    """有些网关返回 HTML（登录页之类）——不该抛异常，要给人话。"""
    base, _ = fake_provider
    _Handler.routes = {"/v1/models": (200, b"<html>nope</html>")}
    # 用一个**有**已核对清单的云端提供者，这样能看到"回退"这条路
    r = fetch_models("deepseek", base + "/v1", "sk-x")
    assert r["ok"] and r["source"] == "known_models", r


def test_gateway_without_known_models_reports_honestly(fake_provider):
    """网关（known_models 刻意留空）答不了时**如实报错**，不假装有清单。

    OpenRouter / Groq 这些聚合网关的模型名是各家自己的目录 slug，
    我们刻意不预设（预设必然很快过时）——所以这里没有兜底可退，
    只能把厂商的原始错误告诉用户。
    """
    base, _ = fake_provider
    _Handler.routes = {"/v1/models": (404, {"error": "nope"})}
    r = fetch_models("openrouter", base + "/v1", "sk-x")
    assert r["ok"] is False, r
    assert r["models"] == []
    assert r["detail"], "必须给出原因，不能只说失败"


# ------------------------------------------------------------------
# 纯函数部分
# ------------------------------------------------------------------

def test_fallback_known_covers_curated_providers():
    """已核对的云端提供者都要能给出兜底清单。"""
    for p in ("deepseek", "zhipu", "qwen", "moonshot", "doubao", "mimo",
              "ernie", "hunyuan", "stepfun", "baichuan", "minimax",
              "mistral", "openai", "gemini"):
        ks = _fallback_known(p)
        assert ks, f"{p} 缺兜底清单"


def test_fallback_known_includes_default_model_first():
    """默认模型一定可用，排在第一位。"""
    ks = _fallback_known("deepseek")
    assert ks[0] == "deepseek-flash", ks


def test_fallback_known_unknown_provider_is_empty():
    assert _fallback_known("nonexistent-provider") == []


def test_maybe_fallback_never_fakes_models_for_local():
    """本机场景下 `_maybe_fallback` 绝不返回模型。"""
    r = _maybe_fallback("ollama", "http://localhost:11434/v1", True, "network", "连不上")
    assert r["ok"] is False and r["models"] == []
