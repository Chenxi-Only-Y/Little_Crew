"""内置**第三方供应商目录**（2026-11-13）。

## 这是什么

对应界面上那个下拉框（「提供商：zai / openai / anthropic / kimi …」）：
每家供应商的**默认基址、协议、凭据变量名**在这里登记一次，
自定义 API 模式只需选供应商 + 填 key（或另填 baseUrl）即可。

```python
VENDORS["zai"] = Vendor(id="zai", display_name="智谱 AI (Z.AI)",
                        base_url="https://open.bigmodel.cn/api/paas/v4",
                        protocol="openai.images", key_env="ZAI_API_KEY")
```

## 三条纪律

1 **不内置任何 key**：只登记**变量名**（`key_env`），值在 `config/.env`。
   界面上那句"留空使用环境认证（ZAI_API_KEY）"就是这个意思。
2 **协议是枚举**（`PROTOCOLS`）：`openai.chat` / `openai.images` / `anthropic.messages`
   / `gemini.generate` / `comfyui`。写错直接报错，不猜。
3 **未核实的授权不因"是内置供应商"而放行** —— 授权核验与"供应商是否知名"无关。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 支持的协议（决定请求路径 / 请求体 / 响应取值）
PROTOCOLS: dict[str, dict[str, str]] = {
    #: OpenAI 兼容的图像生成：POST {base}/images/generations
    "openai.images": {
        "path": "/images/generations",
        "body": '{"model": "{model}", "prompt": "{prompt}", "size": "{size}", "n": 1}',
        "response": "data.0.url",
        "artifact": "image",
        "task": "image.t2i",
    },
    #: OpenAI 兼容的对话补全：POST {base}/chat/completions
    "openai.chat": {
        "path": "/chat/completions",
        "body": '{"model": "{model}", "messages": [{"role": "user", '
                '"content": "{prompt}"}], "temperature": {temperature}}',
        "response": "choices.0.message.content",
        "artifact": "text",
        "task": "text",
    },
    #: Anthropic 消息接口：POST {base}/v1/messages（鉴权头不同，通道层需支持）
    "anthropic.messages": {
        "path": "/v1/messages",
        "body": '{"model": "{model}", "max_tokens": {max_tokens}, '
                '"messages": [{"role": "user", "content": "{prompt}"}]}',
        "response": "content.0.text",
        "artifact": "text",
        "task": "text",
    },
    #: Gemini 生成接口（路径带模型名，通道层需支持）
    "gemini.generate": {
        "path": "/v1beta/models/{model}:generateContent",
        "body": '{"contents": [{"parts": [{"text": "{prompt}"}]}]}',
        "response": "candidates.0.content.parts.0.text",
        "artifact": "text",
        "task": "text",
    },
}


@dataclass(frozen=True)
class Vendor:
    """一家第三方供应商的默认档案。"""

    id: str
    display_name: str
    base_url: str
    #: 默认协议（可被单个模型覆盖）
    protocol: str
    #: 凭据环境变量名（**不是值**）
    key_env: str
    notes: str = ""
    #: 已知模型（可选）：只是"目录提示"，不保证可用
    models: tuple[str, ...] = field(default_factory=tuple)


#: 内置供应商目录。**新增一家只需要在这里加一行。**
VENDORS: dict[str, Vendor] = {
    "zai": Vendor(
        id="zai",
        display_name="智谱 AI (Z.AI / BigModel)",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        protocol="openai.images",
        key_env="ZAI_API_KEY",
        notes="图像：cogview-3-flash；对话：glm-4-flash 系列。协议为 OpenAI 兼容的子集。",
        models=("cogview-3-flash", "glm-4-flash"),
    ),
    "sudorelay": Vendor(
        id="sudorelay",
        display_name="Sudorelay（OpenAI 兼容中转）",
        base_url="https://api.sudorelay.com/v1",
        protocol="openai.images",
        key_env="SUDORELAY_API_KEY",
        notes="OpenAI 兼容中转。图像 gpt-image-2.x；对话同协议。"
              "实测：不带客户端 User-Agent 会被网关 403，通道需带 UA。",
        models=("gpt-image-2.6-sunburst", "gpt-image-2.5-sunburst"),
    ),
    "dashscope": Vendor(
        id="dashscope",
        display_name="阿里云百炼（通义）",
        base_url="https://dashscope.aliyuncs.com",
        protocol="openai.chat",
        key_env="DASHSCOPE_API_KEY",
        notes="对话与语音走各自路径；图像为 wanx 系列。",
        models=("qwen3-max", "qwen3-tts-flash", "wanx-v1"),
    ),
    "deepseek": Vendor(
        id="deepseek",
        display_name="DeepSeek",
        base_url="https://api.deepseek.com",
        protocol="openai.chat",
        key_env="DEEPSEEK_API_KEY",
        notes="本项目长结构任务的主力（deepseek-flash）。无图像/视频。",
        models=("deepseek-flash", "deepseek-chat"),
    ),
    "moonshot": Vendor(
        id="moonshot",
        display_name="月之暗面 Kimi",
        base_url="https://api.moonshot.cn/v1",
        protocol="openai.chat",
        key_env="MOONSHOT_API_KEY",
        notes="对话为主；长上下文见长。",
        models=("moonshot-v1-8k",),
    ),
    "xiaomi": Vendor(
        id="xiaomi",
        display_name="小米 MiMo",
        base_url="https://api.xiaomimimo.com",
        protocol="openai.chat",
        key_env="MIMO_API_KEY",
        notes="本项目跨族盲读的审核方之一（mimo-v2.6-flash）。",
        models=("mimo-v2.6-flash",),
    ),
    "openai": Vendor(
        id="openai",
        display_name="OpenAI",
        base_url="https://api.openai.com/v1",
        protocol="openai.images",
        key_env="OPENAI_API_KEY",
        notes="图像 gpt-image 系列；对话 gpt 系列。",
        models=("gpt-image-1", "gpt-4o-mini"),
    ),
    "anthropic": Vendor(
        id="anthropic",
        display_name="Anthropic",
        base_url="https://api.anthropic.com",
        protocol="anthropic.messages",
        key_env="ANTHROPIC_API_KEY",
        notes="**协议不是 OpenAI 兼容**（鉴权头 x-api-key + 版本头），通道层需单独支持。",
        models=("claude-sonnet-4-5",),
    ),
    "google": Vendor(
        id="google",
        display_name="Google Gemini",
        base_url="https://generativelanguage.googleapis.com",
        protocol="gemini.generate",
        key_env="GEMINI_API_KEY",
        notes="路径里带模型名；协议与 OpenAI 不同。",
        models=("gemini-2.5-flash",),
    ),
    "local-gen": Vendor(
        id="local-gen",
        display_name="本机 local-gen（零成本）",
        base_url="http://127.0.0.1:8100",
        protocol="openai.images",
        key_env="LOCAL_GEN_API_KEY",
        notes="本地服务；与云端同协议，只是 endpoint 不同。",
        models=("z-image-turbo",),
    ),
}


def vendor(vid: str) -> Vendor | None:
    """按 id 取供应商档案（未知返回 None，不猜）。"""
    return VENDORS.get(str(vid or "").strip())


def protocol(pname: str) -> dict[str, str] | None:
    """按名称取协议预设（未知返回 None）。"""
    return PROTOCOLS.get(str(pname or "").strip())


def describe_vendors() -> list[dict]:
    """给 CLI / doctor 用：列出内置供应商（**不含 key 值**）。"""
    return [{
        "id": v.id,
        "displayName": v.display_name,
        "baseUrl": v.base_url,
        "protocol": v.protocol,
        "keyEnv": v.key_env,
        "models": list(v.models),
        "notes": v.notes,
    } for v in VENDORS.values()]
