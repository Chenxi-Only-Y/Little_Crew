"""产出资产路径规则的测试。

对应需求："不同职业生成的东西，视频图片资产都独立放"。

背景：改动之前各处的产出路径**各写各的、互相不一致**——
图片没有类型目录、视频有 `video/`、云端视频适配器连部门/职业都丢了、
白模视频落在 `outputs/whitebox/{uuid}/`。同一部片子的资产散在四处，
想"把某个职业的产出单独打包"做不到。

现在 `app/services/asset_paths.py` 是**唯一**入口，这个文件把它的约定钉住。
"""

from __future__ import annotations

from app.services import asset_paths as ap


# ------------------------------------------------------------------
# 1) 目录结构
# ------------------------------------------------------------------

def test_layout_is_account_project_dept_role_session_kind(tmp_path):
    """七段结构：账号/项目名/部门/职业/会话/类型/文件（用户指定的顺序）。"""
    p = ap.asset_path(tmp_path, "photographer", "images", "shot_01.png",
                      user="tester", project="小喵侦探", session="run-1")
    parts = p.relative_to(tmp_path).parts
    assert parts[0] == "tester", "第一段必须是账号"
    assert parts[1] == "小喵侦探", "第二段必须是**项目名**"
    # 部门名来自注册表（app/registry/roles.py），photographer 属「生图」
    assert parts[2] == "生图", f"第三段应为部门，实际 {parts[2]}"
    assert parts[3] == "photographer", "第四段必须是职业"
    assert parts[4] == "run-1", "第五段必须是会话"
    assert parts[5] == "images", "第六段必须是资产类型"
    assert parts[6] == "shot_01.png"


def test_missing_project_falls_back_to_unnamed(tmp_path):
    """没有工程名时用「未命名工程」兜底，不能抛异常。

    用户可能没保存就直接跑——那也得有地方落盘；统一叫这个名字，
    事后一眼能认出"这批没归到任何工程"。
    """
    d = ap.asset_dir(tmp_path, "photographer", "images",
                     user="t", project="", session="s")
    assert "未命名工程" in d.parts


def test_different_projects_are_separate(tmp_path):
    """同一职业在不同工程下的产出必须分开——这是"按项目归档"的核心。"""
    a = ap.asset_dir(tmp_path, "photographer", "images", user="t", project="片A", session="s")
    b = ap.asset_dir(tmp_path, "photographer", "images", user="t", project="片B", session="s")
    assert a != b
    assert a.relative_to(tmp_path).parts[1] == "片A"
    assert b.relative_to(tmp_path).parts[1] == "片B"


def test_creates_directory_on_disk(tmp_path):
    p = ap.asset_path(tmp_path, "photographer", "images", "a.png",
                      user="tester", project="p", session="s1")
    assert p.parent.is_dir(), "目录必须真的被创建出来（否则写文件时才炸）"


# ------------------------------------------------------------------
# 2) "独立放"的三个维度：职业 / 类型 / 会话
# ------------------------------------------------------------------

def test_different_roles_do_not_share_dirs(tmp_path):
    """不同职业的产出必须落在不同目录——这是"独立放"的核心。"""
    a = ap.asset_dir(tmp_path, "photographer", "images", user="t", project="p", session="s")
    b = ap.asset_dir(tmp_path, "script_editor", "images", user="t", project="p", session="s")
    assert a != b, "两个职业不能共用同一个目录"
    assert a.relative_to(tmp_path) != b.relative_to(tmp_path)


def test_images_and_videos_are_separate(tmp_path):
    """同一个职业的图片与视频必须分开存放。"""
    img = ap.asset_dir(tmp_path, "photographer", "images", user="t", project="p", session="s")
    vid = ap.asset_dir(tmp_path, "photographer", "videos", user="t", project="p", session="s")
    assert img != vid
    assert img.name == "images"
    assert vid.name == "videos"
    # 同一棵树里，只有最后一段不同
    assert img.parent == vid.parent


def test_different_sessions_are_separate(tmp_path):
    a = ap.asset_dir(tmp_path, "photographer", "images", user="t", session="s1")
    b = ap.asset_dir(tmp_path, "photographer", "images", user="t", session="s2")
    assert a != b


def test_different_users_are_separate(tmp_path):
    a = ap.asset_dir(tmp_path, "photographer", "images", user="u1", project="p", session="s")
    b = ap.asset_dir(tmp_path, "photographer", "images", user="u2", project="p", session="s")
    assert a != b


# ------------------------------------------------------------------
# 3) 名称清洗（Windows 上目录名非法会让 mkdir 直接失败）
# ------------------------------------------------------------------

def test_safe_segment_strips_windows_reserved_chars():
    assert ap.safe_segment("a<b>c") == "a_b_c"
    assert ap.safe_segment('x/y\\z') == "x_y_z"
    assert ap.safe_segment("proj:1") == "proj_1"
    assert ap.safe_segment('q?*"|') == "q____"


def test_safe_segment_handles_reserved_device_names():
    """CON / PRN / NUL / COM1 在 Windows 上不能作目录名。"""
    assert ap.safe_segment("CON") == "_CON"
    assert ap.safe_segment("nul") == "_nul"
    assert ap.safe_segment("COM1") == "_COM1"
    # 普通名字不要被误伤
    assert ap.safe_segment("console") == "console"


def test_safe_segment_falls_back_when_empty():
    assert ap.safe_segment("") == "_unknown"
    assert ap.safe_segment("   ") == "_unknown"
    assert ap.safe_segment("...") == "_unknown"
    assert ap.safe_segment("", fallback="local") == "local"


def test_safe_segment_truncates_long_names():
    long = "x" * 200
    assert len(ap.safe_segment(long)) <= 60


# ------------------------------------------------------------------
# 4) 不抛异常：产出落盘是"最后一公里"
# ------------------------------------------------------------------

def test_unknown_role_does_not_raise(tmp_path):
    """未知职业不能抛异常，要落到"综合"部门下。

    产出落盘失败会丢掉**已经花过钱**的生成结果，代价远大于目录名不好看。
    未知职业的部门由 `org.dept_for_role` 兜底成"综合"（不是 `_unknown`——
    那是**职业名本身**为空时的兜底，两者不同层级）。
    """
    d = ap.asset_dir(tmp_path, "no_such_role_xyz", "images", user="t", project="p", session="s")
    assert d.is_dir()
    assert d.name == "images"
    # 职业名仍然被保留（方便事后知道是谁产的），部门用兜底值
    assert "no_such_role_xyz" in d.parts


def test_empty_role_does_not_raise(tmp_path):
    """role 为空也不能抛，退化成 `_unknown`。"""
    d = ap.asset_dir(tmp_path, "", "images", user="t", project="p", session="s")
    assert d.is_dir()
    assert "_unknown" in d.parts


def test_asset_path_keeps_extension(tmp_path):
    p = ap.asset_path(tmp_path, "photographer", "videos", "out.mp4",
                      user="t", project="p", session="s")
    assert p.suffix == ".mp4"
    assert p.name == "out.mp4"


def test_asset_path_sanitizes_filename(tmp_path):
    p = ap.asset_path(tmp_path, "photographer", "images", "a:b?.png",
                      user="t", project="p", session="s")
    assert p.suffix == ".png"
    assert ":" not in p.name and "?" not in p.name


def test_no_session_falls_back_to_hour_bucket(monkeypatch, tmp_path):
    """没有会话时按**小时**分桶，而不是每个 shot 一个目录。

    旧实现在会话缺失时用 `no-session-{shot_id}`，同一批独立生成会散成
    很多目录。按小时分桶至少让同一小时的产出聚在一起。
    """
    monkeypatch.setattr(ap, "current_session", lambda: "no-session-20260101-12")
    d = ap.asset_dir(tmp_path, "photographer", "images", user="t")
    assert "no-session-20260101-12" in d.parts
