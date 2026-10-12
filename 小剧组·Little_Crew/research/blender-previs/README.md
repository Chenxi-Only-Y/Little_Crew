# 白模预演渲染（Blender headless）

**用途**：`docs/00-项目策划书.md` §3.8「3D 白模」的实现。
把**语义镜头规格**渲染为**控制序列**（灰度 / 深度），供 H3 ControlNet 使用。

**定位**：这是 `research/` 下的验证脚本，**不是平台代码**。
理由见 `STRUCTURE.md §1.1`：外部工具与其脚本放项目外/调研区，
验证通过后再规范化为 `runtime/channels/blender.py`。

---

## 为什么是这三点设计

| 决策 | 理由（实测数据） |
|---|---|
| **Workbench（CPU）** 而非 EEVEE | Workbench **0.055 s/帧**、**+88 MB 显存**；EEVEE 3.0 s/帧、+1155 MB。本机 8 GB 显存要留给 ComfyUI（已占 4.6 GB），**预演不能与生成抢显存** |
| **参数化几何** 而非文生 3D | 文生 3D 产**带纹理**的资产且**一致性差**（同一角色生成两次得到两个人）。白模只要几何，且要求"参数相同 ⇒ 几何必然相同" |
| **逐帧重建** 而非骨架驱动 | 每帧约 40 个图元、几十毫秒；换来**确定性**（`params, action, t` 唯一决定几何）与**可缓存**。白模不需要蒙皮变形 |

**关键发现**：`ViewLayer.use_pass_z` 是**与渲染引擎无关**的渲染通道 ——
**Workbench 也能输出深度图**，故不必为深度图付出 EEVEE 的 55 倍时间。

---

## 用法

```powershell
# 渲染一个镜头（输出 gray/ 与 depth/ 两套 PNG 序列）
D:\Blender\blender.exe --background --factory-startup `
  --python render_previs.py -- shot.example.json

# 合成 MP4（FFmpeg 在 D:\AI\FFmpeg）
D:\AI\FFmpeg\bin\ffmpeg.exe -y -framerate 24 -i "out\shot\gray\f%05d.png" `
  -c:v libx264 -pix_fmt yuv420p -crf 18 out\shot_gray.mp4

# 排查姿态不生效（默认关闭）
$env:PREVIS_DEBUG = "1"
```

## 镜头规格（`shot.example.json`）

```jsonc
{
  "outDir": "...",
  "resolution": { "width": 832, "height": 480 },
  "character": {
    "height_cm": 168,
    "build": "slim",              // slim | normal | athletic | heavy | child
    "head_ratio": 7.6,            // 身高 = 几倍头高
    "shoulder_width_m": 0.40,     // ⚠️ 单位见下
    "hair": "long_straight",
    "arm_spread": 7.0, "arm_bend": 10.0, "stance_width": 1.0
  },
  "shot": {
    "shot_size": "full",          // extreme_wide|wide|full|medium_full|medium|medium_close|close|extreme_close
    "angle_deg": 2,               // 俯仰（正=俯拍）
    "azimuth_deg": 18,            // 水平方位（0=正面，90=侧面）
    "lens_mm": 50,
    "duration_sec": 5.0,          // 自动对齐到 17n+5 帧
    "action": "walk",             // 见下方动作模板
    "camera_movement": "dolly_in",// static|dolly_in|dolly_out|truck_left|truck_right
    "path": "enter_from_left",    // static|enter_from_left|exit_right|walk_forward|approach_camera|retreat
    "move_scale": 0.15, "path_scale": 0.45
  }
}
```

### ⚠️ 肩宽的单位必须显式声明

早期版本用「数值 ≤ 1.0 就当成系数」猜单位，结果 `0.40`（本意 0.40 **米**）
被当成身高系数，算出 **0.504 m** 的肩宽 —— 而**从渲染图上看不出来**。

| 写法 | 单位 |
|---|---|
| `shoulder_width_m: 0.40` | **米**（推荐） |
| `shoulder_width_ratio: 0.235` | 身高系数 |
| `shoulder_width: 0.40` | 米（向后兼容） |
| 不给 | 按体型系数 × 身高 |

超出合理区间（0.18~0.30 × 身高）会**告警并夹紧**。

## 动作模板（8 个）

`stand` · `walk` · `run` · `look_back` · `point_forward` · `sit_down` · `wave` · `recoil`

设计原则与平台一致：**LLM 只选模板名，不产生坐标。**
数值空间推理对 LLM 不可靠 —— 让它输出关节角会得到"看似合理但穿模"的结果。

模板是**关键帧列表**（`[(t, pose), ...]`，t ∈ [0,1]），线性插值，**不含随机性**。

### 姿态参数白名单（`POSE_KEYS`）

```
arm_swing_L/R   arm_out_L/R   elbow_bend_L/R
leg_swing_L/R   knee_bend_L/R
spine_lean  spine_twist  head_pitch  head_yaw  pelvis_drop
```

- 未知键 **告警并忽略**（模板库会扩展）
- 越界值（> ±170°）**报错**（那是配置写错，不是新模板）

---

## 两个已修的真实缺陷（都很难从图上看出来）

| # | 缺陷 | 表现 | 根因 |
|---|---|---|---|
| **1** | 躯干偏移加了两次 | 上半身矮了 **0.254 m** | `chest = pelvis + half` 应为 `chest = spine + half`。渲染出来只是"矮一点"，靠"默认姿态必须与原解算一致"的断言才发现 |
| **2** | `_new_scene()` 删掉了相机 | 第二帧起 `Cannot render, no camera` | 逐帧重建时相机是**基础设施**，不属于角色几何，两者生命周期不同 |

另有一个**采样陷阱**（非缺陷）：动作模板 `t=0` 是**起始站姿**，
取首帧会误以为"姿态没生效"。判断姿态是否生效要取**中间帧**或**姿态峰值帧**。
`PREVIS_DEBUG=1` 可打印逐帧网格包围盒来确认。

---

## 实测数据

| 项 | 结果 |
|---|---|
| 单帧（静态） | **0.055 秒** @832×480 |
| 单帧（逐帧重建姿态） | **0.14 秒** @832×480（124 帧约 17 秒） |
| 显存占用 | **+88 MB** |
| 帧数对齐 | **124 = 17×7+5** ✅ |
| 输出帧率 | 固定 **24 fps** |
| 一集 60 镜预演 | 约 **17 分钟**（H3 生成需数小时） |