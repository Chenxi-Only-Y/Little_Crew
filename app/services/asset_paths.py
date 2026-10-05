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

    {output_dir}/{账号}/{项目名}/{部门}/{职业}/{会话}/{文件}

  · `账号`   —— 账号 UID（重名/匿名不会撞在一起）
  · `项目名` —— **当前工程名**（导演台工程栏那个）。
                用户明确要求的分组依据："账号/项目名/部门/职业/会话/类型/文件"。
                一个工程可以有多次运行（多个 session），产出要归到**工程**下，
                这样"把某个项目的资产打包"才成立。
  · `部门`   —— 由职业反查（`app/core/org.py:dept_for_role`）
  · `职业`   —— 生成该资产的 role key
  · `会话`   —— 一次运行；没有会话时退化成按小时分桶的 `no-session-*`
  · `职业`   —— 生成该资产的 role key。**它本身就区分了资产种类**
                （摄影师产图、剪辑师产视频），所以不再单列"类型"目录——
                见 `asset_dir` 的说明。

## 两个刻意的设计

1. **role 为空时用 `_unknown`**，不抛异常。产出落盘是"最后一公里"，
   因为它失败而丢掉已经花过钱的生成结果，代价远大于目录名不好看。

2. **没有工程名时用 `未命名工程`**。用户可能没保存就直接跑——那也得有
   地方落盘；统一叫这个名字，事后一眼能认出"这批没归到任何工程"。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Literal

#: 资产类型。**不再进路径**（职业本身就区分种类，见 `asset_dir` 的说明），
#: 保留它是为了让调用点自解释、并给 `asset_path` 的扩展名兜底留依据。
AssetKind = Literal["images", "videos", "frames", "audio", "meta"]

#: 各类型默认的扩展名（`asset_path` 在文件名没带后缀时用）
_KIND_EXT: dict[str, str] = {
    "images": ".png",
    "videos": ".mp4",
    "frames": ".png",
    "audio": ".mp3",
    "meta": ".json",
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


def current_project() -> str:
    """当前**工程名**（产出路径的第二层）。

    用户指定的结构是「账号 / 项目名 / 部门 / 职业 / 会话 / 类型 / 文件」，
    项目名就是这一层。

    走 `runtime_settings.run_project()`——它在 `/api/crew/run` 时由前端传来的
    工程名写入。没有工程（用户没保存就直接跑）时退化成 `未命名工程`：
    那也得有地方落盘，而且统一叫这个名字，事后一眼能认出
    "这批没归到任何工程"。
    """
    try:
        from app.services.runtime_settings import run_project

        p = run_project()
        if p:
            return safe_segment(p)
    except Exception:  # noqa: BLE001
        pass
    return "未命名工程"


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
    project: str | None = None,
    session: str | None = None,
    create: bool = True,
) -> Path:
    """取（并按需创建）某职业的产出目录。

        asset_dir(settings.output_dir_path, "photographer", "images")
        → {outputs}/{账号}/小喵侦探-第1集/生图/photographer/run-1

    ## 为什么路径里**没有**类型目录（用户："职业和类型重复了吧"）

    之前路径是 `.../{职业}/{会话}/{类型}/`，但**每个职业基本只产一种资产**：

        摄影师 photogrepher → images      剪辑师 editor → videos
        角色设计师          → images      …

    实测 16 个职业里只有 `previs_artist`（白模预演师）会同时产
    `frames` 与 `videos`——为这一个特例给所有路径多挂一层，纯属累赘。

    而且 `frames` 是 `.png`、`videos` 是 `.mp4`，**扩展名本身就能区分**，
    放同一个目录里不会混。

    `kind` 参数保留，但目前只用于**文档与调用点可读性**（`asset_path`
    仍拿它做扩展名兜底），不再进路径。将来若真出现"同职业多类型
    且扩展名也撞"的情况，再按 `kind` 分子目录不迟。
    """
    u = safe_segment(user) if user is not None else current_user()
    proj = safe_segment(project, "未命名工程") if project is not None else current_project()
    sess = safe_segment(session) if session is not None else current_session()
    role_name = safe_segment(role, "_unknown")

    d = Path(output_dir) / u / proj / dept_for(role_name) / role_name / sess
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
    project: str | None = None,
    session: str | None = None,
    create: bool = True,
) -> Path:
    """取（并按需创建）一个资产文件的完整路径。见 `asset_dir`。

    `filename` **可以带子目录**（如 `whitebox-ab12/00000.png`）——
    白模预演师的帧要按批次分子目录，否则多批帧会同名互相覆盖。
    每一段都经 `safe_segment` 清洗，所以 `../` 这类穿越在清洗阶段就被
    打平了（`/` 与 `\\` 属非法字符，会被替换成 `_`），不会逃出产出根目录。
    """
    parts = [p for p in Path(str(filename)).parts if p not in ("", ".")]
    # 先**丢弃** `..` 段：safe_segment 会把它变成 `out`（安全但难看，
    # 会出现 `a/out/out/b.png` 这种）。丢弃之后同样无法上跳，且路径干净。
    parts = [p for p in parts if p != ".."]
    cleaned = [safe_segment(p, "out", limit=80) for p in parts]
    if not cleaned:
        cleaned = ["out"]
    stem = Path(cleaned[-1]).stem
    # 文件名没带后缀时按 kind 补一个——路径里已经没有类型目录，
    # 扩展名是区分 `frames`(png) 与 `videos`(mp4) 的最后一道。
    ext = _UNSAFE.sub("", Path(cleaned[-1]).suffix)[:12] or _KIND_EXT.get(kind, "")
    cleaned[-1] = f"{stem}{ext}"

    d = asset_dir(
        output_dir, role, kind,
        user=user, project=project, session=session, create=create,
    ) / Path(*cleaned[:-1])
    d.mkdir(parents=True, exist_ok=True)
    return d / cleaned[-1]
