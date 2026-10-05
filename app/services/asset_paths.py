"""产出资产的统一路径规则。

## 为什么要有这个模块

"不同职业生成的东西，资产独立放"——这是用户明确要求的结构。
但改动之前各个产出的路径是**各写各的、互相不一致**：

    image.py:88      {user}/{dept}/{role}/{sess}/            （图片）
    video.py:53      {user}/{dept}/{role}/{sess}/video/      （视频，多一层）
    video.py:127,147 {sess}/video/                           （**丢掉部门与职业**）
    main.py:1188     whitebox/{uuid}/                        （**完全没有归类**）

于是同一部片子的资产散在四处，想"把某个职业的产出单独打包"根本做不到。

现在统一成一条规则：

    {output_dir}/{user}/{部门}/{职业}/{会话}/{类型}/{文件}

  · `user`   —— 账号 UID（重名/匿名不会撞在一起）
  · `部门`   —— 由职业反查（`app/core/org.py:dept_for_role`）
  · `职业`   —— 生成该资产的 role key
  · `会话`   —— 一次运行/一个工程；没有会话时退化成 `no-session-<时间戳>`
  · `类型`   —— `images` / `videos` / `frames` / `audio` / `meta`
                **图片与视频各占一个目录**，这是"独立放"的落点

## 两个刻意的设计

1. **类型目录用复数**（`images`/`videos`）。旧代码写的是单数 `video`，
   改齐之后老目录不会再被写入——这是有意的：宁可留一个空壳旧目录，
   也不要把新旧两种规则混在同一个目录里（那正是最初混乱的来源）。

2. **role 为空时用 `_unknown`**，不抛异常。产出落盘是"最后一公里"，
   因为它失败而丢掉已经花过钱的生成结果，代价远大于目录名不好看。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Literal

#: 资产类型 → 子目录名
AssetKind = Literal["images", "videos", "frames", "audio", "meta"]

_KIND_DIR: dict[str, str] = {
    "images": "images",
    "videos": "videos",
    "frames": "frames",
    "audio": "audio",
    "meta": "meta",
}

#: 路径里不允许出现的字符（Windows 保留字符 + 路径分隔符）
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_segment(name: str, fallback: str = "_unknown", limit: int = 60) -> str:
    """把一段名字清洗成可安全用作目录名的字符串。

    Windows 上 `<>:"/\\|?*` 与不可见字符都会让 `mkdir` 失败，
    而 role / session 里偶尔会混进用户输入的工程名。清洗比报错好：
    产出落盘是最后一公里，不该因为目录名不好看而丢掉已经生成的资产。
    """
    s = _UNSAFE.sub("_", str(name or "")).strip().strip(".")
    # Windows 还禁用若干保留名（CON、PRN、NUL…），统一加前缀规避
    if s.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"COM[1-9]|LPT[1-9]", s.upper()):
        s = f"_{s}"
    if not s:
        return fallback
    return s[:limit]


def current_user() -> str:
    """当前账号 UID；拿不到就退化成 `local`。"""
    try:
        from app.services.runtime_settings import user as cur_user
        from app.services.auth import get_user_store

        u = str(cur_user() or "local")
        try:
            # 用唯一 UID 而不是登录名，避免重名/匿名撞在一起
            return safe_segment(get_user_store().resolve_uid(u) or u, "local")
        except Exception:  # noqa: BLE001
            return safe_segment(u, "local")
    except Exception:  # noqa: BLE001
        return "local"


def current_session() -> str:
    """当前会话 id；没有会话时退化成按小时分桶的 `no-session-*`。

    ⚠️ 退化成**按小时**而不是纯 uuid：独立生成（没有工程会话）时，
    同一个小时内的产出仍然聚在一起，方便人翻找；
    用纯 uuid 会退回到"一堆看不出关系的目录"那种状态。
    """
    try:
        from app.services.runtime_settings import run_session

        s = run_session()
        if s:
            return safe_segment(s)
    except Exception:  # noqa: BLE001
        pass
    return f"no-session-{time.strftime('%Y%m%d-%H')}"


def dept_for(role: str) -> str:
    """职业 → 部门。查不到时用 `_unknown`，不抛异常。"""
    try:
        from app.core.org import dept_for_role

        return safe_segment(dept_for_role(role), "_unknown")
    except Exception:  # noqa: BLE001
        return "_unknown"


def asset_dir(
    output_dir: str | Path,
    role: str,
    kind: AssetKind,
    *,
    user: str | None = None,
    session: str | None = None,
    create: bool = True,
) -> Path:
    """取（并按需创建）某职业某类资产的目录。

        asset_dir(settings.output_dir_path, "photographer", "images")
        → {outputs}/{uid}/技术制作部/photographer/2026.../images

    这是**唯一**的产出目录入口。任何新写的落盘代码都应当走它，
    不要再自己拼 `Path(out)/...`——那正是最初四处不一致的来源。
    """
    u = safe_segment(user) if user is not None else current_user()
    sess = safe_segment(session) if session is not None else current_session()
    role_name = safe_segment(role, "_unknown")
    sub = _KIND_DIR.get(kind, kind)

    d = Path(output_dir) / u / dept_for(role_name) / role_name / sess / sub
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def asset_path(
    output_dir: str | Path,
    role: str,
    kind: AssetKind,
    filename: str,
    *,
    user: str | None = None,
    session: str | None = None,
    create: bool = True,
) -> Path:
    """取（并按需创建）一个资产文件的完整路径。见 `asset_dir`。"""
    stem = safe_segment(Path(filename).stem, "out", limit=80)
    ext = _UNSAFE.sub("", Path(filename).suffix)[:12]
    return asset_dir(
        output_dir, role, kind, user=user, session=session, create=create
    ) / f"{stem}{ext}"
