"""Agent 6.5 发行总监/分发协调（app/agents/distributor.py）。

方案B：把成片发布到各平台（B站/抖音/YouTube/快手/小红书）。
  - 自动生成标题、描述、标签（基于故事大纲）
  - 输出各平台发布状态 + 链接
  - 优先实现 B站/抖音；其余平台预留占位
  - Mock 模式返回模拟发布链接

说明：真实平台上传需要各平台的 API 凭证与 OAuth，外部库（OmniPost / social-auto-upload）
在开发环境不可保证可用。这里提供：
  - 平台注册表 + 各平台可用的「真实」上传入口骨架（未接入时抛 NotImplementedError，回退 mock）
  - mock 分支返回模拟链接（状态成功）
  - OAuth「链接」入口：把凭证存到 runtime_settings（session/环境变量）
"""

from typing import Dict, List

from app.agents.base import BaseAgent
from app.core.config import settings
from app.core.state import CrewState

# 平台注册表：key -> {name, enabled, desc}（顺序即优先展示顺序）
PLATFORMS: Dict[str, dict] = {
    "bilibili": {"name": "B站", "enabled": True, "desc": "哔哩哔哩"},
    "douyin": {"name": "抖音", "enabled": True, "desc": "抖音"},
    "youtube": {"name": "YouTube", "enabled": True, "desc": "YouTube"},
    "kuaishou": {"name": "快手", "enabled": True, "desc": "快手"},
    "xiaohongshu": {"name": "小红书", "enabled": True, "desc": "小红书"},
}


def _platform_meta(outline: dict) -> dict:
    """从故事大纲生成发布元数据（标题/描述/标签）。"""
    title = str((outline or {}).get("title") or "小剧组漫剧")
    logline = str((outline or {}).get("logline") or "")
    genre = str((outline or {}).get("genre") or "漫剧")
    style = str((outline or {}).get("style") or "")
    tags = [genre, style, "AI漫剧", "小剧组"]
    return {"title": title, "description": logline, "tags": [t for t in tags if t]}


class Distributor(BaseAgent):
    name = "distributor"

    def run(self, state: CrewState, platforms: List[str] | None = None) -> dict:
        """发行总监
        inputs:
            video: dict              最终视频（dir_or_path / produced，来自 payload.video）
            story_outline: dict      故事大纲（生成标题/描述/标签）
            platforms: list(可选)     指定平台；缺省则全部
        outputs:
            publish_results: dict    平台->{platform,title,description,tags,status,url}
        """
        video = state.get("video") or {}
        outline = state.get("story_outline") or {}
        meta = _platform_meta(outline)

        chosen = platforms or list(PLATFORMS.keys())
        if not PLATFORMS or not chosen:
            chosen = list(PLATFORMS.keys())

        results: Dict[str, dict] = {}
        for pk in chosen:
            if pk not in PLATFORMS:
                continue
            results[pk] = self._publish_one(pk, video, meta, state)
        return {"publish_results": results, "current_phase": self.name}

    def _publish_one(self, pk: str, video: dict, meta: dict, state: CrewState) -> dict:
        """发布单个平台。mock 返回成功+链接；真实入口未接入则回退 mock。"""
        platform = PLATFORMS[pk]
        entry = {
            "platform": pk,
            "name": platform["name"],
            "title": meta["title"],
            "description": meta["description"],
            "tags": meta["tags"],
            "status": "publishing",
            "url": "",
        }
        # 若无视频产物，标注 draft（未发布）
        if not video.get("dir_or_path") and not video.get("produced"):
            entry["status"] = "draft"
            entry["url"] = ""
            return entry
        try:
            # 真实上传：若实现了某平台则走真实，否则 mock
            if self._has_real(pk):
                entry["url"] = self._real_upload(pk, video, meta, state)
                entry["status"] = "success"
            else:
                entry["status"] = "success"
                entry["url"] = self._mock_url(pk, meta["title"])
        except Exception as exc:  # noqa: BLE001
            entry["status"] = "failed"
            entry["url"] = ""
            entry["error"] = str(exc)
        return entry

    @staticmethod
    def _has_real(pk: str) -> bool:
        """是否有真实上传实现（B站/抖音 优先预留）。"""
        # 骨架：B站/抖音 标为可扩展，现阶段仅当设置有对应环境变量才视为真实
        env_key = f"PUBLISH_{pk.upper()}_ENABLED"
        import os

        return str(os.environ.get(env_key, "")).strip() in ("1", "true", "True")

    @staticmethod
    def _mock_url(pk: str, title: str) -> str:
        return f"https://www.{pk}.com/video/mock_{title}"

    @staticmethod
    def _real_upload(pk: str, video: dict, meta: dict, state: CrewState) -> str:
        """真实上传入口（骨架，需接具体平台 API）。未实现则抛错。"""
        raise NotImplementedError(f"{pk} 真实上传未接入，请设置对应 PUBLISH_*_ENABLED 或使用 mock。")
