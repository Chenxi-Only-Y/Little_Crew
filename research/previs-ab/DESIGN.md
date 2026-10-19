# 白模 A/B：白模首帧到底能不能约束构图？

> **预注册文档。判据在跑之前写定，跑完不许改。**
> 跟着 `research/agent-ab/DESIGN.md` 的惯例：先定判据，再跑，跑完照实报。

---

## 一、要回答的问题

> **把白模首帧作为 Z-Image ControlNet 的输入，生成图的构图是否更接近白模？**

这决定了白模这条线的价值：

| 若答案是"能" | 若答案是"不能" |
|---|---|
| 白模是**空间一致性的约束手段** | 白模只是**给人看的预演** |
| 论文里可以声称"空间可控" | 论文里只能声称"预演可读" |

**这是白模唯一的价值证明。** 前面七步（渲染器、校验器、agent、场景）都是准备。

---

## 二、两组

| 组 | 控制图 | 说明 |
|---|---|---|
| **A** | 无 | 纯文生图 |
| **B** | 白模首帧（`previs_first.png`，640×384） | `Z-Image-Turbo-Fun-Controlnet-Union-2.1-lite`（1.88 GB） |

### 必须固定不变的量

| 量 | 值 | 为什么 |
|---|---|---|
| prompt | **逐字相同** | 否则比的是提示词 |
| 随机种子 | **相同** | 否则比的是采样噪声 |
| 分辨率 | 640×384 | 与白模一致（否则首帧被缩放，构图必偏） |
| 采样步数 / CFG | 相同 | — |
| 模型 | `z_image_turbo_int8_convrot` | — |

> **A/B 的合法性只来自"只有控制图这一个变量不同"。**
> 任何一处没固定，结论就不成立 —— 而那种不成立是**看不出来**的。

---

## 三、预注册判据

### 判据 ①（主）：盲判构图接近度

把三张图并排：**白模首帧 / A / B**，由人判断"哪一张更像白模的站位与机位"。

三选一，**不许选第四项**：

| 选项 | 含义 |
|---|---|
| `B_more` | B 更接近白模 |
| `A_more` | A 更接近白模 |
| `same` | 分不出 |

**盲判**：A/B 的顺序随机，判定时不知道哪张是哪组。

### 判据 ②（辅）：人形区域重合度（IoU）

从生成图里估"主体区域"（与背景亮度/饱和度差异超过阈值的连通块），
与白模首帧的**人物矩形框**求 IoU。

**这只是代理指标**。项目里已有先例说明它会骗人：

> `research/lighting-effect/` 的两个代理指标**全部失败**在"局部强调"型灯光上
> （整图均值掩盖空间分布）。所以：

**② 只用于交叉验证 ①，不单独构成结论。**

### 判据 ③（反面对照）：控制是否真的生效

**A 与 B 的像素差**（归一化 L1）。用于区分两种"没差别"：

| 观察 | 解释 |
|---|---|
| A ≈ B（差 < 1%） | **控制通道没生效** —— 技术故障，不是白模无用 |
| A ≠ B，但都离白模远 | **控制生效但约束力弱** —— 白模用处有限 |
| A ≠ B，B 更近白模 | **控制生效且有约束力** —— 白模有用 |

**没有 ③，前两条都可能被"通道根本没通电"骗过去。**

---

## 四、判定规则（跑之前写定）

按顺序：

```
若 ③ 触发（A≈B）:
    → 结论 = 「控制通道未生效」，判据①②作废（不是白模无用，是接线问题）
否则若 ① 判 B_more 的镜数 ≥ ⌈2/3⌉:
    → 结论 = 「白模能约束构图」
否则若 ① 判 A_more 的镜数 ≥ ⌈2/3⌉:
    → 结论 = 「白模反而偏离」← 必须照实写，不许淡化
否则:
    → 结论 = 「差不多」← 这个分支是**预声明**的，不是事后找的台阶
```

**「差不多」一旦命中，论文里就不能声称"空间可控"。**

---

## 五、已知混杂因素（照实记录，不当作借口）

| # | 混杂 | 影响方向 |
|---|---|---|
| 1 | **15.8 GB 内存**，Z-Image int8 + ControlNet 要 offload | 可能引入数值差异，**但 A/B 同条件，方向不可预测** |
| 2 | ControlNet 用 **lite** 版（1.88 GB） | 约束力弱于 full（6.25 GB）→ **对 B 不利**，保守 |
| 3 | 白模人物是**一根圆柱** | 与真人轮廓差异大 → 对 B 不利，保守 |
| 4 | `medium` 景别裁切狠 | 白模与生成图都裁，比较窗口小 |

**其中 2、3 都是"对 B 不利"的方向** —— 如果这样还能判出 B_more，结论就更稳。

---

## 六、样本量

**先跑 3 镜**，作为试点（pilot）。

理由：这轮是**验通道通不通 + 判据能不能用**，不是要统计显著性。
3 镜跑完若 ①②③ 都能得出明确判定，再决定是否扩到 10 镜。

**3 镜不足以支持"统计显著"这类说法** —— 只能支持"通道有效/无效"这种定性判断。

---

## 七、不做什么（避免结论被稀释）

- **不做**多 seed 平均（那会掩盖控制图的作用）
- **不做**提示词调优（调了就不知道是白模还是提示词起效）
- **不换** 景别（都固定 `full`，避免裁切干扰）

---

## 八、跑法：**已探到 90%，卡在最后一个坎**

### 已经确认可用的（本轮实测，不用再摸）

```python
from little_crew.models.registry import Backend, BackendKind
from little_crew.runtime.channels.comfy import ComfyChannel
from little_crew.agent.messages import Shot
from little_crew.agent.ir_bridge import shot_to_ir
from little_crew.runtime.render import render_ir
from little_crew.compiler.compiler import ComfyCompiler

ch = ComfyChannel(Backend(kind=BackendKind.COMFYUI,
                          endpoint="http://127.0.0.1:8188",
                          is_local_by_default=True))
ch.available()        # -> (True, "ComfyUI 0.39.0")
ch.probe_catalog()    # -> dict（ComfyUI 原始 /object_info）

shot = Shot(frm="storyboard_artist", to="x", shot_id="SH-001",
            scene_id="S01", shot_size="full", camera_movement="static",
            action="stand", duration_sec=2.5, characters=("少女",),
            description="...")
ir = shot_to_ir(shot, episode_id="previs-ab",
                checkpoint="z_image_turbo_int8_convrot.safetensors",
                resolution=(640, 384), steps=8, cfg=1.0,
                sampler="euler", scheduler="simple")
# -> IR 节点数 7 ✓
```

**Z-Image 的三个模型名**（`episode.py` 的默认值，直接可用）：

| 用途 | 文件名 |
|---|---|
| unet | `z_image_turbo_int8_convrot.safetensors` |
| clip | `qwen_3_4b_fp4_mixed.safetensors` |
| vae | `ae.safetensors` |

### ✅ 已解决：`CapabilityMatrix` 怎么造

**关键函数 `probe_from_object_info`**（在 `little_crew.capability.capability`）：

```python
raw = ch.probe_catalog()                       # ComfyUI 原始 /object_info
mat = probe_from_object_info(raw, endpoint=EP, comfy_version="0.39.0")
# -> 970 个节点 ✓
comp = ComfyCompiler(mat, default_checkpoint=UNET, model_source_kind="model.source_zimage")
```

**`model_source_kind` 的合法值**（从 `compiler/*.py` 里 grep 出来）：

```
model.checkpoint · model.source_zimage · model.zimage_clip · model.zimage_vae
```

### 🔴 仍卡住的地方：**模型源节点类型不对**

```
编译失败：[MISSING_REQUIRED_NODE] 必需节点或模型文件缺失：
          CheckpointLoaderSimple.ckpt_name (DriftKind.FILE_MISSING)
```

成因链：

1. `shot_to_ir(...)` **只会生成 `CheckpointLoaderSimple`**（它没有"模型源类型"这个参数）
2. 而 `z_image_turbo_int8_convrot.safetensors` 在 ComfyUI 的 **`diffusion_models`** 目录里，
   只有 **`UNETLoader.unet_name`** 的枚举包含它 —— `CheckpointLoaderSimple` 看不到

实测确认：

```
UNETLoader.unet_name 含 z_image_turbo_int8_convrot  →  True
```

3. **改写模型源的那段代码在 `episode.py` 里**（「① 模型源」，
   把 `n_ckpt` 换成 `UNETLoader`，并把 prompt 节点的 `clip` 端口显式指向新源），
   `shot_to_ir` 不做这件事

### 下一轮最短路径

| 方案 | 做什么 | 成本 |
|---|---|---|
| **A（推荐）** | 把 `episode.py` 里「① 模型源」那段抽成一个独立函数（如 `runtime/model_source.py`），A/B 脚本直接调它 | 半小时，且**对生产代码是纯重构** |
| B | 在 A/B 脚本里复制那段节点改写 | 半小时，但**复制即漂移** |

**选 A** —— 那段逻辑本来就该独立，因为"哪些模型源可用"是 Z-Image 通路的公共知识，
不该埋在 `EpisodeRunner` 的私有方法里。

### 下一轮的第一件事（二选一）

| 方案 | 做什么 | 风险 |
|---|---|---|
| **A** | 写那个转换器：`probe_catalog()` 的原始 dict → 归一化场景 → `from_snapshot()` | 要读 `NodeSpec`/`PortSpec` 的完整要求，约 1 小时 |
| **B** | 找到 `episode.py` 整集跑时矩阵的来源（顺着 `EpisodeRunner.__init__` 的 `renderer` 参数找） | 若有现成的，10 分钟 |

**先试 B** —— 如果整集能跑，那说明矩阵在别处已经造好了，只是我没找到入口。

### 一个可能的捷径

`tests/` 里必然有人在测编译器 —— **它们怎么造矩阵，就照着抄**。
本轮没找到 `NodeSpec(`，说明它们可能用 fixture JSON 或 mock。
**读 `tests/test_compiler*.py` 是下一轮最快的入口。**

## 九、产物（跑完后）

| 文件 | 内容 |
|---|---|
| `raw.json` | 每镜：prompt / seed / A 图路径 / B 图路径 / ③的像素差 / ①②的读数 / 盲判结果 |
| `SUMMARY.md` | 按 §四 的规则给结论，**含所有不利数据** |
| `evidence/` | 每镜的 白模 / A / B 三图 |

**判据改过就要在 SUMMARY 里写明改了什么、为什么、偏向哪一方** ——
与 `agent-ab/DESIGN.md §11` 同一要求。
