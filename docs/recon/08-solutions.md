# 小剧组 Little Crew — 四个关键问题的解决方案

> 对应 `07-risks-predictions.md` 中的 4 条主要风险，逐条给出**可执行方案**。
> 用户已确认的方向：①本地换串行 ②解决单例+thread-local ③历史数据全删、按新表 16 职业
> ④三条新管线零原型 ⑤画布回环。

---

## 1. 本地 8GB 并发 → 换成串行

### 结论：**可以，但"串行"要落在正确的位置，否则会变成新的瓶颈**

单纯把 `image_generator.CONCURRENCY = 4` 改成 `1` 能消除 OOM，但会带来三个新问题：

| 问题 | 说明 |
| --- | --- |
| 云端也被拖慢 | 并发度是**模型能力**的函数：本地=1、云端=4~8。写死在调用方就一刀切 |
| 进度语义要改 | 串行下"4 张并行"的进度条要改成**队列视图**，否则用户以为卡住 |
| 长任务无人取消 | 本地一次生成 30–60 秒、视频 45 分钟，**串行队列里的等待必须能取消** |

### 方案：并发度由 **provider 声明**，调用方只负责排队

```python
class ImageProvider(Protocol):
    # 该 provider 能承受的并发；本地=1，云端=N
    max_concurrency: int

    def generate(self, spec, ctx: RunContext) -> ImageResult: ...
```

```python
# 调用方不再硬编码 4，而是问 provider
workers = max(1, provider.max_concurrency)
with ThreadPoolExecutor(max_workers=workers) as pool: ...
```

配套三件事：
1. **真实队列视图**：`{queued: N, running: shot_id, done: M, eta_seconds}`，
   复用你现有的 `payload._retry_progress` 机制（已在用，不需要新发明）。
2. **ETA 而非"按张均分"**：串行下用**历史平均耗时**估算（本地首张含模型加载 ~33s，之后 ~30-60s/张，视频按步 × 54s）。
3. **可取消**：队列里的任务要能整体取消（`task_runner._CANCEL` 已有雏形，但要覆盖排队项）。

> ⚠️ 一个必须知道的坑：`local-gen` 服务端**已经有一把全局锁**（`gpu_lock`）把推理串行化了。
> 所以即使你这边串行，也只是"不浪费线程"，**并不能让本地变快**。
> 串行的真正价值是：**进度可预期 + 可取消 + 不误报并发**。

---

## 2. 9 个模块级单例 + thread-local → 怎么解决

### 先看清问题分两类，解法不同

| 类别 | 问题 | 现例 |
| --- | --- | --- |
| **A. 懒加载单例**（9 个） | 在 `__init__` 里**快照配置** → 运行中改配置不生效；测试无法注入替身 | `llm._llm_instance`、`image._image_instance`、`video._service`… |
| **B. thread-local 上下文** | `threading.local` 在**线程池复用**与**多 worker** 下会串/丢失 | `runtime_settings._local.user / session_id`、`image._role_local` |

### 解法 A：显式依赖注入（不用框架，30 行足够）

```python
# app/core/container.py
@dataclass
class Services:
    llm: LLMService
    image: ImageService
    video: VideoService
    quality: QualityService
    sessions: SessionStore
    users: UserStore
    settings: SettingsProvider      # ← 每次读，不快照

def build_services(cfg: Settings) -> Services:
    return Services(llm=LLMService(cfg), image=ImageService(cfg), ...)
```

要点：
- **配置不落快照**：`LLMService` 不再保存 `self.mock`，改为每次问 `cfg.mock_llm`。
  这样"设置页保存立即生效"才真正成立（现在这条承诺是假的）。
- **Agent 接收 services**：`Agent.run(state, svc)` 而非 `Agent().run(state)` 内部 `get_xxx()`。
- **测试注入替身**：`build_services(cfg)` 换一个 mock 容器即可，不再靠改 `settings.mock_llm` 这种全局副作用。

### 解法 B：上下文改为**显式传递**

```python
@dataclass(frozen=True)
class RunContext:
    user_id: str
    session_id: str
    role: str          # 取代 image._role_local
    run_id: str        # 取代 _THINKING/_CANCEL 的 key

def generate(self, spec: ImageSpec, ctx: RunContext) -> ImageResult: ...
```

**为什么不用 `contextvars`**：`contextvars` 在 `asyncio` 下能自动传播，但**线程池不会自动继承**
（需要手动 `copy_context().run()`）；本地串行 + 云端并发的混合模型下容易踩空。
**显式参数最笨但最稳**，且 IDE 能跳转、类型能检查。

| 现状 | 改后 |
| --- | --- |
| `_role_local = threading.local()` 存 role | `ctx.role` 显式传参 |
| `runtime_settings.set_context(u, s)` + `user()` / `run_session()` 读取 | 同一份 `RunContext` 贯穿调用链 |
| `_THINKING`/`_CANCEL`/`_JOBS` 进程内 dict | Job 表（落库），支持跨进程 cancel/status |

### 迁移策略（与"历史数据全删"配合后大幅简化）

因为已决定**删掉历史数据**，A 类单例**不需要任何向后兼容**：
不需要"读旧 payload 兼容"、不需要双读双写。**这是一次干净替换**，风险显著下降。

顺序建议：
```
1. 引入 Services 容器 + RunContext（新代码只用新方式）
2. 逐个把 9 个 get_xxx() 改成容器注入，删掉模块级 _instance
3. 最后删掉 runtime_settings 的 thread-local（改成显式传参）
4. 加一个测试：注入两个不同配置的容器，行为互不干扰
```

---

## 3. 历史数据全部删除 + 按新表 16 职业

### 结论：**同意，这是让重构干净很多的决定**。但要按清单删，别删错东西。

### 3.1 删除清单（逐项确认）

| 对象 | 大小/量 | 处理 | 理由 |
| --- | --- | --- | --- |
| `data/little_crew.db` | 462 KB / 13 sessions / 10 users | **删** | 全是测试数据（`sec_*`/`final_*`/`verifyuser` 等） |
| `data/outputs/**` | **166 文件 / 116 MB** | **删** | 旧职业命名目录（`技术制作部/photographer`），v2 结构不兼容 |
| `data/verify_token.txt` | 43 B | **删** | 测试残留 |
| `data/e2e_real_drive.py`、`data/drv_review3.py` | 游离脚本 | **移入 `scripts/` 或删** | 不在 `scripts/` 下，属调试遗留 |
| **`data/.little_crew.key`** | 44 B | ⚠️ **先判断再动** | **Fernet 密钥**——删它则 `api_settings` 里已加密的供应商密钥**永久解不开** |
| `.env` | 2036 B | **保留** | 你的真实配置 |
| `docs/recon/*` | 7 份 | **保留** | 重构依据 |

### 3.2 密钥体检结果：**可以放心全删**（已实测）

删前做了解密体检（只读，未改任何数据），结论如下：

| 行 | 类型 | 内容 |
| --- | --- | --- |
| `alice` | 明文 | `llm_provider=zhipu`、`llm_model=glm-4` |
| `local` | 明文 | 空 `{}` |
| `1` | **密文（Fernet）** | **实测解密成功**，内容为：`video_max_duration=8`、`video_resolution=768P`、`max_images=1`、`video_count=1`、`image_quality=standard`、`video_quality=768P` |

**解密后的密文行里只有生成参数，没有任何 API Key / token。**
→ `.little_crew.key` 与 `little_crew.db` **可以一起删，不会丢失任何真实密钥**。

### 3.3 删除带来的三个简化（好处）

| 原风险 | 删除后 |
| --- | --- |
| `07-risks §5` 历史 payload 旧 key 读不出 | **消失**——不需要 `legacy_keys` 别名映射 |
| 产物路径新旧不兼容 | **消失**——不需要"新路径写 + 旧路径读"双兼容 |
| 单例改造要兼容旧数据 | **消失**——可以干净替换，不做兼容层 |

**备份仍在**：`C:\Users\OnlyY\AppData\Local\Temp\ds6-recon\little_crew_20261002_144228.db`
（万一要对照，别删这个）。

### 3.4 新表 16 职业落地时要注意

删除历史数据只解决"数据"层面的问题，**"职业 ≠ 节点"的结构问题仍在**（`07-risks §4`）：

```
Role（职业，16 个，面向用户）
  └─ Panel（面板视图，可多个）
       └─ Stage/Node（执行单元，可被多 Role 复用）
```

**建议在 V0 就用这个三层结构建注册表**，避免以后返工。

---

## 4. 三条新管线零原型 → 怎么解决

### 结论：**不要各做一遍 MVP，而是"接口先行 + 假实现占位 + 按难度排期"**

如果三条管线各憋一个原型，画布前端会被阻塞很久；如果直接进画布，会出现一堆点了没反应的节点。

### 4.1 关键动作：先把**产物契约**定死（半天）

三条管线各自的数据结构，全部以**现有产物为基座**扩展（不另起炉灶）：

```python
# ① 白模预演（2D）
Previs = {
    "shot_id": str,
    "layout": [{"actor": str, "x": float, "y": float, "facing": float}],   # 走位
    "camera": {"x": float, "y": float, "zoom": float, "angle": float},     # 机位
    "overlay_path": str,        # 生成的 2D 俯视/示意 PNG
}
# 复用：storyboard[].description/camera + scene_design_profiles

# ② 配音
VoiceTrack = {
    "shot_id": str,
    "text": str,                # 来自 storyboard[].dialogue
    "voice_id": str,            # 音色
    "emotion": str,             # 情感提示词
    "audio_path": str,
    "duration_s": float,        # ★ 关键：供剪辑对齐用
}
# ③ 剪辑时间线
Timeline = {
    "tracks": [{"kind": "video|voice|bgm|subtitle", "clips": [
        {"shot_id": str, "start": float, "end": float, "transition": str,
         "subtitle": str, "source": str}]}],
    "total_s": float,
    "output_path": str,
}
# 复用：现有 video.py 的 images + rhythm_plan[].duration
```

**把 `duration_s` 显式纳入 `VoiceTrack`**，是解决回环的前提（见 §5）。

### 4.2 假实现占位（让画布先跑通）

注册表里三个节点先返回**结构正确、内容占位**的数据：
- 白模：画一张纯色图 + 默认站位（PIL，20 行）
- 配音：生成**静音 wav + 按字数估算的 duration**（不调 TTS）
- 剪辑：直接复用现有 FFmpeg 幻灯片逻辑

**收益**：画布拓扑、端口、校验、编排在**真实管线完成前就能全部跑通**。

### 4.3 按难度排期实现（难易排序）

| 管线 | 难度 | 依据 | 建议顺序 |
| --- | --- | --- | --- |
| **白模预演 2D** | ★☆☆ | PIL 画俯视图 + 箭头，**零新依赖** | 第一个做 |
| **配音** | ★★☆ | Edge TTS 免费已预留（`video.py` 的 TODO）；情感/音色选型要试 | 第二个 |
| **剪辑时间线** | ★★★ | 现只有"幻灯片拼接、时长固定 4s"；转场/字幕/音画对齐是独立领域 | 最后，且**先做"能对齐 + 能出字幕"两个最小闭环** |

---

## 5. 画布回环 → 怎么解决

### 结论：**用"静态近似 + 局部修正"消除 80% 回环需求，剩下的用"有限轮次"兜住**

### 5.1 先看清回环到底从哪来

唯一真实回环是：
```
配音时长  ← 需要 →  剪辑点
剪辑点    ← 需要 →  配音时长
```
本质是"**音画对齐要迭代**"，不是真的需要无限循环。

### 5.2 三层解法（从简到繁，建议只做前两层）

**第一层：拆成顺序（消除回环）**
```
配音 → 产出 VoiceTrack.duration_s → 剪辑 → 按 duration 决定剪辑点
```
配音先跑，拿到每句实际时长；剪辑再按"取 max(画面时长, 配音时长)"对齐。
**这在 v2 完全够用**——因为配音是"读已有台词"，不需要看剪辑结果。

**第二层：剪辑内部局部修正（不触发全局重跑）**
音画差一点时，用**剪辑节点内部的规则**消化，而不是回边：
- 语速微调（TTS 支持 speed 参数，±10% 内听感可接受）
- 插入停顿、画面拉伸（`rhythm_plan` 允许的时长区间内）
- 画面定格补帧（配音比画面长时）

这些都在**剪辑节点内部解决**，用户看到的是"已对齐"，而不是"跑第二轮"。

**第三层（仅在必要时）：有限轮次迭代**
如果确实需要全局重跑（比如配音重录导致节奏变了）：
```python
# 注册表标记可迭代组 + 最大轮次
Stage(iters_with=["voice_director"], max_rounds=2)
```
- 拓扑仍是 **DAG**，但运行时**按轮次重复执行子图**
- 每轮带 `round_index`，超过 `max_rounds` 转**人工介入**
- 好处：拓扑可校验、执行可追踪、不会无限循环

### 5.3 画布上怎么表现

| 情况 | 画布表现 |
| --- | --- |
| DAG 内的依赖 | **实线箭头**（可拖、可校验） |
| 迭代组 | **虚线框 + 轮次指示**（"第 2 轮"），**不允许用户手画环** |
| 手动修正 | 节点内按钮（"重录这句配音"），不产生边 |

**关键设计决定：画布禁止用户画环，回环能力由"迭代组"配置提供。**
这样 React Flow 的图校验、后端拓扑校验都能保持简单且一致。

### 5.4 与执行引擎的配合

LangGraph 本身支持环（条件边），所以第三层可实现：
```python
g.add_conditional_edges("editor", route_round,
    {"retry": "voice_director", "done": "distributor"})   # 带上 round_index 与上限
```
但**必须显式设上限**，否则重演 `route_quality()` 那种"重试 ≤3"的隐式规则，
在昂贵模型上就是烧钱（见 `07-risks §1`）。

---

## 6. 五个问题的相互依赖（排序建议）

```
① 删历史数据（§3）           ← 最先，因为它让其他都变简单
     ↓
② 定注册表三层结构（§3.4）    ← 依赖①（无兼容负担）
     ↓
③ 单例 + 上下文改造（§2）     ← 依赖②（新代码直接用新方式）
     ↓
④ 唯一"串行"进 provider（§1） ← 依赖③（并发度由 provider 声明）
     ↓
⑤ 三条管线：契约 → 假实现 → 真实现（§4）
     ↓
⑥ 回环：先拆顺序，必要时有限轮次（§5）  ← 依赖④⑤
```

---

## 7. 执行前需要你确认

| # | 确认项 | 我的建议 |
| --- | --- | --- |
| 1 | `data/.little_crew.key` 删还是留 | **先解密体检**看里面有没有真实供应商 key，再决定 |
| 2 | 备份库 `Temp\ds6-recon\...db` 保留 | **保留**（对照用，不占空间） |
| 3 | 注册表用三层（Role/Panel/Node） | **采用**（避免返工） |
| 4 | 画布禁止用户画环 + 迭代组配置 | **采用**（校验简单、可追踪） |
| 5 | 是否先做"接口 + 假实现"让画布跑通 | **采用**（不阻塞前端） |
| 6 | 五个问题的执行顺序（§6） | 按①→⑥ |
