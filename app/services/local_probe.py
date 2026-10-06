"""本机能力探测 —— 判断**这台机器上**哪些本地提供者真的能用。

## 为什么要有这个模块

提供者注册表里的 `available` 字段含义是"**代码里接没接**"
（`WaveSpeedAI` 还没接，所以 False）。它**不是**"这台机器上能不能用"。

于是没有本地模型的用户，照样在下拉里看到「本地 Ollama」「本地 · Z-Image」，
选了要到**跑到那一步**才失败，报的还是技术性错误 —— 用户不知道
"是你本机没装"。用户报过这个：

    "直接不列，如果有你觉得怎么识别"

## 识别的原则：**证明不了就当不可用**

界面上少列一个能用的，代价是用户手动找（还有 `local_gen_url` 可覆盖）；
列一个用不了的，代价是用户配了半天然后失败。前者便宜得多。

所以每种能力都用"**能证明它是它**"的方式探测，而不是看端口有没有人占：

  · **端口在监听 ≠ 那个服务在**。最典型的反例：vLLM 默认端口是 **8000**，
    而本项目后端也占 8000 —— 光看端口会把"我们自己的后端"当成 vLLM。
    所以 vLLM 必须校验 `/v1/models` 的响应**长不长得像** OpenAI 格式。
  · ffmpeg 光有文件还不够，要真跑一次 `-version`。
  · edge-tts 要在**外部 venv** 里 `import` 得动。

## 探测方式一览

    ollama        GET :11434/api/tags     且响应里有 models 数组
    vllm          GET :8000/v1/models     且响应像 OpenAI（有 data/object）
    local-gen     GET :8100/health（退回 /）—— 出图 / 出视频共用
    ffmpeg        <外部目录>/bin/ffmpeg.exe -version 退出码 0
    edge-tts      外部 venv 的 python -c "import edge_tts"
    render_2d     不需要探测（渲染在前端 Three.js，无外部依赖）

## 用户显式配置的地址**不探测**

如果 `local_gen_url` / `comfyui_url` 与默认值不同，说明用户知道自己在
做什么（可能有远程的自建服务）。这时以他的配置为准 —— 否则他自己搭的
服务会被误判成"本机没有"。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("little_crew")

#: 探测结果缓存多久（秒）。60 秒是折中：用户刚启动 ollama 后最多等一分钟
#: 就能在界面上看到它，而正常使用期间不会反复探测。
CACHE_TTL_S = 60.0

#: 单个探测的超时（秒）。要短 —— 它是**界面渲染路径**上的一环，
#: 不能让"某个服务没开"把设置页拖慢。
PROBE_TIMEOUT_S = 1.5

#: 并行探测的总超时（秒）。
TOTAL_TIMEOUT_S = 2.5

_lock = threading.Lock()
_cache: dict[str, bool] = {}
_cache_at: float = 0.0


# ------------------------------------------------------------------
# 底层探测原语
# ------------------------------------------------------------------

def _http_ok(url: str, *, validate=None) -> bool:
    """GET 一个地址，200 且 `validate(解析后的 JSON)` 为真才算可用。

    故意收得很紧：**连接失败、超时、非 200、JSON 解析失败、校验不过**
    一律算不可用。宁可漏判（少列一个），不要误判（列了用不了）。
    """
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as r:
            if getattr(r, "status", 200) != 200:
                return False
            body = r.read(200_000).decode("utf-8", "replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return False
    except Exception:  # noqa: BLE001  探测不许把异常抛给调用方
        return False

    if validate is None:
        return True
    try:
        data = json.loads(body)
    except ValueError:
        return False
    try:
        return bool(validate(data))
    except Exception:  # noqa: BLE001
        return False


def _cmd_ok(cmd: list[str]) -> bool:
    """跑一个命令，退出码 0 才算可用。"""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=PROBE_TIMEOUT_S + 1.5,
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _base(url: str, default: str) -> str:
    return (url or default).strip().rstrip("/")


# ------------------------------------------------------------------
# 各提供者的探测
# ------------------------------------------------------------------

def _probe_ollama() -> bool:
    base = _base(os.environ.get("OLLAMA_HOST", ""), "http://127.0.0.1:11434")
    if not base.startswith("http"):
        base = "http://" + base
    # `/api/tags` 是 Ollama 的原生列表接口，返回 `{"models": [...]}`。
    # 用它而不是 `/v1/models`，还因为原生接口带 `capabilities`
    # （能区分对话模型与嵌入模型）。
    return _http_ok(f"{base}/api/tags", validate=lambda d: "models" in d)


def _probe_vllm() -> bool:
    """⚠️ 必须校验响应内容。

    vLLM 默认端口 **8000** 与本项目后端**撞车**。只看"端口有人占"会把
    我们自己的后端当成 vLLM，于是界面上多出一个选了必然失败的选项。
    OpenAI 兼容的 `/v1/models` 返回 `{"object":"list","data":[...]}`，
    而本项目的后端该路径返回的是 404 或别的结构。
    """
    return _http_ok(
        "http://127.0.0.1:8000/v1/models",
        validate=lambda d: isinstance(d, dict) and ("data" in d or d.get("object") == "list"),
    )


def _probe_local_gen() -> bool:
    """本机自建推理服务（出图 / 出视频共用）。

    先试 `/health`，再退回根路径 —— 不同版本的实现挂的路径不一样。
    只要**有一个**返回 200 就算在跑（不校验内容：这是自建服务，
    没有稳定的响应结构约定；而它一旦响应就基本说明是它，因为它
    跑在一个不常用的小端口上）。
    """
    from app.core.config import settings

    base = _base(settings.local_gen_url, "http://127.0.0.1:8100")
    return _http_ok(f"{base}/health") or _http_ok(f"{base}/")


def _probe_ffmpeg() -> bool:
    """外部目录里的 ffmpeg 能真跑起来。

    光有文件不算 —— 架构不对的二进制、被截断的下载都会"存在但跑不了"。
    """
    from app.services.whitebox_video import tools_bin_ffmpeg

    exe = tools_bin_ffmpeg()
    if not exe:
        # 用户 PATH 里自己的那份也算（`find_ffmpeg()` 的第一优先级之一）
        import shutil as _sh

        exe = _sh.which("ffmpeg")
    if not exe:
        return False
    return _cmd_ok([exe, "-version"])


def _probe_edge_tts() -> bool:
    from app.services import tts

    py = tts.tools_python()
    if not py:
        return False
    return _cmd_ok([py, "-c", "import edge_tts"])


#: **不需要探测**就能确定可用的本地提供者。
#:
#: 目前只有 `render_2d`：它是**白模预演**，渲染发生在**前端**（Three.js 体块
#: 场景，见 `web/src/previs/`），后端没有外部服务或二进制依赖 ——
#: 所以"能不能用"不取决于本机装了什么，永远可用。
#:
#: （原先这里写的是"用 PIL 画的俯视示意图"，那是**过时**的描述：
#:  3D 预演做出来之后，后端已无 PIL 绘制白模的代码。能力名仍是 `render_2d`。）
#:
#: ⚠️ 写白名单而不是"没探测函数就默认放行" —— 后者会让**以后新加的**
#: 本地提供者悄悄变成"永远显示"，而那正是本模块要消灭的问题。
NO_PROBE_NEEDED: frozenset[tuple[str, str]] = frozenset({
    ("render_2d", "local"),
})

#: 提供者 key → 探测函数。
#:
#: ⚠️ key 是 `(capability, provider_key)` 的组合，不是单纯的 provider key：
#: 同一个 key `local` 在多个能力下都是"本机自建服务"，但要分开判断
#: （比如出图服务在跑不代表出视频也齐了）。
PROBES: dict[tuple[str, str], object] = {
    ("llm", "ollama"): _probe_ollama,
    ("llm", "vllm"): _probe_vllm,
    ("image_gen", "local"): _probe_local_gen,
    ("image_edit", "local"): _probe_local_gen,
    ("video_gen", "local"): _probe_local_gen,
    ("video_gen", "ffmpeg"): _probe_ffmpeg,
    ("tts", "edge"): _probe_edge_tts,
    # render_2d 的 `local` 渲染在前端（Three.js），无外部依赖，永远可用 ——
    # 不放进探测表，由 `is_ready()` 直接放行。
}


# ------------------------------------------------------------------
# 对外接口
# ------------------------------------------------------------------

def _needs_probe(capability: str, key: str, *, category: str) -> bool:
    """这个提供者要不要探测。"""
    if category not in ("local", "free"):
        return False          # 云端：只要有 Key 就能用，本机探测无意义
    return (capability, key) in PROBES


def is_ready(capability: str, key: str, *, category: str) -> bool:
    """**这台机器上**这个提供者现在能不能用。

    规则：
      · 云端 → 一律 True。能不能用取决于有没有 Key，那不是"本机能力"。
      · 本地 / 免费 → 查探测结果。**没有探测方式的一律 False**
        （证明不了就当不可用）。唯一例外是渲染在前端、无外部依赖的 `render_2d`
        （PIL 绘制，项目自带，没有外部依赖）。
    """
    if category not in ("local", "free"):
        return True
    if (capability, key) not in PROBES:
        # 没有外部依赖的少数几个可以无条件放行。写白名单而不是
        # "默认放行" —— 默认放行会让**以后新加的**本地提供者悄悄变成
        # "永远显示"，那正是这个模块要消灭的问题。
        return (capability, key) in NO_PROBE_NEEDED
    return bool(snapshot().get(f"{capability}:{key}", False))


def snapshot() -> dict[str, bool]:
    """当前探测结果（过期则刷新）。键是 `capability:key`。"""
    return refresh(force=False)


def refresh(force: bool = False) -> dict[str, bool]:
    """重新探测（并行）。

    `force=False` 时缓存还新鲜就直接返回 —— 所以调用方可以无脑调它，
    "要不要真探"这件事在这里决定（而不是散落在调用处）。

    ⚠️ 探测（几秒）**不持锁**，只有换缓存的瞬间持锁。否则并发请求会
    在探测期间全被堵住。
    """
    global _cache, _cache_at
    with _lock:
        if not force and _cache and (time.monotonic() - _cache_at) < CACHE_TTL_S:
            return dict(_cache)

    results: dict[str, bool] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(PROBES) or 1)) as pool:
        futures = {
            pool.submit(_run_probe, cap, key, fn): (cap, key)
            for (cap, key), fn in PROBES.items()
        }
        for fut, (cap, key) in futures.items():
            try:
                results[f"{cap}:{key}"] = bool(fut.result(timeout=TOTAL_TIMEOUT_S))
            except Exception:  # noqa: BLE001  超时或异常 → 当作不可用
                results[f"{cap}:{key}"] = False

    with _lock:
        _cache = results
        _cache_at = time.monotonic()
    logger.info("本机能力探测：%s", results)
    return dict(results)


def _run_probe(cap: str, key: str, fn) -> bool:
    try:
        return bool(fn())  # type: ignore[operator]
    except Exception as exc:  # noqa: BLE001
        logger.debug("探测 %s:%s 失败：%s", cap, key, exc)
        return False


def warm_up() -> None:
    """后台预热一次探测（不阻塞调用方）。

    在 `app.main` 启动时调一下：这样**第一次打开设置页就已经有结果**，
    不必让第一个请求等 `TOTAL_TIMEOUT_S`。
    """
    threading.Thread(target=lambda: refresh(force=True), daemon=True,
                     name="lc-local-probe-warmup").start()
