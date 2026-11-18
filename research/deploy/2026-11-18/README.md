# Docker 镜像 `little-crew:0.8.6` 实构建 + 冒烟证据（2026-11-18）

> 收尾动作（HANDOFF 待做队列第 5 项）：全量回归 PASS 后统一重建镜像、打 tag。
> 依据 `docs/21-部署与外部服务.md` §八「镜像更新策略」——**不随每个小版本重建**，只在收尾/里程碑重建。

## 构建

```powershell
cd D:\AI\ds.8\Little_Crew
docker build -t little-crew:0.8.6 --build-arg APK_MIRROR=mirrors.aliyun.com .
```

| 项 | 结果 |
|---|---|
| 基础镜像 | `python:3.12-alpine`（平台纯 Python，零第三方运行依赖） |
| 构建参数 | `--build-arg APK_MIRROR=mirrors.aliyun.com`（只换源域名，不改依赖版本） |
| 构建耗时 | **300.1s**（含 ffmpeg 安装；本机首次拉层） |
| 镜像大小 | **269 MB**（与上一版 0.4.1 的 268MB 同量级 → 没把模型/外置服务塞进来） |
| 镜像 ID | `5219b37b8ea3` |
| 旧镜像 | `little-crew:0.4.1` 已按 §八「旧标签不保留多份」删除 |

原始日志：`build-log-tail.txt`（完整日志在构建机 `%TEMP%`，本目录留尾部与命令）。

## 冒烟（容器实跑）

```powershell
docker run -d --name lc-smoke -p 8788:8787 little-crew:0.8.6
```

| 检查 | 结果 | 证据 |
|---|---|---|
| 容器状态 | `Up (healthy)`（镜像自带 `HEALTHCHECK /healthz`） | `container-status.txt` |
| `GET /healthz` | `200 {"ok": true, "version": "0.8.6"}` | `healthz.txt` |
| `GET /v1/capabilities` | `roster=19 · inChain=11`；模型/端点/接口面齐全 | `capabilities.json` |
| 容器内 `doctor --json` | **blocking=0**（平台本体 OK；外置服务全 ✗ 但 `external/blocking=False`） | `doctor.json` / `doctor-summary.txt` |
| 容器内注册表 | `version 0.8.6 · roster 19 · registry 19`（插件化 core_crew 在镜像内正常加载） | `container-roster.txt` |
| 容器内存 | ~15.92 MiB（idle） | `mem.txt` |
| 镜像布局 | `/app` = `src / tools / pyproject / README / LICENSE / outputs`；**无 `config`**（`.dockerignore` 挡住，密钥不进镜像） | `image-layout.txt` |

> 结论：**平台本体可独立部署**（外置服务全挂也能起、doctor 不阻塞）；
> 镜像 269MB 证明没有把模型/外置服务打进去；插件化后的 19 职位名册在容器内同样成立。
