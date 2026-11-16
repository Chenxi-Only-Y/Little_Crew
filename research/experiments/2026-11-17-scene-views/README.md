# 场景空间视图（Tier1/Tier2）· 2026-11-17

- `scene_views_episode.json`：《最后一班》12 镜 4 场全跑：**4 场全部 Tier2**（3 镜/场 + 复用信号）· 16/16 图 · ¥0.80
- `scene_views_qc.json`：单场验证（空景 QC 开）：4/4 图通过 VLM 人物检测 · ¥0.20
- `base/reverse/oblique45/topdown.png` + `contact_sheet.jpg`：同一场的四视图
- 口径：正俯图只作空间草图/ControlNet 输入，不回灌 img2img；空景 QC 检出人物会重试一次（更硬约束）
