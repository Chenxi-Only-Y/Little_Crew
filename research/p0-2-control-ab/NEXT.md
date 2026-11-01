# NEXT · P0-2 扩镜实验（2026-11-01 停在这里）

> **下次打开先读这一页。** 状态、素材在哪、下一步怎么跑，全写在这里。
> 做完请删掉本文件。

---

## 一、已经做好的

| 项 | 状态 |
|---|---|
| **3 镜整集跑通**（LLM 剧本 + 白模 + 控制图 + ComfyUI 后端） | ✅ `outputs/_E8` · 470.9s · 覆盖率 100% |
| **三镜控制图** | ✅ `outputs/_E8/previs/SH-E801-S01-00{1,2,3}/previs_control.png` |
| **场景越权门禁开火** | ✅ 真跑命中 1 镜：`SH-E801-S01-001` 引入「花田」（剧本只有山坡/岩石/木屋） |
| 单镜 A/B 脚本 | ✅ `research/previs-ab/step3_render_ab.py`（单镜版） |
| 强度扫描脚本 | ✅ `step4_strength_sweep.py` |
| 硬指标脚本 | ✅ `metric_hard.py`（**实测不可用**，见下） |
| 盲判工具 | ✅ `make_blind_set.py` |

**ComfyUI 8188 已起（后台任务 `pwsh-f181f93eb48b4e40`），大概率仍在跑。**

---

## 二、下一步：把单镜版改成三镜版

`step3_render_ab.py` 现在是「一镜、A 与 B 各渲一次」。改成：

```
for sid in (SH-E801-S01-001, -002, -003):
    A：不调 apply_control                 → 存 A_<sid>.png
    B：apply_control(strength=0.5)        → 存 B_<sid>.png
    记录：像素差 / 地平线行
```

- 提示词从 `outputs/_E8/delivery/episode.master.json` 里逐镜取（**逐字复用**）
- 控制图从 `outputs/_E8/previs/<sid>/previs_control.png` 取，先 copy 进 `D:\ComfyUI\ComfyUI\input\`
- 常量照抄 `step3`：UNET `z_image_turbo_int8_convrot.safetensors` ·
  PATCH `...lite-2601-8steps.safetensors` · CLIP `qwen_3_4b_fp4_mixed` · VAE `ae`

**判据**：三镜的 B(0.5) 是否**都** ≥ A 的盲判分。
- 都 ≥ → E7 从「单镜」升级为「三镜一致」，可下断言
- 有翻转 → 改成「依镜头而定」，这也是有价值的诚实结论

**盲判**：出 6 张图（3×A/B）→ 跑 `make_blind_set.py` 改造版 → 固定种子打乱 → 打分 → 才对答案。

---

## 三、不要重犯的三个错

| 错 | 教训 |
|---|---|
| **用像素差当"构图听话度"** | 已实测失效：0.35 的像素差 80%，画面已经对了 |
| **用"紫色占比/地平线"** | 也已失效：1.0 那张上半是高饱和土坡，前提崩了 |
| **一次只变一个变量** | 别"跑两集对比"——提示词会变，差异无法归因 |

**现在唯一可信的判据是「预注册盲判」**（`make_blind_set.py` 里那两问）。

---

## 四、另一条待办（与本实验无关但记着）

**E3 重做**：返工 n=10 → 20~30，**并记录每次退回的原因文本**，
看「指令质量」与「改善/变差」是否相关。这条不需要 GPU，纯记录分析。

---

## 五、文档状态

```
HEAD 7eff9f2（或更新）  alpha.155  全部已推送
一~十章齐：六=实测证据 · 七=竞品边界 · 八=局限与负结果 · 附录 A/B/C/D=历史
```
