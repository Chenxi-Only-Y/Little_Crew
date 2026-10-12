# 项目架构总纲

> **本文回答一个问题**：这个项目由哪些部分组成、各自职责边界在哪、彼此怎么通信、按什么顺序实现。
>
> 配套文档：《[设计文档](漫剧平台设计文档.md)》（做什么、为什么）·《[模型调研与适配](漫剧平台-模型调研与适配方案.md)》（接什么模型）

---

## 0. 一句话架构

```
语义图  ←→  统一 IR  ←→  ComfyUI 执行图
             ↑
     三层智能体协议（L1 决策 / L2 协作 / L3 执行）
```

**分层铁律**：`ir` 与 `governance` 是叶子层，不得依赖任何兄弟包；
其余各层只能依赖序号比它小的层。**该约束由 `tests/test_architecture.py` 机器强制。**

---

## 1. 两个正交的分层轴（先说清，否则后面全是混淆）

项目里有两个"分层"概念，它们**互不替代**：

| 轴 | 含义 | 落在哪 | 数量 |
|---|---|---|---|
| **轴 A：代码分层** | 源码包的组织与依赖方向 | `src/little_crew/*` 各子包 | 9 层 |
| **轴 B：运行时分层** | 智能体调用链的层级 | `agent` 包内的 `Layer` 枚举 | 3 层（L1/L2/L3） |

**轴 A 是"代码住在哪"，轴 B 是"运行时谁调用谁"。**

一个例子说清二者关系：

- `little_crew.compiler`（轴 A 的第 2 层）**不是**智能体，它是**纯函数库**——给定 IR 与环境快照，返回执行图。
- 运行时，`image_gen` Agent（轴 B 的 L2）在一次推理循环里调用 `comfy.submit` 工具（轴 B 的 L3）；
  那个 L3 工具**内部**会去调 `little_crew.compiler.compile()`（轴 A 的第 2 层）。

```
轴 B（运行时调用树）              轴 A（代码依赖）
  L1 master_director
    └─ L2 image_gen ──────────► 用 little_crew.agent.protocol（第 2 层）
         └─ L3 comfy.submit ──► 用 little_crew.compiler（第 2 层）
                                 └─► 用 little_crew.capability（第 1 层）
                                      └─► 用 little_crew.ir（第 0 层）
```

> **为什么必须分清**：轴 B 的 R1–R4 规则（L3 不得回调上层）保证 **L3 永远确定性**；
> 轴 A 的依赖方向保证**代码可独立测试、可独立发布**。两者管的不是一回事。

---

## 2. 代码分层：11 个包

```
小剧组·Little_Crew/
├── src/little_crew/
│   ├── version.py              [0] 版本与标识的单一来源（包根豁免层序）
│   ├── ir/                     [0] 叶子：统一 IR 规范与规范化
│   ├── governance/             [1] 叶子：模型授权门禁
│   ├── models/                 [2] 模型规格与通道声明 ← 双通道抽象的落点
│   ├── capability/             [2] ComfyUI 环境能力矩阵与漂移检测
│   ├── compiler/               [3] IR -> 执行图/请求编译器 + 模型路由
│   ├── agent/                  [3] 三层调用协议
│   ├── runtime/                [4] 执行层（通道/队列/缓存/检查点/GPU 调度）
│   │   └── channels/               通道实现：comfy.py · http.py
│   ├── review/                 [4] 验收层（断言算子/门禁/返工闭环）
│   │   └── operators/              确定性断言算子
│   ├── api/                    [5] 对外 HTTP 接口
│   └── cli/                    [5] 命令行入口
├── tests/
│   ├── run_all.py              一键全量测试
│   ├── test_architecture.py    架构守卫（机器强制依赖方向）
│   ├── test_model_routing.py   跨层集成：模型路由与双通道
│   └── bump_version.py         发版工具（版本号单一来源的保障）
├── config/                     配置（见 STRUCTURE.md §2 的五分类）
├── docs/                       设计 · 调研 · 规范文档
├── research/                   调研归档（只读，不参与构建）
├── ARCHITECTURE.md             本文
├── STRUCTURE.md                文件放哪、配置怎么写
├── CHANGELOG.md                本版做了什么、有何限制
├── README.md                   项目入口
└── pyproject.toml              打包配置（Apache-2.0，运行期零第三方依赖）
```

> **本文只管"代码怎么分层"，不管"文件放哪"。**
> 目录职责、配置分类、新增文件的决策流程见 [`STRUCTURE.md`](STRUCTURE.md)。

> **测试放哪由依赖决定**：只依赖更低层的**单元测试**放 `src/little_crew/<层>/`；
> 需要跨层协作的**集成测试**放 `tests/`。
> 把集成测试放进包内会构成反向依赖，架构守卫会直接判定违规 ——
> 这个约束不是形式主义，它保证"包内测试"始终能反映该层的真实可独立测试性。

### 2.1 各层职责与边界

| # | 包 | 职责 | 明确**不做**什么 | 状态 |
|---|---|---|---|---|
| 0 | `ir` | IR 结构定义、六步规范化、内容哈希、种子派生、三级缓存键 | 不碰网络、不碰文件、不知道 ComfyUI 的存在 | ✅ 已实现 36 测试 |
| 1 | `governance` | 模型授权事实登记与门禁判定（地区/营收/AUP/归属标注） | 不关心模型怎么跑、不管管线 | ✅ 已实现 33 测试 |
| 2 | `models` | **模型规格与通道声明**：模型画像、能力/资源/成本/实测、任务档案 | 不含节点名（那是 `capability` 的事）、不做路由决策（那是 `compiler/router` 的事） | ✅ 已实现（含于 36 测试） |
| 2 | `capability` | `/object_info` 解析、能力矩阵、环境哈希、三态漂移检测、**ComfyUI 接口契约知识** | 不生成图、不做模型选型决策 | ✅ 已实现（含于 94 测试） |
| 3 | `compiler` | 编译规则表、拓扑排序、端口映射与类型强制、降级链、约束注册表、阈值联动、**模型路由** | 不发起执行、不写数据库 | ✅ 已实现 94 测试 |
| 3 | `agent` | 三原语（Registry/Envelope/Blackboard）、R1–R4 层边界强制、有界推理循环 | 不含业务逻辑、不知道漫剧是什么 | ✅ 已实现 27 测试 |
| 4 | `runtime` | **执行通道**（ComfyUI / HTTP）、任务队列与租约、三级缓存、渲染检查点、产物时效清单、GPU 调度 | 不做质量判定 | ⚠️ 通道已实现，其余待实现 |
| 4 | `review` | 断言算子库、硬/软门禁、跨模型盲读编排、返工闭环、交付证据链 | 不生成内容 | ⬜ 待实现 |
| 5 | `api` | HTTP 路由、鉴权、SSE/WebSocket 进度推送 | 不含业务逻辑（薄壳） | ⬜ 待实现 |
| 5 | `cli` | 命令行：探测环境、编译 IR、跑单镜、跑一集 | 同上 | ⬜ 待实现 |

### 2.1.1 模型双通道：本地与 API 是**同一抽象的两个 endpoint**

这是"用户可选择模型 API / 本地双通道"的架构落点。

**设计要点：把"模型"与"通道"拆成两个正交维度。**

```
模型（干什么、擅长什么）        通道（在哪跑、怎么调）
  TaskKind      任务域            BackendKind.COMFYUI   本地节点图执行
  Capability    能力画像          BackendKind.HTTP      直连 HTTP
  ResourceProfile 资源画像        BackendKind.PROCESS   本地进程内（预留）
  CostProfile   成本画像
```

二者**正交**：加一个通道不改模型，加一个模型不改通道。

**关键判断：本地服务与云端 API 不是两种通道，是同一种通道的两个 endpoint。**

`D:\AI\local-gen` 暴露的是与云端等价的 OpenAI 兼容 HTTP 接口，因此它和
DeepSeek / 通义 走**同一段代码**（`runtime/channels/http.py`），只是 endpoint 不同。
只有 ComfyUI 因为需要"编译成节点图"才是**真正不同的通道**。

| | ComfyChannel | HttpChannel |
|---|---|---|
| 任务形态 | `CompiledJob.graph`（节点图） | `CompiledJob.request`（method/path/body） |
| 进度 | `/ws` 细粒度（帧/步） | 同步无中间进度；异步按轮询 |
| 中断 | `/interrupt` + 队列删除 | 同步**不可中断**（抛错，不静默忽略） |
| 对账 | `/history/{prompt_id}` 可回收 | 需持久化，进程内仅能同会话回收 |
| 是否计费 | 否（本地电费） | **远端计费 → 幂等去重是硬要求** |

**为什么这个区分重要**：`metered` 决定"重复提交"的后果。
本地重跑浪费 GPU 时间（数分钟）；云端重跑**重复计费**（真金白银）。
因此 `Channel.submit` 契约把幂等（R1）列为**第一条**。

**`endpoint` 留空的语义**：表示"用 `config/.env` 提供的默认值"，
便于换机器不改代码。此时本地性由 `Backend.is_local_by_default` 判定 ——
否则一个部署在 `127.0.0.1:8100` 的本地服务会被误判为云端 API，
导致"用户选本地"时它被错误排除。

**能力不足时的表现**：授权的硬约束在**路由阶段**生效（`compiler/router.py`），
能力不足在**编译阶段**生效（`compiler` 的降级链），
环境缺失在**探测阶段**生效（`capability` 的漂移检测）。
三者分层，任何一层都不越权替另一层做判断。

### 2.1.2 编译器必须自证"产出的图是完整的"

**这条约定来自一次真实的失败。**

首次端到端真机验证时，编译器产出的图**本地全绿**，提交给 ComfyUI 却被拒：

```json
{"node_errors": {
  "n_prompt": {"errors": [{"type": "required_input_missing", "details": "clip"}]},
  "n_lora":   {"errors": [{"type": "value_not_in_list",
                            "details": "lora_name: 'linwan_v3.safetensors' not in []"}]}
}}
```

两类根因，**都是"局部看都对、合起来是错的"**：

| 类别 | 局部为何"对" | 合起来为何错 |
|---|---|---|
| 必需端口留空 | 该端口 IR 没声明，编译器没理由自作主张 | 图中出现结构不完整的节点，ComfyUI 直接拒绝 |
| 生成引用缺失文件的节点 | 漂移检测**已经报了**缺失，也发了通知 | 通知发了，但**图没改** —— 节点照样生成 |

由此确立两条**编译期自证**义务：

**① 产出完整性终检**（`compile()` 的 3.5 步）

逐个节点对照**真实 spec** 校验必需端口齐备，缺则抛 `INCOMPLETE_GRAPH`。
判据只取**连线端口**（`PortSpec.is_link`）：

```
连线端口（MODEL/CLIP/LATENT/...）  缺失 -> 图不完整，ComfyUI 必拒 -> 报错
widget 端口（INT/FLOAT/STRING/COMBO）缺失 -> ComfyUI 用节点默认值 -> 不报错
```

若把 widget 也算缺陷，会逼着每个 IR 写全所有参数，与"IR 只表达语义"相悖。

**② 降级必须**改变图**，而不只是发通知**

"漂移检测发现了问题"与"编译器据此行动了"是两件事。
通知是给**人**看的，改图才是给**执行**用的。二者缺一即为静默错误。

| 情形 | 动作 |
|---|---|
| 可选节点的文件不可用 | **移除该节点** + 下游引用**重接到最近的存活祖先** |
| 必需端口的提供者缺失 | 按**端口类型**自动接线；接不上则报 `INCOMPLETE_GRAPH` |
| 必需规则的权重缺失 | 硬阻断（不假装能跑） |

重接时**做类型校验**：类型不符宁可留空 —— 留空会被 ComfyUI 拦下（显式失败），
错接是静默错误。**两害相权取其显。**

### 2.2 三点关键边界约定

**① `capability` 是唯一知道 ComfyUI 节点名的地方**

`UNETLoader`、`CLIPLoader`、`MiniMaxH3ImageToVideo` 这些字符串**只允许**出现在
`capability/` 与 `compiler/` 的规则表中。其他任何包出现节点名，都说明抽象泄漏了。

**② `governance` 是唯一知道模型授权的地方**

`Apache-2.0`、`excluded_regions`、`revenue_threshold_usd` 只允许出现在 `governance/`。
`runtime` 路由模型时**必须**调 `governance.LicenseGate.check()`，不得自行判断。

**③ `review` 不做生成，`runtime` 不做判定**

这是"不自我签收"原则在代码结构上的体现：产出与判定必须分属不同包，
从物理上杜绝"同一个模块既生成又判定"。

---

## 3. 依赖方向（机器强制）

```
          api ── cli
           │      │
           └──┬───┘
              ▼
      ┌───── runtime ─────┐
      │         │         │
      ▼         ▼         ▼
   review ──► agent ──►  ir  ◄── governance
                │        ▲
                ▼        │
            compiler ────┤
                │        │
                ▼        │
           capability ───┘
```

**允许的依赖边**（已实现部分，由守卫实测输出）：

```
agent     -> ir
compiler  -> capability
compiler  -> ir
```

**禁止的反向依赖**（守卫会拦下并报 `R1 违反`）：

| 禁止 | 理由 |
|---|---|
| `ir` → 任何兄弟包 | `ir` 是基石。它一旦依赖别人，所有层都被绑死，且无法独立发布规范 |
| `capability` → `compiler` | 能力矩阵是"环境是什么样"，编译器是"怎么用它"。反过来就是循环 |
| `governance` → `capability` | 授权判定只关心模型与部署情形，与环境节点无关 |
| `compiler` → `runtime` | 编译器必须保持纯函数；一旦能发起执行就无法保证可复现 |
| 任何 → `api` / `cli` | 上层不得被下层依赖 |

### 3.1 为什么要有架构守卫

违反依赖方向**通常不影响功能**——代码照样跑、测试照样过。
所以功能测试**永远不会**发现架构退化。等发现时已经是一团泥球。

因此 `tests/test_architecture.py` 是独立的一类测试：它不测功能，只测架构。
三条规则：

- **R1** 下层不得 import 上层
- **R2** `ir` 不得 import 任何兄弟包（零依赖基石）
- **R3** 不得使用 `sys.path` 操控（禁止隐式路径依赖）

> **这条守卫不是形式主义**。本项目在统一架构时正是靠它发现了
> 原先 4 个模块同名平铺、靠 `sys.path.insert` 并列才能 import 的问题。

---

## 4. 层间契约

### 4.1 核心数据类型（已实现）

```
ir.canonical  ──►  normalize(graph) -> dict           规范化的 IR
                   content_hash(graph) -> "sha256:..." 语义哈希
                   cache_key(...) -> "t1:..."          三级缓存键
                   derive_seed(...) -> int             确定性种子

capability    ──►  probe_from_object_info(raw) -> CapabilityMatrix
                   matrix.catalog_hash() -> "sha256:..."
                   matrix.detect_drift(reqs) -> [Drift]

compiler      ──►  ComfyCompiler(matrix, rules).compile(ir) -> CompileResult
                   CompileResult{ graph, notices[], drifts[], graph_hash,
                                  threshold_adjustments, auto_injected }

governance    ──►  LicenseGate(context).check(model_id) -> LicenseVerdict
                   filter_allowed(ids, ctx) -> ([allowed], [verdicts])
                   collect_attributions(ids) -> [str]

agent         ──►  LayerRuntime(registry, blackboard).invoke(cap, payload, budget)
                   -> (CallResult, CallEnvelope)
```

### 4.2 待实现的层间契约（接口先定，实现后补）

```python
# runtime 对外（供 api / cli 调用）
class RunOrchestrator:
    def submit_episode(self, episode_id: str, plan: EpisodePlan) -> RunHandle: ...
    def watch(self, run_id: str) -> Iterator[ProgressEvent]: ...
    def resume(self, run_id: str) -> RunHandle: ...        # 断点续跑
    def freshness(self, episode_id: str) -> FreshnessReport: ...  # 产物时效清单

# runtime 内部
class ContentCache:
    def lookup(self, key: str) -> CachedArtifact | None: ...
    def store(self, key: str, artifact: Artifact) -> None: ...
    def invalidate_by_catalog(self, catalog_hash: str) -> int: ...
    #: ⚠️ 命中统计必须**分开**：平台命中 vs ComfyUI 命中（见下）
    def stats(self) -> CacheStats: ...

class RenderCheckpoint:
    def record_submitted(self, prompt_id: str, node_exec: NodeExecution) -> None: ...
    def reconcile(self, comfy: ComfyClient) -> ReconcileReport: ...

class GpuScheduler:
    """按模型分批调度 —— 8GB 显存同时只能加载一个模型（实测约束）"""
    def plan_batches(self, tasks: list[Task]) -> list[ModelBatch]: ...

# review 对外
class GateRunner:
    def evaluate(self, artifact: Artifact, constraints: list[Constraint]) -> GateReport: ...
    def assert_operators(self) -> dict[str, AssertOperator]: ...

# 编译规则表扩展（不动编译器）
def register_model_rules(table: CompileRuleTable, spec: ModelSpec) -> CompileRuleTable: ...
```

### 4.2.1 三条来自源码核实的 runtime 硬约束

这三点是核实 ComfyUI 内部机制后确定的，**必须体现在 runtime 实现里**
（依据见 `docs/01-平台设计文档.md` §7.8 与 `research/comfyui-internals/`）：

| # | 约束 | 违反后果 |
|---|---|---|
| **C1** | **命中平台的 T1/T2 仍不足以跳过提交** —— IR 变了但底层图大部分未变时，应照常提交，让 ComfyUI 的节点级缓存吃掉未变的祖先链 | 白跑一趟，浪费一次往返；缓存收益被低估 |
| **C2** | **平台命中与 ComfyUI 命中必须分开统计**。ComfyUI 通过 `execution_cached` 消息广播本次命中的节点列表 | 把 ComfyUI 的功劳算成平台的 → E7 缓存收益实验的量化指标失真，答辩时数据不可信 |
| **C3** | **编译结果的 `determinism_concerns` 必须被消费**。对确定性存疑的节点，runtime 在复用缓存前应核对，或至少在日志与交付证据里标记 | "以为重跑了其实没跑" —— 缺陷静默通过验收 |

```python
# C2 的接口形态（runtime 实现时照此写）
@dataclass
class CacheStats:
    platform_hits: int          # T1/T2/T3 命中，未提交
    comfyui_hits: int           # 已提交但 ComfyUI 全命中（execution_cached 覆盖全部节点）
    comfyui_partial: int        # 部分命中（祖先链被复用）
    misses: int                 # 真正发生计算

# C3 的接口形态
class CacheDecision:
    def may_reuse(self, prompt_id: str, ir_node_id: str) -> tuple[bool, str]:
        """返回 (是否可复用, 理由)。对 determinism_concerns 中的节点需额外核对。"""
```

### 4.3 数据流：一次镜头生成的完整链路

```
ShotComposerAgent (L2)
  │ 1. 产出一个镜头 IR
  ▼
ir.canonical.content_hash(ir) ──────────────► irHash
  │
  ▼
governance.LicenseGate.check(model) ────────► 授权通过？否则换模型
  │
  ▼
runtime.ContentCache.lookup(tier1_key) ─────► 命中？直接复用产物
  │ 未命中
  ▼
compiler.ComfyCompiler(matrix).compile(ir) ─► graph + notices + graphHash
  │                                            （含降级与阈值联动）
  ▼
runtime.RenderCheckpoint.record_submitted ──► prompt_id 落盘
  │
  ▼
ComfyClient.submit(graph) ──────────────────► ComfyUI
  │
  ▼
review.GateRunner.evaluate(artifact) ───────► KEEP / RETRY / ESCALATE
  │
  ▼
写回 Blackboard → 触发失效传播 → 更新产物时效清单
```

**这条链路上每一环都有对应的已实现或已定义接口**——这就是"架构统一"的实际含义。

---

## 5. 构建与运行

### 5.1 环境

```powershell
cd "<项目根>"

# 方式一：用项目自带 venv（推荐，.idea 已指向它）
& ".\.venv\Scripts\python.exe" tests\run_all.py

# 方式二：装为可编辑包，之后可直接 python -m
& ".\.venv\Scripts\python.exe" -m pip install -e .
```

> 本机 `python`（`D:\Python`）与项目 `.venv` 都是 3.12.13。
> `.venv` 目前只装了 pip —— 核心层零依赖，暂时不需要更多。
> 引入 `runtime` 层依赖时用 `.venv` 安装，**不要污染系统 Python**。

### 5.2 跑测试

**22 个功能套件 + 1 个架构守卫**，共 1153 项。套件清单与期望项数
的**单一来源**是 [`tests/run_all.py`](tests/run_all.py) —— 这里不再重复列举，
因为手抄清单**必然漂移**（本文档此前就写着"四个套件"，而实际早已是 23 个）。

一键全跑：

```powershell
python tests\run_all.py
```

跨层集成套件（`tests/*.py`）：

```powershell
python tests\test_e2e_comfy.py        # 端到端真机（无 ComfyUI 时自动跳过）
python tests\test_agent_collab.py     # 多智能体协作
python tests\test_model_routing.py    # 模型路由与双通道
python tests\test_architecture.py     # 架构 + 存储守卫（非功能测试）
```

单层自检（各层自带，命名不统一，以实际文件为准）：

```powershell
python -m little_crew.runtime.episode_check     # 一集流水线
python -m little_crew.review.check              # 确定性验收门禁
python -m little_crew.review.embed_check        # L2 一致性描述子
python -m little_crew.cli.check                 # 命令行与 HTTP 接口
```

> **为什么不用 pytest**：核心层刻意保持**零第三方依赖**，包括测试。
> 这样在任何只有 Python 3.12 的机器上都能验证正确性——包括离线环境与答辩现场。
> 未来 `runtime`/`api` 层需要 httpx 等依赖时再引入 pytest，见 `pyproject.toml` 的 `[dev]`。

### 5.3 编码约定（**踩过坑，必须遵守**）

**规则（与 shell 版本无关，一律照此执行）：**

1. 读写源码**优先用编辑器工具**，不用 shell 的文本命令
2. 必须用脚本处理文本时，**一律用 .NET 显式编码**：

   ```powershell
   $u8 = New-Object System.Text.UTF8Encoding($false)      # $false = 不写 BOM
   $t  = [System.IO.File]::ReadAllText($path, $u8)
   # ... 处理 ...
   [System.IO.File]::WriteAllText($path, $t, $u8)
   ```

3. ⚠️ **不要依赖 `cd` 影响 .NET 的当前目录** —— `[System.IO.File]` 用的是**进程**工作目录，
   不是 PowerShell 的 `$PWD`。脚本里一律用**绝对路径**（本项目已因此踩过两次）

**为什么必须写死这条规则**（而不是"看 shell 版本决定"）：本项目历史上损坏过一次
4 个测试文件 —— 用 `Get-Content -Raw` + `Set-Content -Encoding utf8` 往返处理 UTF-8 源码，
中文被双重编码，部分标点**不可逆丢失**，只能逐文件重写。

**一个关键细节**：DSH harness 的 `pwsh` 工具会按以下顺序解析 shell ——

```
① 显式配置的 pwshPath
② C:\Program Files\PowerShell\7\pwsh.exe          （MSI 版）
③ PATH 中每一项拼接 \pwsh.exe                      （Microsoft Store 版走这条）
④ C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe   （5.1，最后兜底）
```

因此**同一个会话内 shell 版本可能因环境变量而变化**。上面第 1–3 条与版本无关，
在任何版本下都安全。

支持上述结论的 harness 源码注释（`app.asar` 内 `resolvePwshPath`）：
Store 的应用执行别名是 reparse point，harness 特意用 `lstatSync` 而非 `statSync`
来识别它（`stat` 会因目标 ACL 报 EACCES）。

---

## 6. 实施顺序

按依赖方向自底向上，**每层完成即可独立验收**：

```
已完成 ████████████████████████  全部 11 层
         ir · config · governance · models · capability · compiler
         agent · review · runtime · api · cli
                                  （1153 项测试全通过 + 架构守卫 PASS）

未完项 ────────────────────────  C2 自研 Transformer 三模块
                                 一致性图像层（角色锚点图）
                                 R13 其余职位的真实 LLM 实现
                                 → 见 docs/00-项目策划书.md §5.10
```

| 阶段 | 内容 | 可验收标志 |
|---|---|---|
| **R1 runtime（客户端）** | ComfyUI HTTP/WebSocket 客户端、`/object_info` 真实探测 | 连上真实 ComfyUI，打印完整能力矩阵与环境哈希 |
| **R2 runtime（缓存）** | 三级内容寻址缓存、`cacheKey` 落库 | 同一 IR 二次生成命中 Tier-1，0 秒返回 |
| **R3 runtime（续跑）** | 渲染检查点、`reconcile` 对账、断点续跑 | 强制 kill 进程后重启，不重复烧 GPU |
| **R4 runtime（调度）** | 队列/租约/幂等、按模型分批的 GPU 调度 | 一次跑完 60 镜且无模型反复加载 |
| **V1 review** | 断言算子库（11 种）、硬门禁 | 注入缺陷样本全部被拦 |
| **V2 review** | 软门禁、跨模型盲读编排、返工闭环 | 缺陷自动返工并留证据链 |
| **I1 api** | HTTP 路由 + 进度推送 | 前端能看到镜头级进度 |
| **I2 cli** | `probe` / `compile` / `shot` / `episode` 四个命令 | 命令行跑通一集 |

> **R1–R4 建议合并为一轮"执行层"**，因为它们的接口互相咬合。
> 但**验收顺序不能乱**：先客户端（能看到环境），再缓存（省钱），再续跑（抗中断），最后调度（提效率）。
> 这个顺序的理由是：本机一集需 4~7.5 小时 GPU 时间（实测），**续跑能力必须先于调度优化**。

---

## 7. 待决策事项

| # | 事项 | 选项 | 影响 |
|---|---|---|---|
| D1 | `runtime` 的 ComfyUI 通信 | 自研 httpx/websockets vs 引入 `comfyui-mcp`（Apache-2.0） | 自研可控但工作量大；引入快但多一层依赖 |
| D2 | 状态存储 | PostgreSQL vs SQLite vs 纯文件 | 单机本地版 SQLite 足够；集群需 PG |
| D3 | `review` 的算子实现 | 纯 Python（Pillow/numpy）vs 调外部服务 | 已确认 numpy/Pillow 可用 |
| D4 | `api` 框架 | FastAPI vs 无框架 stdlib | 核心层零依赖原则是否延伸到 api 层 |
| D5 | 前端是否自研 | 自研 React 画布 vs 复用 TapCanvas 契约设计 | 后者可省数千小时 |

---

## 8. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-10-11 | 首版。确立双轴分层模型（代码分层 9 包 / 运行时分层 3 层）；完成包重构与统一命名空间；建立架构守卫；定义层间契约与实施顺序 |
| v1.1 | 2026-10-11 | 项目迁入 `小剧组·Little_Crew/`，脚手架从 `platform/` 上移至项目根；`src/` 只保留源码；§2 目录树与 §5 构建命令同步更新。**目录职责与配置规范移交 [`STRUCTURE.md`](STRUCTURE.md)**，本文专注代码分层 |
| v1.2 | 2026-10-11 | **ComfyUI 接口契约修正**（源码级核实，依据 `research/comfyui-internals/`）：① 新增 `capability/comfy_interfaces.py` 作为接口契约知识的唯一位置（失效钩子已知表 + 确定性三分类 + 端口类型表）；② 修正"种子需强制重算"的过度判断 —— 种子在 `inputs` 里，ComfyUI 签名已覆盖；③ 修复**把文件路径喂给 IMAGE 端口**的缺陷（新增 `image.file` 规则 + `_coerce_for_port` 自动注入 `LoadImage` + 路径派生确定性节点 id）；④ 环境哈希纳入钩子存在性（ABI 变化），新增 `FINGERPRINT_HOOK_CHANGED` 漂移；⑤ 新增 §4.2.1 三条 runtime 硬约束。测试 69 → 89 项 |
| v1.3 | 2026-10-11 | **降噪修正**：新增官方确定性节点白名单 `KNOWN_PURE_CLASS_TYPES` 与第三方节点在册表 `THIRD_PARTY_NODES_IN_USE`，区分"未收录"与"确实未知"；确定性存疑告警改为**按 `class_type` 去重**并声明影响范围。实测同一镜头图的告警由 **9 条降至 1 条**，且保留的那条是真实的（`IPAdapterAdvanced` 钩子未核实）。测试 89 → 94 项 |
