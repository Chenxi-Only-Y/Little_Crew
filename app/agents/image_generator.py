"""Agent 5 图片生成（app/agents/image_generator.py）。

方案B 角色一致性：集成 IP-Adapter 生成角色一致的画面。

输入：state.prompts（含 shot_id/prompt/character_name/character_prompt）
     + state.character_reference_images（{"角色名": url}）
处理：
  - 对每个镜头，提取角色名（优先用 prompt.character_name，其次从描述/正向词里找）
  - 从 character_reference_images 查该角色参考图
  - 有参考图 → 用 IP-Adapter 生成（单角色/双角色/多角色按策略）
  - 无参考图 → 回退普通文生图
  - IP-Adapter 失败 → 回退普通文生图；普通文生图失败 → 占位图；逐级记录降级日志
优化：
  - 并发生成（ThreadPoolExecutor，默认 4 线程）
  - 缓存：相同 角色+提示词 30 分钟内复用结果
输出：state.images —— [{shot_id, image_url, local_path, status, retry, method}]
"""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.agents.base import BaseAgent
from app.core.state import CrewState
from app.services.image import get_image_service
from app.services.ip_adapter_service import generate_with_ip_adapter

logger = logging.getLogger("little_crew")

# 参考图缓存：{"<session>|<refs>|<prompt>": {"url": str, "expires": ts}}（30 分钟）
_IPADAPTER_CACHE: dict = {}
_CACHE_TTL_SEC = 30 * 60
_CACHE_MAX = 256
_CACHE_LOCK = __import__("threading").Lock()


def _extract_character_names(prompt: dict, known_names: list) -> list:
    """提取镜头中的角色名。优先用 prompt.character_name，其次在描述/正向词里匹配。"""
    names = []
    cn = str(prompt.get("character_name") or "").strip()
    if cn:
        names.append(cn)
    for text in (str(prompt.get("description") or ""), str(prompt.get("prompt") or ""),
                 str(prompt.get("positive") or "")):
        for name in known_names:
            if name and name in text and name not in names:
                names.append(name)
    return names


def _ipadapter_cache_key(refs: list, prompt: str) -> str:
    """缓存键加入当前会话，避免跨用户/跨会话复用同一张图。"""
    from app.services.runtime_settings import run_session

    sess = run_session() or "-"
    return f"{sess}||{'|'.join(sorted(refs))}||{prompt}"


def _cache_get(key: str):
    with _CACHE_LOCK:
        entry = _IPADAPTER_CACHE.get(key)
        if entry and entry["expires"] > time.time():
            return entry["url"]
        _IPADAPTER_CACHE.pop(key, None)
    return None


def _cache_set(key: str, url: str) -> None:
    now = time.time()
    with _CACHE_LOCK:
        # 清掉过期项，限制内存增长
        expired = [k for k, v in _IPADAPTER_CACHE.items() if v.get("expires", 0) <= now]
        for k in expired:
            _IPADAPTER_CACHE.pop(k, None)
        if len(_IPADAPTER_CACHE) >= _CACHE_MAX:
            _IPADAPTER_CACHE.clear()
        _IPADAPTER_CACHE[key] = {"url": url, "expires": now + _CACHE_TTL_SEC}


class ImageGenerator(BaseAgent):
    name = "image_generator"

    # 失败重试次数（总尝试次数 = MAX_RETRIES + 1）
    MAX_RETRIES = 2
    # 并发生成线程数
    CONCURRENCY = 4

    def run(self, state: CrewState) -> dict:
        """图片生成（集成 IP-Adapter）
        inputs:
            prompts: list[dict]              结构化 Prompt 列表
            character_reference_images: dict 角色参考图 {"角色名": url}
        outputs:
            images: list[dict]               图片结果
            retry_count: dict                记录图片生成轮次
        """
        prompts = list(state.get("prompts") or [])
        char_refs = dict(state.get("character_reference_images") or {})
        known_names = list(char_refs.keys())
        svc = get_image_service()
        ip_svc = get_ip_adapter_service()

        # 人工确认点④触发的重做：强制全量重生成；否则仅重算失败项
        force_redo = state.get("review_decision") == "redo" and state.get("review_point") == 4
        if force_redo:
            images_by_shot: dict = {}
        else:
            images_by_shot: dict = {im["shot_id"]: im for im in (state.get("images") or [])}

        # 精确重排：retry_shots 指定了要重新生成的镜头集合（其余保持原图）。
        # 命中这些镜头时，即使原来 status=ok 也强制重生成。
        retry_shots = set(state.get("retry_shots") or [])

        # 图片重做反馈：并入每个 prompt 的正向词
        if state.get("feedback"):
            prompts = [self._apply_feedback(p, state["feedback"]) for p in prompts]

        # 过滤出仍需生成的镜头
        pending = []
        for p in prompts:
            sid = p.get("shot_id")
            if retry_shots:
                # 精确重排模式：只重排被点选的镜头
                if sid in retry_shots:
                    pending.append(p)
                continue
            existing = images_by_shot.get(sid)
            if existing and existing.get("status") == "ok":
                continue
            pending.append(p)

        if pending:
            # 并发生成
            with ThreadPoolExecutor(max_workers=self.CONCURRENCY) as pool:
                futs = {
                    pool.submit(self._generate_one_with_ip, p, svc, ip_svc, char_refs, known_names): p
                    for p in pending
                }
                for fut in as_completed(futs):
                    p = futs[fut]
                    try:
                        images_by_shot[p["shot_id"]] = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        images_by_shot[p["shot_id"]] = {
                            "shot_id": p["shot_id"], "image_url": "", "local_path": "",
                            "status": "failed", "retry": self.MAX_RETRIES,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    if not svc.mock:  # 真实 API：镜头之间加间隔，避免突发限流 429
                        time.sleep(1.5)

        # 按 prompt 顺序输出，保持与分镜一一对应
        ordered = [images_by_shot[p["shot_id"]] for p in prompts if p.get("shot_id") in images_by_shot]

        retry_count = dict(state.get("retry_count") or {})
        # 仅在「因失败/重做而重跑」时才递增：首轮已成功的不计入重试次数。
        if state.get("images"):
            retry_count["image_generator"] = retry_count.get("image_generator", 0) + 1

        return {"images": ordered, "retry_count": retry_count, "current_phase": "image_generator"}

    # ---------------------------------------------------------------
    # 单镜头生成：IP-Adapter → 文生图 → 占位图 降级链
    # ---------------------------------------------------------------
    def _generate_one_with_ip(self, prompt: dict, svc, ip_svc, char_refs: dict, known_names: list) -> dict:
        """对单个镜头生成，优先 IP-Adapter（仅当配置了真实 provider），失败逐级降级到文生图(MiniMax)。"""
        sid = prompt.get("shot_id")
        names = _extract_character_names(prompt, known_names)
        ref_images = self._select_reference_images(names, char_refs)
        # 只有配置了真实 IP-Adapter(replicate/comfyui)才用；mock 时直接用主图片服务(MiniMax)出真实图
        from app.core.config import settings as _st
        ip_provider = (getattr(_st, "ip_adapter_provider", "mock") or "mock").lower()
        use_ip = ref_images and ip_provider not in ("", "mock")

        method = "text2img"
        try:
            if use_ip:
                best = self._generate_with_ipadapter_cached(ref_images, prompt, ip_svc)
                if best:
                    path = self._save_local(best, sid)
                    return {"shot_id": sid, "image_url": best, "local_path": path,
                            "status": "ok", "retry": 0, "method": "ipadapter",
                            "characters": names}
                logger.warning("[image_generator] %s IP-Adapter 无结果，回退文生图", sid)
                method = "text2img"
            else:
                logger.info("[image_generator] %s 用主图片服务(%s)直接出图", sid, _st.image_provider)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[image_generator] %s IP-Adapter 失败(%s)，回退文生图", sid, exc)

        # 普通文生图（MiniMax）
        return self._generate_one(svc, prompt, method_base=method, chars=names)

    def _generate_one(self, svc, prompt: dict, method_base: str = "text2img", chars: list | None = None) -> dict:
        """普通文生图，失败重试；最终失败返回占位图。"""
        sid = prompt.get("shot_id")
        last_error = None
        for attempt in range(self.MAX_RETRIES + 1):
            try:
                url, path, ok = svc.generate(prompt, role="photographer")
                if ok:
                    return {"shot_id": sid, "image_url": url, "local_path": path, "status": "ok",
                            "retry": attempt, "method": method_base, "characters": chars or []}
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning("[image_generator] %s 文生图尝试%d失败：%s", sid, attempt + 1, exc)
        # 降级到占位图
        placeholder = self._placeholder(sid)
        return {"shot_id": sid, "image_url": placeholder, "local_path": "", "status": "failed",
                "retry": self.MAX_RETRIES, "method": "placeholder",
                "error": f"{type(last_error).__name__}: {last_error}" if last_error else "生成失败",
                "characters": chars or []}

    def _placeholder(self, sid: str) -> str:
        """生成确定性占位图 URL（mock 模式下仍可回显）。"""
        try:
            # 用 PIL 落一张占位图到本地，路径前加数据可访问
            from PIL import Image
            from app.services.image import _run_output_dir
            from app.core.config import settings
            out = _run_output_dir(settings.output_dir_path, "photographer", sid)
            p = out / f"{sid}.png"
            Image.new("RGB", (768, 1024), (120, 120, 130)).save(p)
            return str(p)
        except Exception:  # noqa: BLE001
            return f"https://picsum.photos/seed/{sid}/768/1024"

    def _save_local(self, url: str, sid: str) -> str:
        """把远端图片 URL 下载到本地（用于落盘展示，按账号/部门/职业/会话归类）。"""
        try:
            import httpx
            from app.core.config import settings
            from app.services.image import _run_output_dir
            out = _run_output_dir(settings.output_dir_path, "photographer", sid)
            path = out / f"{sid}.png"
            with httpx.Client(timeout=60) as client:
                resp = client.get(url)
                resp.raise_for_status()
                path.write_bytes(resp.content)
            return str(path)
        except Exception:  # noqa: BLE001
            return ""

    # ---------------------------------------------------------------
    # 参考图选择策略
    # ---------------------------------------------------------------
    def _select_reference_images(self, names: list, char_refs: dict) -> list:
        """按多角色策略挑选参考图列表。

        - 1 个角色：用该角色参考图
        - 2 个角色：用两个角色参考图（IP-Adapter 多参考）
        - 3+ 角色：仅用主要角色(第一个)参考图，其余靠文本描述
        - 名称均未命中但存在参考图（如用户上传的自定义文件名）：回退为通用参考池（取前 2 张）
        """
        available = [n for n in names if char_refs.get(n)]
        if not available:
            # 兜底：名称对不上但有参考图 → 当通用参考池用
            pool = [v for v in char_refs.values() if v]
            return pool[:2] if pool else []
        if len(available) <= 2:
            return [char_refs[n] for n in available]
        return [char_refs[available[0]]]  # 3+：只取主要角色

    # ---------------------------------------------------------------
    # IP-Adapter（带 30 分钟缓存）
    # ---------------------------------------------------------------
    def _generate_with_ipadapter_cached(self, ref_images: list, prompt: dict, ip_svc) -> str:
        """调用 IP-Adapter 生成，命中缓存则直接复用（30 分钟）。"""
        prompt_text = str(prompt.get("prompt") or prompt.get("positive") or "").strip()
        key = _ipadapter_cache_key(ref_images, prompt_text)
        cached = _cache_get(key)
        if cached:
            return cached
        seed = prompt.get("seed")
        negative = str(prompt.get("negative") or prompt.get("negative_prompt") or "")
        strength = float(prompt.get("ipadapter_strength") or 0.8)
        url = generate_with_ip_adapter(ref_images, prompt_text, negative, strength=strength, seed=seed)
        if url:
            _cache_set(key, url)
        return url

    def _apply_feedback(self, prompt: dict, feedback) -> dict:
        """把人工反馈并入 prompt 的正向词（重做时用）。"""
        p = dict(prompt)
        hint = feedback if isinstance(feedback, str) else json.dumps(feedback, ensure_ascii=False)
        p["prompt"] = f"{p.get('prompt', '')}, {hint}".strip(", ")
        if p.get("positive"):
            p["positive"] = p["prompt"]
        return p


# 延迟导入，避免循环依赖
def get_ip_adapter_service():
    from app.services.ip_adapter_service import get_ip_adapter_service as _g

    return _g()
