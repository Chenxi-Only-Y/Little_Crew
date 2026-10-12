# 历史数据删除记录

> 执行时间：2026-10-02（用户确认「以前历史数据全部删除，按新表 16 职业」）。
> 目的：为 v2 重构提供干净起点，消除旧 key / 旧目录名的兼容负担。

---

## 1. 删除前记录：会丢失的运行时配置

`api_settings` 表删掉后，这些**运行时选择会回到 `.env` / `config.py` 默认值**。
删除前实测解出的值（密文行已解密）：

| 键 | 删除前的值 | 删除后回退到 | 影响 |
| --- | --- | --- | --- |
| `video_max_duration` | **8** | `settings.video_max_duration` = **15** | ⚠️ 本地模式下 15 超过 10s 安全上限，**必 OOM** |
| `video_resolution` | `768P` | `768P` | 无 |
| `max_images` | **1** | `settings.max_images` = **8** | 出图量从 1 变 8，成本/耗时上升 |
| `video_count` | **1** | `settings.video_count` = **0**（跟图片数） | 视频段数变多 |
| `image_quality` | `standard` | `standard` | 无 |
| `video_quality` | `768P` | `768P` | 无 |

另有明文行：`alice` → `llm_provider=zhipu`、`llm_model=glm-4`（也一并删除）。

**密钥安全性已核实**：那行密文解密后**只有上述生成参数，没有任何 API Key**，
因此 `.little_crew.key` 与数据库**可以一起删**（见 `08-solutions.md §3.2`）。

> ⚠️ 本轮执行时**保留** `.little_crew.key`（一个 44 字节文件）：
> 因为它不含任何秘密、留着无害，且保留它可避免将来手工恢复设置时触发密钥重建。
> 若你希望彻底干净，可直接删除该文件（应用会按需重新生成）。

---

## 2. 删除清单

| 对象 | 删除前 | 处理 |
| --- | --- | --- |
| `data/little_crew.db` 全部表数据 | 13 sessions / 10 users / 3 api_settings / 1 profiles / 2 critic_history | **清空表数据**（保留表结构，应用启动即可用） |
| `data/outputs/**` | 166 文件 / 116.2 MB | **删除**（旧职业命名目录，v2 结构不兼容） |
| `data/verify_token.txt` | 43 B | **删除**（测试残留） |
| `data/.little_crew.key` | 44 B | **保留**（不含秘密，见 §1 说明） |
| `data/e2e_real_drive.py`、`data/drv_review3.py` | 2.2 KB / 3.0 KB | **保留**（游离在 `data/` 而非 `scripts/`，属历史遗留；删除与否待你决定，不阻断重构） |
| `.env` | 2.0 KB | **保留**（真实配置） |
| `docs/recon/**` | 9 份 | **保留**（重构依据） |

---

## 3. 备份位置（删除前均已备份）

| 备份 | 路径 |
| --- | --- |
| 数据库（删除前快照） | `C:\Users\OnlyY\AppData\Local\Temp\ds6-recon\little_crew_20261002_144228.db` |
| 数据库（本轮删除前快照） | `C:\Users\OnlyY\AppData\Local\Temp\ds6-recon\little_crew_<本轮时间戳>.db` |
| 未提交代码改动 | `C:\Users\OnlyY\AppData\Local\Temp\ds6-recon\uncommitted-20261002_151803\` |

---

## 4. 删除后必须记住的三件事

1. **重新登录**：`users` 表已清空，原有 token 全部失效，需重新注册/登录。
2. **重新配置 provider**：尤其是 `video_max_duration` —— 若用本地模式，**必须设回 ≤10**，
   否则会撞上 8GB 显存的安全上限（`05-roles-v2.md` / `local-gen` README 的实测边界）。
3. **产物目录从零开始**：`data/outputs/` 会按新结构（`<uid>/<部门>/<职业>/<会话>/`）重建。
