"""小剧组 — 后端组织映射：职业 -> 部门(中文)/职业(中文)。

用于把生成产物按「账号 / 部门 / 职业 / 会话」归类到输出目录，方便人找。

部门/职业的唯一事实来源是 `app/registry/roles.py`；这里只保留**中文名**，
用途仅是拼输出目录（因此不重复维护依赖关系与顺序）。

> 历史上这里写过"与前端 frontend/_org.py 保持一致"——
> 那个 Streamlit 前端以及 `_org.py` 都已删除（前端现为 `web/` 导演台，
> 它从 `/api/registry` 取职业定义，不再有第二份清单）。

## ⚠️ 这张表曾经和注册表**对不上**，导致资产归错部门

之前 `ROLE_ORG` 的 key 是更早一版的职业名（`writer` / `critics` /
`rhythm_director` / `continuity_checker` / `director`），而注册表已经改成
`showrunner` / `script_editor` / `supervising_editor` / `continuity` 等。
实测 16 个职业里有 **8 个查不到**，`dept_for_role()` 全部落到兜底「综合」：

    showrunner · script_editor · previs_artist · continuity
    voice_director · supervising_editor · workflow_architect · technical_director

后果是这些职业的产出被归到「综合」目录，没法按部门找到——
正是"资产要独立放"想解决的问题本身。

`tests/test_org.py` 现在会**双向核对**这张表与注册表：
   · 注册表里每个职业都必须能查到（否则资产归错部门）；
   · 这张表里不能有注册表已删除的 key（僵尸条目）。
"""

# 职业 -> (部门中文名, 职业中文名)
#
# ⚠️ 这张表的正确性由 `tests/test_org.py` 对着注册表双向校验。
#    改动注册表后必须同步这里，否则测试会直接指出哪几个 key 对不上。
ROLE_ORG = {
    # 基建
    "workflow_architect": ("基建", "工作流架构师"),
    "technical_director": ("基建", "技术指导"),
    # 策划
    "showrunner": ("策划", "策划总监"),
    "script_editor": ("策划", "文学策划"),
    "screenwriter": ("策划", "编剧"),
    "storyboarder": ("策划", "分镜师"),
    # 资产
    "character_designer": ("资产", "角色设计师"),
    "scene_designer": ("资产", "场景设计师"),
    # 生图
    "photographer": ("生图", "摄影师"),
    "quality_assessor": ("生图", "图像质检师"),
    "inpainter": ("生图", "修图师"),
    # 白模（注意：是"模"不是"膜" —— 影视预演里的白模 = greybox / previz，
    # 指没有材质贴图的灰白素模。这一行原先写成"白膜"，与下面同一行的
    # "白模预演师"自相矛盾，且部门名会直接显示在界面上。）
    "previs_artist": ("白模", "白模预演师"),
    # 动态
    "continuity": ("动态", "场记"),
    "motion_compositor": ("动态", "动态合成师"),
    "video_assessor": ("动态", "视频质检师"),
    "voice_director": ("动态", "配音指导"),
    # 后期
    "supervising_editor": ("后期", "剪辑指导"),
    "editor": ("后期", "剪辑师"),
    "distributor": ("后期", "发行总监"),
}

# 兜底：注册表里新增了职业但这里忘了同步时用
_DEFAULT = ("综合", "其他")


def dept_for_role(role: str) -> str:
    """返回职业对应的部门中文名。"""
    return ROLE_ORG.get(role or "", _DEFAULT)[0]


def label_for_role(role: str) -> str:
    """返回职业对应的中文名。"""
    return ROLE_ORG.get(role or "", _DEFAULT)[1]
