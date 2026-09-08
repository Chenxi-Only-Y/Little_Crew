"""小剧组 — 后端组织映射：职业 -> 部门(中文)/职业(中文)。

用于把生成产物按「账号 / 部门 / 职业 / 会话」归类到输出目录，方便人找。
与前端 frontend/_org.py 的部门/职位保持一致，这里仅需中文名用于目录。
"""

# 职业 -> (部门中文名, 职业中文名)
ROLE_ORG = {
    "writer": ("创作策划部", "策划总监"),
    "critics": ("创作策划部", "文学策划"),
    "screenwriter": ("创作策划部", "编剧"),
    "character_designer": ("美术资产部", "角色设计师"),
    "scene_designer": ("美术资产部", "场景设计师"),
    "storyboarder": ("导演分镜部", "分镜师"),
    "rhythm_director": ("导演分镜部", "剪辑指导"),
    "continuity_checker": ("导演分镜部", "场记"),
    "director": ("技术制作部", "技术指导"),
    "photographer": ("技术制作部", "摄影师"),
    "quality_assessor": ("技术制作部", "质检总监"),
    "editor": ("后期合成分发部", "剪辑师"),
    "distributor": ("后期合成分发部", "发行总监"),
}

# 已知的职业 key 列表（用于兜底分类）
_DEFAULT = ("综合", "其他")


def dept_for_role(role: str) -> str:
    """返回职业对应的部门中文名。"""
    return ROLE_ORG.get(role or "", _DEFAULT)[0]


def label_for_role(role: str) -> str:
    """返回职业对应的中文名。"""
    return ROLE_ORG.get(role or "", _DEFAULT)[1]
