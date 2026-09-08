"""小剧组 — 组织架构元数据（5 部门 × 13 职位）。

供「首页(按部门划分) → 部门页(选职业) → 职业页」三级导航共用。
一份数据驱动，避免硬编码散落。

每个职位字段：
  key:      职业唯一标识（对应 _console 各页传入的 role_key）
  title:    职位名称（Agent 职位）
  real_job: 对应真人职业（展示用）
  agent:    Agent 名称
  duty:     一句话职责
  page:     该职业的 Streamlit 页面路径
  ready:    是否已实现（False -> 页面显示占位提示，不可进入）
"""

# 已实现、可作为独立起点进入的职位页面
_IMPLEMENTED_PAGES = {
    "writer": "pages/01_策划总监.py",
    "screenwriter": "pages/03_编剧.py",
    "storyboarder": "pages/06_分镜师.py",
    "director": "pages/09_技术指导.py",
    "photographer": "pages/10_摄影师.py",
    "editor": "pages/12_剪辑师.py",
}

# 按部门组织的机构表（顺序即展示顺序）
DEPARTMENTS = [
    {
        "key": "story_dev",
        "name": "创作策划部",
        "desc": "把故事从点子变成剧本",
        "roles": [
            {"key": "writer", "title": "策划总监", "real_job": "策划总监", "agent": "故事策划",
             "duty": "把点子变大纲", "page": _IMPLEMENTED_PAGES["writer"], "ready": True},
            {"key": "critics", "title": "文学策划", "real_job": "文学策划", "agent": "剧评",
             "duty": "检查剧本好不好", "page": "pages/02_文学策划.py", "ready": True},
            {"key": "screenwriter", "title": "编剧", "real_job": "编剧", "agent": "编剧",
             "duty": "把大纲变剧本", "page": _IMPLEMENTED_PAGES["screenwriter"], "ready": True},
        ],
    },
    {
        "key": "art_dept",
        "name": "美术资产部",
        "desc": "确定角色长什么样、场景长什么样",
        "roles": [
            {"key": "character_designer", "title": "角色设计师", "real_job": "角色设计师", "agent": "角色设计",
             "duty": "设计演员长啥样", "page": "pages/04_角色设计师.py", "ready": True},
            {"key": "scene_designer", "title": "场景设计师", "real_job": "场景设计师", "agent": "场景设计",
             "duty": "设计舞台长啥样", "page": "pages/05_场景设计师.py", "ready": True},
        ],
    },
    {
        "key": "direction",
        "name": "导演分镜部",
        "desc": "把剧本翻译成镜头语言",
        "roles": [
            {"key": "storyboarder", "title": "分镜师", "real_job": "分镜师", "agent": "分镜构建",
             "duty": "把剧本变镜头", "page": _IMPLEMENTED_PAGES["storyboarder"], "ready": True},
            {"key": "rhythm_director", "title": "剪辑指导", "real_job": "剪辑指导", "agent": "节奏导演",
             "duty": "决定每个镜头多长", "page": "pages/07_剪辑指导.py", "ready": True},
            {"key": "continuity", "title": "场记", "real_job": "场记", "agent": "连续性检查",
             "duty": "查穿帮", "page": "pages/08_场记.py", "ready": True},
        ],
    },
    {
        "key": "tech_prod",
        "name": "技术制作部",
        "desc": "把镜头变成实际画面，并保证质量",
        "roles": [
            {"key": "director", "title": "技术指导", "real_job": "提示词工程师", "agent": "提示词编译",
             "duty": "写AI能看懂的话", "page": _IMPLEMENTED_PAGES["director"], "ready": True},
            {"key": "photographer", "title": "摄影师", "real_job": "摄影师", "agent": "图片生成",
             "duty": "拍出实际画面", "page": _IMPLEMENTED_PAGES["photographer"], "ready": True},
            {"key": "quality_assessor", "title": "质检总监", "real_job": "质检总监", "agent": "质量评估",
             "duty": "检查拍得好不好", "page": "pages/11_质检总监.py", "ready": True},
        ],
    },
    {
        "key": "post_dist",
        "name": "后期合成分发部",
        "desc": "整合成片，发布到平台",
        "roles": [
            {"key": "editor", "title": "剪辑师", "real_job": "剪辑师", "agent": "结果聚合",
             "duty": "整理素材", "page": _IMPLEMENTED_PAGES["editor"], "ready": True},
            {"key": "distributor", "title": "发行总监", "real_job": "发行总监", "agent": "分发协调",
             "duty": "发到平台", "page": "pages/13_发行总监.py", "ready": True},
        ],
    },
]

# 方便按 key 查找职位 / 部门
ROLE_BY_KEY = {r["key"]: r for dept in DEPARTMENTS for r in dept["roles"]}
DEPT_BY_KEY = {d["key"]: d for d in DEPARTMENTS}


def role_dept_key(role_key: str) -> str | None:
    """返回某职业所属部门的 key。"""
    for dept in DEPARTMENTS:
        if any(r["key"] == role_key for r in dept["roles"]):
            return dept["key"]
    return None
