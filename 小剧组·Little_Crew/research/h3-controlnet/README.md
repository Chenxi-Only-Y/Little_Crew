# H3 ControlNet 官方样片归档

**用途**：docs/00-项目策划书.md §3.8.4「可行性依据」的实物证据。

**来源**：[alibaba-pai/MiniMax-H3-Fun-Controlnet-Union-2.0](https://huggingface.co/alibaba-pai/MiniMax-H3-Fun-Controlnet-Union-2.0)
（阿里 PAI，来自 VideoX-Fun 线；授权：MiniMax H3 Community License）

**下载日期**：2026-10-11

---

## ⚠️ 媒体文件**不入库**（只入库本文）

本目录的 `*.mp4` / `*.png` 共 **39.42 MB**，在 `.gitignore` 里被排除。理由两条：

| 理由 | 说明 |
|---|---|
| **可重新获取** | 它们是**他人发布的公开样片**，不是我们的产物；按来源 URL 可重新下载 |
| **属于他人版权** | MiniMax H3 Community License 覆盖，与项目自身的 Apache-2.0 不同 |

这与既有规范一致 —— `STRUCTURE.md` §1 对 `research/**/src-*/`（第三方源码快照）
用的正是同一条判据：**可重新下载的派生内容不入库**。

**复现方式**：按上面的来源 URL 下载，放进本目录，文件名保持原样
（`asset_*.mp4` = 控制输入，`results_*.mp4` = 生成结果）。
下面的「文件说明」「关键实证结论」是**从这些样片读出来的结论**，不依赖文件在库。

---

## 文件说明

| 文件 | 内容 |
|---|---|
| `all_conditions.png` | **8 种控制条件的对比总图**（左=控制输入，右=生成结果） |
| `asset_*.mp4` | 官方公布的**控制输入**样片 |
| `results_*.mp4` | 官方公布的**生成结果**样片 |

对照关系：`asset_X.mp4` 是喂给模型的，`results_X.mp4` 是模型产出的。

## 8 种控制条件

| 条件 | 控制信号 | 对 3D 白模的意义 |
|---|---|---|
| **Gray** | 灰度（亮度） | ⭐ **白模渲染出来本就是灰度图，可直接用** |
| **Depth** | 单目深度图 | ⭐ 更精确的空间约束 |
| **Layout** | 边界框布局 | 站位控制（彩色框即可，无需 3D） |
| Canny / HED / MLSD | 边缘类 | 轮廓控制 |
| Pose | DWPose 骨架 | 动作控制 |
| Scribble | 手绘草图 | 手绘预演 |

## 关键实证结论

取自 `all_conditions.png` 与逐帧比对：

1. **Depth 控制有效**：狗的位置、朝向、**大小变化**严格跟随深度序列；
   背景透视一致；生成画面质量**未因控制而退化**
2. **但外观每帧不同** —— Depth 管「东西在哪、多大、怎么动」，
   **不管「长什么样」**
3. 因此：**参考图锁「长相」，白模锁「空间」，二者互补而非替代**

## 可调参数

`control_context_scale`：`1.0` 最强控制 / `0.0` 完全关闭。
这使"白模影响多大"成为**可调参数**而非硬约束。

## ⚠️ 版本陷阱（官方明示）

**v1 的配置配 2.0 的权重会静默失败。**
旧配置只建 5 个控制块，`load_state_dict(strict=False)` 会把
`control_blocks.5~9` 当意外键丢弃 —— **输出是错的，但不报错**。

v2.0 必须用 `minimax_h3_control_inpaint_post_norm.yaml`。

## 相关的 ComfyUI 格式权重

官方 ComfyUI 重打包仓库：[Comfy-Org/MiniMax-H3](https://huggingface.co/Comfy-Org/MiniMax-H3)

| 文件 | 体积 |
|---|---|
| `model_patches/minimax_h3_fun_controlnet_union_2.0_pruned_int8_convrot` | 4.22 GB |
| `model_patches/minimax_h3_fun_controlnet_union_pruned_int8_convrot` | 2.14 GB |

（跑通生成还需底模 19.53 GB + 文本编码器 14.61 GB + VAE 2.62 + 0.56 GB，合计 39.46 GB）