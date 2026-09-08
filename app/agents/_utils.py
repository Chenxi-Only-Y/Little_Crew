"""文本 Agent 共用工具：DeepSeek 调用 + 容错 JSON 解析。"""

import json

from app.services.llm import get_llm


def call_deepseek(system_prompt: str, user_input: str, role: str = "", retries: int = 2) -> str:
    """调用 DeepSeek 返回原始文本。mock 模式下会被 LLMService 拒绝。

    Args:
        system_prompt: 系统提示词（约束输出 JSON 等）。
        user_input: 用户输入（通常为上一阶段的 JSON 文本）。
        role: 该环节角色，用于切换不同模型（作家/编剧/分镜师/技术指导…）。
        retries: 失败后重试次数（真实模型偶发失败时重试提升稳定性）。

    Returns:
        str: 模型返回的正文。
    """
    import time as _t
    last: Exception | None = None
    for attempt in range(retries + 1):
        if attempt:
            _t.sleep(1.5 * attempt)
        try:
            return get_llm().chat(system_prompt, user_input, role=role)
        except Exception as exc:  # noqa: BLE001
            last = exc
            continue
    raise RuntimeError(f"DeepSeek 调用重试 {retries} 次仍失败：{type(last).__name__}: {last}")


def parse_json(text: str):
    """容错解析：自动剥 ```json``` 包裹；必要时截取首尾 {} 之间的内容。

    Args:
        text: 模型返回文本。

    Returns:
        object: dict / list，取决于模型输出。

    Raises:
        json.JSONDecodeError: 无法解析时抛出。
    """
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) >= 2 else text
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])
