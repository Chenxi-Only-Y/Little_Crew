"""架构依赖守卫。

**这个文件的作用不是测试功能，而是测试架构本身。**

背景：`little_crew` 采用分层包结构，层间依赖有严格方向（见 ARCHITECTURE.md）。
一旦某次"顺手 import"违反了方向，架构会缓慢退化成泥球，而功能测试
**不会报错** —— 因为违反依赖方向通常不影响功能。因此需要独立的守卫。

依赖铁律
--------
    cli/api  ->  runtime  ->  agent  ->  ir
                      \\->  compiler ->  capability  ->  ir
                      \\->  governance -> ir
                                 ^
                            （ir 不得 import 任何兄弟包）

三条规则
--------
  R1  下层不得 import 上层（反向依赖禁止）
  R2  `ir` 不得 import 任何兄弟包（零依赖基石）
  R3  不得使用 `sys.path` 操控（禁止隐式路径依赖）
"""

from __future__ import annotations

import ast
import os
import fnmatch
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "little_crew"

#: 分层序号（下标越小越底层）。依赖只允许"序号大 -> 序号小"。
#:
#: 编号依据是**真实依赖深度**，不是概念上的重要性：
#:   [0] 叶子层 —— 零内部依赖，可独立测试、可独立发布
#:   [1] 依赖叶子层
#:   [2] 依赖 capability 层
#:   [3]+ 上层
LAYER_ORDER: list[str] = [
    "version",      # 0 版本与标识的单一来源。零内部依赖
    "config",       # 0 运行期配置读取（`config/.env`）。零内部依赖
                    #   为什么是**层**而不是豁免：任何层都需要知道
                    #   "ComfyUI 端点在哪"，所以它必须能被任何层依赖 ——
                    #   这正是"层 0 叶子"的定义。
                    #   登记为层（而非进 ROOT_LEVEL_EXEMPT）的价值：
                    #   它的**零内部依赖**这一性质会被守卫持续检查，
                    #   而豁免只声明"别管它"。
    "ir",           # 0 基石：IR 规范、规范化、哈希。零内部依赖
    "governance",   # 1 模型授权门禁。只依赖 ir（复用其规范序列化）
    "models",       # 2 模型规格与通道声明 —— **双通道抽象的落点**
    "capability",   # 2 环境能力矩阵（唯一含 ComfyUI 节点知识的地方）
    "compiler",     # 3 IR -> 执行图/请求（依赖 capability + models + governance + ir）
    "agent",        # 3 三层调用协议（依赖 ir）
    "review",       # 4 验收层：断言算子、门禁、返工闭环
                    #   ⚠️ **必须排在 `runtime` 之前（下标更小 = 更下层）。**
                    #
                    #   `LAYER_ORDER` 的语义是**下标越大越上层**，
                    #   "依赖只允许序号大 -> 序号小"。
                    #   而 `runtime` 要**调用**门禁（把产物递过去），
                    #   所以是 runtime(下标大) -> review(下标小)，合法。
                    #
                    #   写成 review 在后（更上层）是**反的** —— 那会让
                    #   `runtime -> review` 变成"低层依赖高层"，实测被守卫拦下。
                    #
                    #   为什么方向必须是 runtime 调用 review（而不是反过来）：
                    #   **审核方不该有取产物的能力** —— 那意味着它能自己去跑生成，
                    #   而"生成方与审核方隔离"是 C3 的前提。
                    #   所以产物是**被递过来的**，不是 review 去拿的。
    "runtime",      # 4 执行层：通道实现、队列、缓存、检查点、GPU 调度
    "api",          # 5 对外接口
    "cli",          # 5 命令行
]

#: 未分层的顶层文件（包根的直接成员）。它们不属于任何"层"，
#: 只做聚合导出，因此豁免层序检查 —— 但豁免必须**显式列出**，
#: 避免新增文件因放在包根而静默绕过守卫。
ROOT_LEVEL_EXEMPT: frozenset[str] = frozenset({
    "__init__.py",
    "version.py",     # 版本与标识的单一来源；其自身已被登记为层 "version"
    # 运行期配置读取。与 `version.py` 同一处理：
    # **既登记为层（使其"零内部依赖"这一性质被持续检查），
    # 又豁免"必须在子目录里"这条形式要求** ——
    # 它只有几十行，为它建一个目录只会多一层无意义的嵌套。
    "config.py",      # 已登记为层 "config"
})

#: 允许的跨层依赖白名单：(源层, 目标层)
#: 未列出的组合一律视为违规，除非源层下标 > 目标层下标（即"高层依赖低层"）
ALLOWED_SAME_LEVEL: set[tuple[str, str]] = {
    ("compiler", "capability"),
    ("compiler", "models"),
    ("models", "capability"),   # 预留：模型声明若需引用环境事实
}

#: 明确禁止的内部包（防止有人把"临时依赖"写进基石）。
#: 注意：``version`` **不在禁止之列** —— 它是零依赖的标识模块，
#: 任何层（含 ir）引用它都是合法的。
FORBIDDEN_IN_IR: set[str] = set(LAYER_ORDER) - {"ir", "version"}


def layer_of(path: Path) -> str | None:
    """从文件路径推断所属层。

    - 位于子目录中的文件 -> 该子目录名（即层名）
    - 位于包根的直接成员 -> 若在 ``ROOT_LEVEL_EXEMPT`` 中则返回 ``None``（豁免），
      否则返回一个**不存在的层名**，使上层逻辑报出违规而非静默跳过。
    """
    rel = path.relative_to(SRC)
    if len(rel.parts) == 1:
        # 包根文件：豁免者返回 None，未豁免者返回其文件名（必然不在 LAYER_ORDER 中，
        # 从而在 main() 里被识别为"未声明归属的文件"并报出）
        if rel.parts[0] in ROOT_LEVEL_EXEMPT:
            return None
        return f"<root:{rel.parts[0]}>"
    return rel.parts[0]


def parse_imports(path: Path) -> tuple[list[str], list[str]]:
    """返回 (little_crew 内部导入的目标层列表, 是否使用了 sys.path 操控)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    targets: list[str] = []
    uses_syspath = False

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sys" or alias.name.startswith("sys."):
                    pass
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith("little_crew."):
                parts = mod.split(".")
                if len(parts) >= 2 and parts[1] in LAYER_ORDER:
                    targets.append(parts[1])
            elif mod == "sys":
                pass
        elif isinstance(node, ast.Call):
            func = node.func
            # 检测 sys.path.insert / sys.path.append
            if (isinstance(func, ast.Attribute)
                    and func.attr in ("insert", "append", "extend")
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "path"):
                uses_syspath = True

    return targets, uses_syspath


def _check_config_precedence() -> list[str]:
    """钉住配置取值的三条规则（改动会让它们变红）。

    ⚠️ 这三条都**看着像细节、错了却极难排查**，所以必须有测试：

    ① **非空才遮蔽**：空值不参与。否则把 `.env.example` 拷成 `.env`
       后满屏的 `KEY=` 会挡住项目外秘密文件里的真值 ——
       表现是"明明配了却读不到"。
    ② **`config/.env` 优先于秘密文件**：本机配置要能覆盖共享凭据。
    ③ **秘密文件必须能缺失**：缺它应当走默认值，
       **不能让所有命令（含 `--help`）都失败**。
    """
    import tempfile
    from pathlib import Path as _P

    sys.path.insert(0, str(ROOT / "src"))
    from little_crew import config as C

    bad: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        tmp = _P(td)
        (tmp / "config").mkdir()
        (tmp / "pyproject.toml").write_text("", encoding="utf-8")
        secrets = tmp / "secrets.env"
        secrets.write_text("K=realsecret\nONLY=sec\n", encoding="utf-8")

        saved = os.environ.pop("K", None), os.environ.pop("ONLY", None)
        os.environ["LC_SECRETS_FILE"] = str(secrets)
        try:
            # ① 空值不遮蔽
            (tmp / "config" / ".env").write_text("K=\n", encoding="utf-8")
            got = C.env_value("K", "dflt", root=tmp)
            if got != "realsecret":
                bad.append(
                    f"空值遮蔽了秘密文件：K 取到 {got!r}，应为 'realsecret'。"
                    f"`config/.env` 里的 `K=` 是**空串**，不该赢过秘密文件的真值"
                    f"（否则 .env.example 拷成 .env 就会让配置失效）")

            # ② .env 的非空值优先
            (tmp / "config" / ".env").write_text("K=fromdotenv\n",
                                                 encoding="utf-8")
            got = C.env_value("K", "dflt", root=tmp)
            if got != "fromdotenv":
                bad.append(
                    f"`config/.env` 没有优先于秘密文件：K 取到 {got!r}，"
                    f"应为 'fromdotenv'（本机配置要能覆盖共享凭据）")

            # ③ 秘密文件缺失不抛异常
            os.environ["LC_SECRETS_FILE"] = str(tmp / "nope.env")
            try:
                got = C.env_value("ONLY", "dflt", root=tmp)
            except Exception as e:                              # noqa: BLE001
                bad.append(f"秘密文件缺失时抛了异常：{type(e).__name__}: {e}"
                           f"（应当安静地走默认值）")
            else:
                if got != "dflt":
                    bad.append(f"秘密文件缺失时应取默认值，实取 {got!r}")
        finally:
            os.environ.pop("LC_SECRETS_FILE", None)
            for k, v in zip(("K", "ONLY"), saved):
                if v is not None:
                    os.environ[k] = v
    return bad


# ---------------------------------------------------------------------------
# 存储守卫 ⑥：`.gitignore` 不得误伤源码包
# ---------------------------------------------------------------------------
#
# ## 这条守卫是怎么来的
#
# 2026-10-17 真实事故：`.gitignore` 里写了裸的 `models/`，
# 而 gitignore 的**裸名字会匹配任意深度** —— 于是
# `src/little_crew/models/` 被整体忽略，**4 个源文件从未入库**。
# 任何人 clone 下来 `import little_crew.models` 直接失败。
#
# 更糟的是它**不会报错**：`git status` 干净、本机测试全过、
# `git ls-files | grep models` 空空如也却被误判成"空目录"。
# 是 `git archive` 干净快照验证才抓到的。
#
# ## 为什么必须机器守
#
# 因为这类错误的**表现形式是"什么都没发生"** ——
# 只有把"规则会不会匹配到源码包"这件事跑一遍，才能看见它。
# 靠人读 .gitignore 是看不出来的（`models/` 看起来完全合理）。
#
# ## 判据
#
# `.gitignore` 里**未锚定**（不以 `/` 开头）的目录规则，
# 不允许匹配到 `src/little_crew/` 下的任何包目录名。

def _check_gitignore_shadows_src() -> list[str]:
    """`.gitignore` 里未锚定的规则不得匹配到源码包目录。**返回违规说明列表。**

    沿用本文件既有约定：返回 list[str]，空列表 = 通过。
    """
    gi = ROOT / ".gitignore"
    if not gi.is_file():
        return [".gitignore 不存在，无法校验"]

    src = ROOT / "src" / "little_crew"
    if not src.is_dir():
        return [f"找不到源码包目录 {src}"]
    pkgs = sorted(p.name for p in src.iterdir()
                  if p.is_dir() and p.name != "__pycache__")
    if not pkgs:
        return ["源码包目录为空，无法校验"]

    patterns = [ln.strip() for ln in gi.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.strip().startswith(("#", "!"))]

    problems: list[str] = []
    for pat in patterns:
        if pat.startswith("/"):
            continue                     # 已锚到根 -> 不会命中 src/ 下面
        base = pat.rstrip("/")
        if not base or "/" in base:
            continue                     # 带子路径的不会命中包目录
        for pkg in pkgs:
            if pkg == base or fnmatch.fnmatchcase(pkg, base):
                problems.append(
                    f".gitignore 规则 `{pat}` 会误伤 src/little_crew/{pkg}/"
                    f"（未锚定 -> 匹配任意深度）"
                    f"；应改成 `/{pkg}/` 只忽略根目录的同名目录")
    return problems

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    if not SRC.exists():
        print(f"错误：源码目录不存在 {SRC}")
        return 2

    violations: list[str] = []
    checked = 0
    edges: set[tuple[str, str]] = set()
    layers_present: dict[str, list[str]] = {}

    for path in sorted(SRC.rglob("*.py")):
        src_layer = layer_of(path)
        rel = path.relative_to(SRC)

        # 包根的直接成员：豁免者（__init__.py 等）跳过；未豁免者报违规，
        # 避免新增文件因放在包根而静默绕过整个守卫。
        if src_layer is None:
            continue
        if src_layer not in LAYER_ORDER:
            violations.append(
                f"未声明归属：{rel} 位于包根但不在 ROOT_LEVEL_EXEMPT 中。"
                f"请把它移入某个层，或显式加入豁免清单并说明理由"
            )
            continue

        checked += 1
        layers_present.setdefault(src_layer, []).append(path.name)

        targets, uses_syspath = parse_imports(path)

        # R3：禁止 sys.path 操控
        if uses_syspath:
            violations.append(f"R3 违反（sys.path 操控）: {rel}")

        for dst_layer in targets:
            if dst_layer == src_layer:
                continue
            edges.add((src_layer, dst_layer))

            # R2：ir 不得依赖任何兄弟包
            if src_layer == "ir" and dst_layer in FORBIDDEN_IN_IR:
                violations.append(f"R2 违反（ir 依赖了 {dst_layer}）: {rel}")
                continue

            src_i = LAYER_ORDER.index(src_layer)
            dst_i = LAYER_ORDER.index(dst_layer)

            # 高层 -> 低层：合法
            if src_i > dst_i:
                continue
            # 同层：仅白名单
            if src_i == dst_i and (src_layer, dst_layer) in ALLOWED_SAME_LEVEL:
                continue
            # 低层 -> 高层：违规
            violations.append(
                f"R1 违反（反向依赖 {src_layer} -> {dst_layer}）: {rel}"
            )

    # ---------- 输出 ----------
    print("=" * 74)
    print("架构依赖守卫")
    print("=" * 74)
    print(f"扫描源文件: {checked}")
    print()
    print("已存在的层:")
    for layer in LAYER_ORDER:
        if layer in layers_present:
            files = ", ".join(sorted(layers_present[layer]))
            print(f"  [{LAYER_ORDER.index(layer)}] {layer:12} {files}")
        else:
            print(f"  [{LAYER_ORDER.index(layer)}] {layer:12} (未实现)")
    print()
    print("层间依赖边（应全部为 高层 -> 低层）:")
    if not edges:
        print("  （无）")
    for src_layer, dst_layer in sorted(edges):
        src_i, dst_i = LAYER_ORDER.index(src_layer), LAYER_ORDER.index(dst_layer)
        ok = src_i > dst_i or (src_layer, dst_layer) in ALLOWED_SAME_LEVEL
        print(f"  {'OK ' if ok else 'BAD'} {src_layer:12} -> {dst_layer}")
    print()

    if violations:
        print(f"发现 {len(violations)} 处架构违规：")
        for v in violations:
            print(f"  X {v}")
        print("=" * 74)
        print("结果：FAIL")
        return 1

    print("结果：PASS（依赖方向全部合规）")
    print("=" * 74)
    return 0


# ==========================================================================
# 存储规范守卫
# ==========================================================================

ROOT = Path(__file__).resolve().parents[1]

#: 项目根**只允许**出现的条目。依据 `STRUCTURE.md` §1 目录职责表。
#:
#: ⚠️ **为什么这条检查必须存在**：`STRUCTURE.md` 早就写清了目录职责，
#: 但它只是文档 —— 2026-10-12 那轮 P3 验证里，我仍然在项目根堆了
#: 10 个一次性探针脚本（`_probe*.py` / `_chk*.py`）、自创了一个
#: `outputs/previs_explain/`（结构表里根本没这个位置）、
#: 并把实验中间产物留在了 `outputs/` 下。
#:
#: **规范没有机器检查，就等于没有规范。** 这条守卫把它变成会失败的测试。
ALLOWED_ROOT_ENTRIES: frozenset[str] = frozenset({
    # 目录（§1 目录职责表逐条对应）
    "src", "tests", "config", "docs", "research", "outputs", "logs",
    # 前代实现（v0.3.0，LangGraph 版）。
    #
    # ⚠️ 它**不是本项目的一部分**，是**同仓库的历史归档**。
    # 2026-10-12 把 v0.4.0 提为仓库主体时，v0.3.0 整体移到这里。
    # 它有自己的 src/tests/docs（在 legacy/ 内部），但**不参与本项目的
    # 层序检查与测试** —— 守卫只认本项目自己的 src/ 与 tests/。
    #
    # ⚠️ **本机真源（ds.8）里没有这个目录**，它只存在于仓库副本（ds.6）。
    # 白名单必须包含它，否则**同一份文件在仓库里会挂、在真源里却过** ——
    # 而那正是踩过的坑（同步文件时把这条冲掉了，仓库副本立刻 FAIL）。
    # 白名单多列一个不存在的目录是**无害**的：检查是"根里的条目 ∈ 白名单"，
    # 不是"白名单 ⊆ 根里的条目"。
    "legacy",
    # 入库的顶层文件
    "README.md", "STRUCTURE.md", "ARCHITECTURE.md", "CHANGELOG.md",
    "pyproject.toml", ".gitignore",
    # 许可：`README.md` 与 `pyproject.toml` 都声明 Apache-2.0，
    # 而**没有 LICENSE 文件的公开仓库在法理上等于未授予任何许可** ——
    # 三者必须一致，所以它必须在白名单里（2026-10-12 补）
    "LICENSE",
    # 允许存在但不入库的本地状态
    #
    # ⚠️ 三个 `*_cache` **必须在这里**：它们由工具**自动创建在项目根**，
    # 不是人手放的。不进白名单的话，开发机上跑一次 pytest 就会让守卫失败 ——
    # 而那是**假警报**，假警报会训练人忽略守卫，比没有守卫更糟。
    ".venv", ".idea", ".vscode", ".git",
    ".pytest_cache", ".ruff_cache", ".mypy_cache",
})

#: 一次性脚本的命名特征 —— 出现在项目里即为违规。
#:
#: 这些前缀是**我实际用过的**：临时验证脚本一律以 `_` 开头，
#: 用完即删。写进守卫是为了让"忘了删"变成测试失败而不是靠记性。
TEMP_FILE_PREFIXES: tuple[str, ...] = ("_", "tmp_", "scratch_")


def _check_storage() -> list[str]:
    """检查项目根与 outputs/ 是否符合存储规范。"""
    bad: list[str] = []

    # ---- ① 项目根只能有白名单里的条目 ----
    for entry in sorted(ROOT.iterdir()):
        name = entry.name
        if name in ALLOWED_ROOT_ENTRIES:
            continue
        # 秘密目录有**专门**的提示：说"一次性脚本"会把人引到错的方向
        # （他该做的是把凭据挪出项目，不是"用完即删"）。
        if "secret" in name.lower() or name in (".env", ".env.local"):
            bad.append(
                f"项目根出现疑似秘密文件 {name!r} —— 秘密凭据必须放"
                f"项目**外**（如 D:\\AI\\_secrets），见 STRUCTURE.md §2")
        elif name.startswith(TEMP_FILE_PREFIXES):
            bad.append(
                f"项目根有一次性脚本 {name!r} —— 验证脚本不入库，"
                f"应放项目外（如 D:\\AI\\_dsh_tmp）或用完即删")
        elif name.endswith(".py"):
            bad.append(
                f"项目根有 Python 文件 {name!r} —— §1：业务代码进 "
                f"src/little_crew/，测试进 tests/，两者都不该在根目录")
        else:
            bad.append(
                f"项目根有未登记的条目 {name!r} —— §1 目录职责表里没有它。"
                f"若是新增的顶层文件，请先更新 STRUCTURE.md §1 与白名单")

    # ---- ② outputs/ 下不得出现"实验中间物" ----
    #
    # `outputs/` 本身是合法的（协议规定的产物目录），但**一次验证的中间产物
    # 不该长期留在项目里** —— 它们既不入库，又会让 `outputs/` 无限膨胀，
    # 且下次跑同一实验时会与旧结果混淆。
    out = ROOT / "outputs"
    if out.is_dir():
        for entry in sorted(out.iterdir()):
            if entry.name == ".gitkeep" or not entry.name.endswith("_nowm"):
                continue
            bad.append(
                f"outputs/{entry.name} 是对照实验的中间产物 —— "
                f"证据图应归档到 research/，中间产物应清理")

    # ---- ②b outputs/ 顶层只应有子目录与 .gitkeep ----
    #
    # 报告类文件（*.json / *.md）**属于证据，归 research/**。
    # 留在 outputs/ 的后果：它不入库，于是被引用的数字**随 outputs/ 清理而消失**。
    # 这条是实际犯过的错：m5_gate_report.json 同时存在两处，
    # 而 outputs/ 那份随时可能被清掉。
    if out.is_dir():
        for entry in sorted(out.iterdir()):
            if entry.is_dir() or entry.name == ".gitkeep":
                continue
            bad.append(
                f"outputs/{entry.name} 是文件 —— outputs/ 只放一次跑的产物。"
                f"若它是被文档引用的证据，请归 research/")

    # ---- ③ research/ 里不得有自己的 Python 包 ----
    #
    # §1：research/ 是**只读归档**，"任何被代码 import 的东西"禁止放这里。
    #
    # ⚠️ **必须排除第三方源码快照**（`src-*/`）：那些目录按 §1 与 `.gitignore`
    # 是**明确授权保留**的证据（可重新下载、属他人版权），里面有 `__init__.py`
    # 完全正常 —— 本项目的包结构禁令管的是**我们自己的**代码。
    res = ROOT / "research"
    if res.is_dir():
        for p in res.rglob("*.py"):
            parts = p.relative_to(res).parts
            if any(seg.startswith("src-") for seg in parts):
                continue                      # 第三方快照，跳过
            if p.name == "__init__.py":
                bad.append(
                    f"research/ 里有包结构文件 {p.relative_to(ROOT)} —— "
                    f"research 不是 Python 包（第三方快照 src-*/ 已豁免）")

    # ---- ④ 秘密凭据必须住在项目**外** ----
    #
    # `config/.env` 虽然在项目内且被 `.gitignore` 排除，但它位置在项目里 ——
    # 而项目迟早会被打包、压缩、复制、分享，**凭据的安全性就只依赖
    # "`.gitignore` 恰好写对了"**。一次 `git add -f`，或一次"拷走整个目录"
    # 就漏了。项目外那份**从位置上就不可能进版本库**。
    #
    # 所以这条守卫检查的不是"值有没有泄露"，而是**位置对不对** ——
    # 位置对了，泄露在结构上就不可能发生。
    secrets = ROOT / "_secrets"
    if secrets.exists():
        bad.append(
            "项目根出现 _secrets/ —— 秘密凭据必须放项目**外**"
            "（如 D:\\AI\\_secrets）。放在项目里，不进版本库就只靠 "
            ".gitignore 记得写对，而项目迟早会被整体拷走/打包")

    # 默认秘密文件的位置也必须是项目外的绝对路径
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from little_crew.config import secrets_path
        sp = secrets_path()
        if sp.is_relative_to(ROOT):
            bad.append(
                f"config.secrets_path() 指向项目内（{sp}）—— "
                f"默认值应是项目外的绝对路径（见 config.py 的 "
                f"_DEFAULT_SECRETS）")
    except Exception:                                          # noqa: BLE001
        # 取不到不算违规：`config` 的导入失败由别的套件负责报。
        # 守卫只报**它自己**能证明的问题，不制造假警报。
        pass

    # ---- ⑤ `.env` 必须在 .gitignore 里 ----
    #
    # 这条看似多余（写了不就行了），但它防的是**有人改 .gitignore 时
    # 顺手删掉**。而删掉的后果不会立刻显形 —— 要等到某次 `git add -A`
    # 之后，凭据已经在历史里了。
    gi = ROOT / ".gitignore"
    if gi.is_file() and (ROOT / "config" / ".env").is_file():
        text = gi.read_text(encoding="utf-8", errors="replace")
        if ".env" not in text:
            bad.append(
                "`.gitignore` 里没有 `.env` —— 而 `config/.env` 存在。"
                "凭据文件必须被忽略，否则一次 git add -A 就进历史了")

    return bad


def storage_main() -> int:
    """存储规范守卫的独立入口。"""
    print("=" * 74)
    print("存储规范守卫（依据 STRUCTURE.md §1 目录职责表 + §2.0 凭据政策）")
    print("=" * 74)
    print(f"项目根: {ROOT}")
    print(f"白名单: {len(ALLOWED_ROOT_ENTRIES)} 个条目")
    print()
    problems = (_check_storage() + _check_config_precedence()
                + _check_gitignore_shadows_src())
    if not problems:
        print("结果：PASS（项目根合规 · 凭据在项目外 · 配置取值规则成立"
              " · .gitignore 未误伤源码包）")
        print("=" * 74)
        return 0
    print(f"发现 {len(problems)} 处违规：")
    for p in problems:
        print(f"  X {p}")
    print("=" * 74)
    print("结果：FAIL")
    return 1


if __name__ == "__main__":
    _rc = main()
    if _rc == 0:
        print()
        _rc = storage_main()
    raise SystemExit(_rc)
