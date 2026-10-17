# 项目结构与配置规范

> **本文是项目的强制约定。** 新增任何文件前先查本文确定它该放哪；
> 本文没有覆盖的情况，先在本文补一条规则，再动文件。
>
> 制定日期：2026-10-11 · 适用范围：`Little_Crew/` 全目录

---

## 0. 两条总原则

### 原则 1：配置不硬编码，但也不过度外置

**判断标准是一句话：这个值会不会因"换一台机器 / 换一个人"而改变？**

| 会变 → 外置到配置文件 | 不会变 → 写在代码里 |
|---|---|
| 文件路径、模型清单、API 端点 | 编译规则表（`DEFAULT_RULES`） |
| 端口、并发数、分辨率档位、预算上限 | IR 的 Schema 定义 |
| API Key、数据库连接串 | 断言算子的实现 |
| 选哪个模型、走本地还是云端 | 授权事实目录（`LICENSE_CATALOG`） |

> ⚠️ **反例警示**：把"编译规则表"外置成配置文件是**错误的**——它是平台的核心逻辑，
> 不是部署参数。外置会让"引擎升级导致行为静默变化"且无法用测试固定。
> 反过来，把"底模文件名"硬编码在代码里也是错误的——那明显是部署参数。

### 原则 2：单一分布 + 单一 venv

项目是**一个** Python 分发（distribution），不是微服务集合。

- 所有包共用一个 `pyproject.toml`
- 所有代码共用一个 venv（`.venv/`，已存在且 `.idea/` 已指向它）
- 核心层（`ir` / `governance` / `capability` / `compiler` / `agent`）**运行期零第三方依赖**

引入第三方依赖需满足两个条件之一：① 属于 `runtime` 及以上层；② 仅用于开发/测试。
并且必须同步更新 `pyproject.toml` 的 `dependencies` 或 `[project.optional-dependencies]`。

---

## 1. 目录职责表（唯一权威）

```
Little_Crew/
├── .venv/                        【本机依赖环境】不入库
├── .idea/                        【IDE 配置】不入库
├── src/little_crew/               【源码】唯一的代码位置
│   ├── version.py                【版本单一来源】三类版本的定义与边界
│   ├── ir/  governance/          [0] 叶子层：零内部依赖
│   ├── capability/               [1]
│   ├── models/  compiler/        [2]/[3]
│   ├── agent/                    [3]  名册、协议、编排、定妆表
│   ├── review/  runtime/         [4]  门禁 / 执行
│   └── api/  cli/                [5]  对外接口
├── tests/                        【测试脚手架】不含业务逻辑
├── config/                       【配置】只放"会随部署变化"的配置
├── docs/                         【文档】设计、调研、规范
├── research/                     【调研归档】只读，不参与构建
│   ├── github-survey/            第三方项目源码快照（src-*/ 不入库）
│   ├── blender-previs/           白模预演脚本 + p3-evidence/ 证据图
│   ├── h3-controlnet/            官方样片归档
│   ├── comfyui-internals/        ComfyUI 内部机制笔记
│   ├── model-survey/             模型生态笔记
│   ├── consistency-calibration/  L2 标定实验 + degradations/ 退化图
│   ├── review-evidence/          M5 门禁测量脚本与报告
│   ├── llm-crew/                 真实 LLM 职位跑通下游的证据（图 + 运行报告）
│   ├── threshold-calibration/    通过阈值的 10 次实测标定（raw/summary/README）
│   └── cast-consistency/         新算子的真实发生率测量（163 镜，0% 命中）
├── outputs/                      【产物】不入库（证据图不放这里，见 §1.0.2）
├── logs/                         【日志】不入库
├── ARCHITECTURE.md               【架构总纲】代码怎么分层、依赖方向
├── STRUCTURE.md                  【本文】文件该放哪、配置怎么写
├── CHANGELOG.md                  【变更记录】本版做了什么、有何限制
├── README.md                     【入口】给人看的门面
├── pyproject.toml                【依赖与工具配置】不接受其他同类文件
├── .gitignore
├── .gitattributes             【换行符策略】文本一律 LF，二进制显式声明
└── LICENSE
```

### 1.0 这一节由**机器强制**执行

⚠️ **规范没有机器检查，就等于没有规范。**

2026-10-12 那轮 P3 验证里，我在项目根堆了 10 个一次性探针脚本
（`_probe*.py` / `_chk*.py`）、自创了一个 `outputs/previs_explain/`
（本节目录表里根本没有这个位置）、并把实验中间产物留在了 `outputs/` 下
—— **而 `STRUCTURE.md` 早就写清了职责**。文档没能拦住我，因为它是文档。

因此加了两道守卫，都在 `tests/test_architecture.py` 里，
**任一失败即整个测试套件失败**：

| 守卫 | 检查什么 |
|---|---|
| `storage_main()` | ① 项目根只能有白名单里的条目（18 个，含 `LICENSE`）<br>② `outputs/` 下不得留 `*_nowm` 这类对照实验中间物<br>③ `research/` 里不得有自己的 Python 包（第三方快照 `src-*/` 豁免） |

同一文件里既有**依赖方向守卫**（R1–R3），又有**存储规范守卫** ——
两者的共同点是：**它们检查的不是功能，而是项目本身是否还在正轨上。**

### 1.0.1 一次性脚本放哪

验证脚本（探针、对照实验、临时拼图）**不入库，也不留在项目里**：

| 去处 | 说明 |
|---|---|
| ✅ **项目外**（如 `D:\AI\_dsh_tmp\`） | 推荐。用完即弃，与项目完全隔离 |
| ✅ **`research/<主题>/`** | **仅当**它值得复现（如 `blender-previs/*.py`），且需配 README 说明来源与用途 |
| ❌ 项目根 | 守卫会直接判失败 |
| ❌ `src/` | §1 明确规定禁止 |

**判据**：*这个脚本下个月还有人会跑吗？* 不会 → 丢项目外。

### 1.0.2 证据图放哪（`outputs/` 还是 `research/`）

| 类型 | 去处 | 理由 |
|---|---|---|
| **一次跑的产物**（成片、帧序列） | `outputs/` | 不入库，可随时重生成 |
| **支撑结论的证据**（对照实验图） | `research/<主题>/<证据名>/` | 结论要能**复核** |

分界线是 **"它是否被写进了文档作为判定依据"**。

> 实例：`research/blender-previs/p3-evidence/` —— 那 8 张对照图是
> 策划书 §3.8 与 CHANGELOG 里多项结论（"控制有效"、"灰度优于深度"、
> "模糊控制信号最优"）的**唯一实物依据**。
> 它们**都靠看图得出**，不是指标算出来的 ——
> 恰恰因为那轮里两个自动指标都是错的（见该目录 README）。
> 只留文字结论、不留原始图，等于结论无法复核。

| 目录 | 允许放 | **禁止**放 |
|---|---|---|
| `src/little_crew/` | Python 源码，按 `ARCHITECTURE.md` 的分层组织 | 配置文件、数据文件、临时脚本 |
| `tests/` | 测试脚手架（runner、架构守卫、集成测试） | 业务逻辑（业务逻辑必须在 `src/` 里才可被复用） |
| `config/` | 运行期配置与模板（见 §2） | 密钥真实值、代码常量 |
| `docs/` | Markdown 文档，序号前缀 | 代码、配置、大体积二进制 |
| `research/` | 调研原文与第三方源码快照（**只读归档**） | 任何被代码 import 的东西 |
| `outputs/` | 生成的图片/视频/报告 | 任何需要入库的内容 |
| `logs/` | 运行日志 | — |

> **`research/` 的定位**：它是**证据留存**，不是代码。里面的第三方源码快照
> （`research/github-survey/src-*/`）已加入 `.gitignore`——它们是可重新下载的派生物，
> 且属于他人版权内容。调研报告本身（`docs/03-*`）入库。

### 1.1 外部工具链：一律放项目外

**判据**：用原则 1 的那句话 —— **这个位置会不会因"换一台机器 / 换一个人"而改变？**
会变的东西是**部署环境**，不是项目内容。

| 东西 | 放哪 | 理由 |
|---|---|---|
| **ComfyUI 应用本体** | ❌ 项目外，如 `D:\AI\ComfyUI_windows_portable` | 第三方工具，解压后 3.5~4 GB，可随时重装 |
| **模型权重** | ❌ 项目外 | 单个数 GB ~ 数十 GB；换机器就得重下 |
| **FFmpeg / 7-Zip / 独立 Python 等** | ❌ 项目外 | 系统级工具 |
| **ComfyUI 端点地址** | ✅ `config/.env` 的 `COMFYUI_ENDPOINT` | 属配置第 ①/② 类，随部署变化且含凭据 |
| **`/object_info` 探测结果快照** | ✅ `config/env/<环境名>.json` | 属配置第 ④ 类。**这是"项目内不存工具、但可复现"的关键** |

**三条硬理由**（不是洁癖）：

| # | 理由 |
|---|---|
| **1** | 项目迟早初始化 git。外部工具若在项目内且漏配 `.gitignore`，一次 `git add .` 会把数 GB 二进制写进仓库历史，**几乎无法干净移除**。放外面则从根上不可能发生 |
| **2** | **路径不能含中文。** 本机路径与仓库目录**现已统一为 `Little_Crew`**（纯 ASCII）。此前项目名含 `·` 与中文，而 comfyui 的 Python 生态对非 ASCII 路径处理**不总是可靠** —— 当时只能靠"外部工具放项目外"绕开，现在从根上消除了。2026-10-12 由 `小剧组·Little_Crew` 改名而来 |
| **3** | 换机器/重装工具时不应触及项目目录。项目目录只应因**项目本身变化**而变化 |

> **可复现性从哪来**：项目里不存 ComfyUI，但存**那个环境的指纹快照**。
> 因此 `capability.py` 的测试无需真机即可运行 —— 它们用快照与夹具。

> **外部运行时与本项目 venv 的关系**：ComfyUI 便携版自带内嵌 Python，
> 与本项目 `.venv`、`local-gen\.venv`、系统 `D:\Python` **完全隔离**，互不污染。
> 本项目**不把 ComfyUI 作为依赖安装**，只通过 HTTP 与它通信。

---

## 2. 配置放哪：五类配置，五个位置

**这是本文最核心的部分。任何配置都能在这五类里找到归属。**

| 类型 | 位置 | 命名 | 是否入库 | 例子 |
|---|---|---|---|---|
| **①a 秘密凭据（首选）** | **项目外** `D:\AI\_secrets\` | `little-crew.env` | ❌ 且**位置在项目外** | `LLM_API_KEY=sk-...` |
| **①b 部署专用覆盖** | `config/.env`（可选） | 固定名 `.env` | ❌ 绝不入库 | `COMFYUI_ENDPOINT=http://127.0.0.1:8188` |
| **② 秘密模板** | `config/.env.example` | 固定名 | ✅ 入库（值为空或占位） | `LLM_API_KEY=` |
| **③ 项目可调参数** | `config/project.toml` | `*.toml` | ✅ 入库 | 分辨率档位、单镜时长上限、预算、并发数、种子基点 |
| **④ 环境能力快照** | `config/env/<name>.json` | `<环境名>.json` | ✅ 入库 | `local-4060.json`（`/object_info` 的探测结果，用于离线测试） |
| **⑤ 代码常量** | 源码内该层的 `constants` 模块 | — | ✅ 入库 | 编译规则表、IR Schema、授权事实目录、断言算子 |

### 2.0 ⚠️ 凭据为什么放**项目外**（2026-10-16 修订）

本文原先写"秘密凭据 → `config/.env`，靠 `.gitignore` 排除"。
**那个方案不够** —— 它把凭据的安全性**押在"`.gitignore` 恰好写对了"**上：

| 风险 | `.gitignore` 挡得住吗 |
|---|---|
| `git add -f config/.env` | ❌ 挡不住（`-f` 就是强制） |
| 把整个项目目录**拷给别人** / 压成 zip | ❌ 完全不相关 —— 文件被原样带走 |
| 打包进 Docker 镜像 | ❌ 要另一套 `.dockerignore` 记得写对 |
| 某次重构 `.gitignore` 时手滑删掉一行 | ❌ 而且**不会立刻显形**，要等下次 `git add -A` |

**所以改成：凭据放项目外，从位置上就不可能进版本库。**

```
D:\AI\_secrets\little-crew.env     ← 凭据（本项目之外）
D:\AI\_secrets\little-crew.env.example  ← 模板（可入库/可分享）
```

**加载方式**（`src/little_crew/config.py`）：

| 优先级 | 来源 | 用途 |
|---|---|---|
| 1 | 进程环境变量 | 部署方 / CI 的显式覆盖 |
| 2 | `config/.env` | 本项目"随部署变化"的配置（**可覆盖**秘密文件） |
| 3 | **项目外秘密文件** | 凭据主位置。用 `LC_SECRETS_FILE` 可改路径 |
| 4 | 调用方默认值 | |

**为什么 `config/.env` 排在秘密文件之前**：本机配置应当能覆盖共享凭据
（例如临时换个模型试），反之会让"改 `.env` 不生效"变成难查的问题。

**⚠️ 取值只看"非空"**：空值**不**参与遮蔽。否则把 `.env.example`
拷成 `.env` 后满屏的 `KEY=` 会挡住秘密文件里的真值 ——
而那种失败的表现是"**明明配了却读不到**"，极难排查。

**排查**：`config.masked(keys)` 给出「值从哪来 + 掩码后的值」，
可安全打印（`describe()` 会带真值，**只该在本机用**）。

**这条由机器强制**：`tests/test_architecture.py` 的存储守卫检查
①项目根不得出现 `_secrets/` 或 `.env`、②`secrets_path()` 必须指向项目外、
③`.gitignore` 必须含 `.env`。

### 2.1 为什么 secrets 用 `.env` 而参数用 `.toml`

- **`.env`**：生态标准（Docker Compose、python-dotenv、绝大多数部署平台都认），
  且天然适合扁平键值对。**秘密只走这里。**
- **`.toml`**：支持嵌套结构与类型，适合"项目参数"这种有层次的配置。
  Python 3.11+ 标准库自带 `tomllib`，**不引入第三方依赖**（符合原则 2）。

### 2.2 配置文件命名规则

| 场景 | 命名 | 例 |
|---|---|---|
| 默认配置 | `config/<name>.toml` | `config/project.toml` |
| 环境覆盖 | `config/<name>.<env>.toml` | `config/project.server.toml` |
| 环境快照 | `config/env/<env-name>.json` | `config/env/local-4060.json` |
| 秘密 | `config/.env` | — |
| 秘密模板 | `config/.env.example` | — |

**`.env` 与 `.env.example` 的键必须完全一致**（值可不同）。
不一致就是缺陷——会造成"新机器跑不起来但不知道缺哪个键"。

### 2.3 明确禁止

| 禁止 | 原因 |
|---|---|
| ❌ 在 `src/` 里放 `.env` / `*.toml` 配置 | 配置与代码混住，无法按部署替换 |
| ❌ 把密钥写进源码或 `project.toml` | 会随代码入库，一旦提交无法撤回 |
| ❌ 新建 `config.yaml` + `config.json` + `settings.ini` 并存 | 多套配置体系必然漂移 |
| ❌ 在 `pyproject.toml` 之外再建打包配置 | 同上 |
| ❌ 把编译规则表/授权目录外置成配置文件 | 它们是核心逻辑，不是部署参数（见原则 1） |
| ❌ 把**应用版本**写进缓存键 | 会导致"改个版本号就让全量缓存失效"（见 §2.4） |

### 2.4 版本号：单一来源 + 三类分离

**唯一来源**：`src/little_crew/version.py`。发行流程只改这一处，并同步以下两处：

| 位置 | 写法 | 说明 |
|---|---|---|
| `src/little_crew/version.py` | `VERSION = "0.4.0-alpha.35"` | **单一来源**，人类可读 |
| 同上 | `PEP440_VERSION = "0.4.0a1"` | 打包体系要求的点号写法 |
| `pyproject.toml` | `version = "0.4.0a1"` | 必须用 PEP 440 写法 |
| `config/project.toml` | `version = "0.4.0-alpha.35"` | 供运行时读取与界面展示 |
| `CHANGELOG.md` | `## [0.4.0-alpha.35]` | 每次发版追加一节 |

**三类"版本"必须严格区分，不可互相代替：**

| 类别 | 例子 | 是否参与缓存键 |
|---|---|---|
| **应用版本** | `0.4.0-alpha.35` | ❌ **绝不** |
| **协议版本** | `little_crew.ir/v1`、`little_crew.compile/v1`、`little_crew.catalog/v1`、`little_crew.interfaces/v1` | ✅ **必须** |
| **业务版本** | 模型/权重/引擎版本 | ✅ 作为产物时效清单字段 |

> **两个都发生过的错误**（因此写进规范）：
> ① 把应用版本写进缓存键 → 改个版本号（哪怕只改文档）就让全量缓存失效；
> ② 把协议版本漏出缓存键 → 规则升级后静默复用旧产物。
>
> 判断标准：**这个变化会不会改变"输入 → 输出"的映射？** 会，则必须进缓存键。

**协议版本变更时的义务**：因协议版本参与哈希，改名或升版会使基准哈希变化。
此时必须同步更新 `CHANGELOG.md` 中的「基准哈希」存档段，否则历史对照失效。

**发版工具**：改版本号不要手改四处，用

```powershell
python tests\bump_version.py 0.4.0-alpha.2 0.4.0-alpha.3
```

它会：① 按 PEP 440 换算打包写法 ② 同步上表全部位置 ③ 同步 `CHANGELOG` 之外
的测试计数 ④ **校验一致性并给出 PASS/FAIL**。幂等，可安全重复运行。

> 之所以要做成工具：版本号散落在 5 处且其中一处必须用**不同写法**（PEP 440）。
> 手改必然漏，而"版本号不一致"这类缺陷**不会导致任何功能失败** ——
> 它会安静地让 `pip install` 装上错误的版本、让缓存键计算与预期不符。

---

## 3. 新增文件时的决策流程

```
我要加一个东西
  │
  ├─ 是 Python 代码？
  │    ├─ 是 → src/little_crew/<层>/        （层归属查 ARCHITECTURE.md §2.1）
  │    └─ 是测试脚手架 → tests/
  │
  ├─ 是配置？
  │    ├─ 含密钥 → config/.env（并同步 .env.example）
  │    ├─ 随部署变 → config/*.toml
  │    └─ 环境快照 → config/env/*.json
  │
  ├─ 是文档？
  │    ├─ 设计/方案/规范 → docs/NN-名称.md（序号递增）
  │    └─ 架构/结构约定 → 根目录 ARCHITECTURE.md / STRUCTURE.md
  │
  ├─ 是调研原文或第三方源码 → research/<主题>/
  │
  └─ 是生成物 → outputs/
```

---

## 4. 代码层归属速查

完整定义见 [`ARCHITECTURE.md`](ARCHITECTURE.md) §2.1。速查：

| 我要写的功能 | 放哪层 |
|---|---|
| IR 结构、哈希、种子、缓存键 | `ir/`（叶子，零依赖） |
| 模型授权判定、地区/营收门禁 | `governance/`（叶子，零依赖） |
| ComfyUI 节点解析、能力矩阵、漂移检测 | `capability/` |
| IR → 执行图的编译规则与算法 | `compiler/` |
| 智能体调用协议、层边界、推理循环 | `agent/` |
| ComfyUI 客户端、队列、缓存、检查点 | `runtime/` |
| 断言算子、门禁、返工闭环 | `review/` |
| HTTP 路由 | `api/` |
| 命令行 | `cli/` |

**依赖方向由 `tests/test_architecture.py` 机器强制**，违反会直接测试失败。

---

## 5. 开发工作流

```powershell
# 项目根（此后所有命令都在此目录）
cd "<项目根>"

# 用项目自带 venv
& ".\.venv\Scripts\python.exe" tests\run_all.py

# 或先安装为可编辑包，之后可直接 python -m
& ".\.venv\Scripts\python.exe" -m pip install -e .
```

| 场景 | 命令 |
|---|---|
| 全量验证（1153 项 + 架构守卫） | `tests\run_all.py` |
| 单层自检 | `python -m little_crew.<层>.check` |
| 仅架构守卫 | `python tests\test_architecture.py` |

### ⚠️ 编码约定（**踩过坑，必须遵守**）

**规则（与 shell 版本无关，一律照此执行）：**

| # | 规则 |
|---|---|
| 1 | 读写源码**优先用编辑器工具**，不用 shell 文本命令 |
| 2 | 必须用脚本处理时，**一律用 .NET 显式编码**：`[System.IO.File]::ReadAllText/WriteAllText` + `New-Object System.Text.UTF8Encoding($false)` |
| 3 | ⚠️ **不要依赖 `cd` 影响 .NET 的当前目录** —— `[System.IO.File]` 用**进程**工作目录，不是 `$PWD`。脚本里一律用**绝对路径** |
| 4 | `.ps1` 脚本内设 `$env:PYTHONIOENCODING="utf-8"`，否则 Python 输出重定向时会回退编码 |

**为什么写死这条规则而不是"看 shell 版本决定"**：本项目历史上损坏过一次 4 个测试文件 ——
用 `Get-Content -Raw` + `Set-Content -Encoding utf8` 往返处理 UTF-8 源码，中文被双重编码，
部分标点**不可逆丢失**，只能逐文件重写。

> **本机 shell 说明**：DSH harness 的 `pwsh` 工具按此顺序解析 shell：
> ① 配置的 `pwshPath` → ② `C:\Program Files\PowerShell\7\pwsh.exe`（MSI 版）
> → ③ PATH 中的 `pwsh.exe`（**Microsoft Store 版走这条**）
> → ④ Windows PowerShell 5.1（最后兜底）。
>
> 因此**同一会话内 shell 版本可能随环境变量变化**，上面 1–3 条与版本无关，任何版本下都安全。


---

## 6. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-10-11 | 首版。确立两条总原则、目录职责表、五类配置归属、新增文件决策流程、代码层速查、开发工作流与编码约定 |
| v1.1 | 2026-10-11 | 项目定名《小剧组 · Little Crew》并设为 `0.4.0-alpha.1`；包名 `comicdrama` → `little_crew`；新增 **§2.4 版本号约定**（单一来源、三类版本分离、协议版本变更义务）；目录树补充 `version.py` 与 `CHANGELOG.md` |
| v1.2 | 2026-10-11 | 新增 **§1.1 外部工具链：一律放项目外** —— 明确 ComfyUI / 模型权重 / 系统工具的位置归属与三条硬理由；编码约定改为**与 shell 版本无关**的硬规则（原描述依赖"本机 pwsh 是 5.1"，现已不准确），新增"不要依赖 `cd` 影响 .NET 当前目录"一条 |
