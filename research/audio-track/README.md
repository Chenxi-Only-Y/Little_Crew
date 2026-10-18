# `research/audio-track/` —— **成片有声**（H3 音画同出，实测）

**日期** 2026-10-18 · **版本** `0.4.0-alpha.50` · **成片** `audio_demo.mp4`

---

## 一句话结论

**不需要 TTS。** local-gen 的 H3 是 `FL2VA`（first-last -> video + **audio**），
**音轨与视频同一遍前向产出** —— 产物 mp4 自带 AAC 音轨，零额外成本。

**实测成片**：3 镜 / 13.41 秒 / 16.35 MB / **h264 640×384 24fps + AAC 32kHz 立体声 176 kb/s**。

---

## 一、怎么发现的（用户提示 + 实测确认）

用户指出"部分生视频模型有音频"。顺着查：

| 证据 | 内容 |
|---|---|
| `local-gen/app/engines/h3.py` | `from diffsynth.pipelines.minimax_h3_audio_video import MiniMaxH3Pipeline`<br>`video, audio = pipe(...)`<br>`write_video_audio(... audio=audio, ...)` |
| 权重目录名 | `.../MiniMax-H3/FL2VA/processor` —— **FL2VA = First-Last to Video + Audio** |
| 注册表 | `minimax-h3` 的 `capability.native_audio=True`（**早就声明了**） |
| `h3-native-audio` 条目 | "零额外成本：音轨与视频同一遍前向产出，不优先于独立 TTS" |

**但"代码看起来会出音频"与"产物里真有音轨"是两件事** —— 中间可能被丢弃、
mux 失败、或参数为 0 导致静音。**所以不看代码，看文件。**

### 实测探测（不依赖 ffprobe —— 本机没装）

```
vide handler : True
soun handler : True
编解码器      : ['avc1', 'mp4a']      ← H.264 视频 + AAC 音频
trak 数量     : 2                     ← 两条轨
```

**✅ H3 确实音画同出。**

---

## 二、接线：注册表里一直有，但没有一条能走通的通道

| 后端 | 状态 |
|---|---|
| 本机 ComfyUI（8188） | ❌ **没起** |
| MiniMax 官方 API | ❌ **没有 `MINIMAX_API_KEY`** |
| **local-gen（8100）** | ❌ **根本没登记** ← 缺的是这条 |

> **声明了却走不通，比没声明更糟** —— 它让人以为已经接好了。

### 三处改动

| 位置 | 改动 |
|---|---|
| `models/registry.py` | 给 `minimax-h3` 加**第三个后端**：local-gen 异步通道 |
| `runtime/channels/http.py` | 提交时也认 **`job_id`**（local-gen 用的就是这个键） |
| `review/observe.py` + `ops.py` | 新增容器探测 `_media_flags` + 断言 `audio_track_present` |

**第二处是个真 bug**：代码只认 `extract_path(raw, "id") or extract_path(raw, "task_id")`，
而 local-gen 返回 `{"job_id": "...", "status": "queued"}` ——
任务会被判"服务端没给任务 id"而**每次提交都失败**，看起来像服务不可用。

---

## 三、新断言 `audio_track_present`（第 13 个）

**为什么需要它**：`capability.native_audio=True` 只是一句**声明**。
在此之前**没有任何断言能验证"成片真的有声音"** ——
`audio.tts` 是一条编译规则，它描述"应该去合成语音"，
而不是"产物里必须有一条音频轨"。

> **声明与验证是两件事。**

### 判据（容器层的事实，不是推测）

| 容器 | 判据 |
|---|---|
| MP4/MOV | `ftyp` 头 + `vide` / `soun` handler、`mp4a` 标记 |
| Matroska | EBML 魔数 + `V_*` / `A_*` |
| WAV | `RIFF....WAVE` |

**不依赖 ffprobe**（本机没有）。只找标记，不做完整解析 ——
足以回答"有没有音轨"，而不必实现一个 mp4 解析器。

⚠️ **它不判断音轨内容对不对**：只回答"有没有"。
声音是否对得上画面、音量是否合适，属 L2/L3 的范围。

---

## 四、4 镜短片（实际做了 3 镜）

### 为什么走 **i2v** 而不是 t2v

H3 支持首帧图。把 Z-Image 静帧当首帧喂进去，得到的是：

```
我们控制的构图  +  模型补的运镜  +  模型自带的音轨
```

纯 t2v 会丢掉构图（那正是 `img2img` 否证过的那类问题）。
**i2v 是这里唯一合理的选择** —— 已用抽帧验证构图完整保留。

### 为什么只做 3 镜

单镜实测 **约 340 秒纯生成**（640×384 / 8 步 / 107 帧）。

| 规模 | 耗时 |
|---|---|
| 3 镜（本次） | **1011 秒 ≈ 17 分钟** |
| 24 镜（整集） | **约 2.6 小时** |

**把慢说清楚，比假装它能跑整集诚实。**

### 结果

| 项 | 值 |
|---|---|
| 片段 | 3 段，各 ~330-340 秒 |
| 每段探测 | `container=mp4` `hasVideo=True` **`hasAudio=True`** |
| 拼接 | ffmpeg concat `-c copy`（保留音轨） |
| **成片** | `audio_demo.mp4` · **13.41 秒** · **16.35 MB** |
| 视频流 | h264 (High) 640×384 24fps 10073 kb/s |
| **音频流** | **aac (LC) 32000 Hz 立体声 176 kb/s** |
| **算子判定** | **✅ 通过 —— mp4 容器，音轨与视频轨齐全** |

**176 kb/s 是有内容的音频** —— 静音轨的码率会低得多。

### 验证用的是**我们自己的算子**

```
review.observe.measure_artifact(成片)   -> hasAudio=True
review.ops.audio_track_present({}, ...) -> passed=True
```

**不靠肉眼，不靠"我觉得应该有"。**

---

## 五、⚠️ 这次没做到的

| 项 | 状态 |
|---|---|
| 整集 24 镜全用 H3 | ❌ 未做（2.6 小时，且本机同时只能加载一个模型） |
| 把 H3 接进 `EpisodeRunner` 主流程 | ❌ 未做 —— 目前是独立脚本（见下） |
| 音轨内容质量评估 | ❌ 未做（只验"有没有"，不验"对不对"） |
| 第 4 镜 | ❌ 静帧被前一次 `--fresh` 覆盖了，只做了 3 镜 |

**主流程未接的原因**：`EpisodeRunner` 目前是"Z-Image 静帧 -> ffmpeg 补运动 -> 拼"，
而 H3 是"静帧 -> 视频+音频"。两条路的 IR 与编译规则不同，
硬塞进去会把已经跑通的整集链路搞坏。**独立脚本先证明可行性，再接主流程**——
这与项目一贯的做法一致（先量，再改）。

---

## 六、复现

```powershell
# 1) 探测（只需 5 分钟，验证 H3 是否真的出音轨）
python research\audio-track\probe_h3_audio.py

# 2) 3 镜短片（约 17 分钟）
python research\audio-track\make_audio_demo.py
```

前置：local-gen 在 `127.0.0.1:8100` 运行。**不需要任何云端 key。**

---

## 七、文件

| 文件 | 内容 |
|---|---|
| `audio_demo.mp4` | **成片（有声）** |
| `probe.json` / `demo.json` | 探测与成片的完整证据 |
| ~~`h3_probe.mp4`~~ / ~~`clips/`~~ | **未入库** —— 中间产物，可由脚本重跑；成片已包含其内容（16 MB 不值得进版本库） |
| `frame_2s.jpg` / `frame_12s.jpg` | 抽帧（验证构图保留） |
| `clips/clip0*.mp4` | 3 段原始片段 |
| `probe_h3_audio.py` / `make_audio_demo.py` | 可复跑脚本 |


---

## 附录：异步通道探针（2026-10-18）

`probe_async_channel.py` 跑通了 `HttpChannel` 的 **submit → poll → collect** 全程 ——
这是 M3 的地基（`alpha.52` 起一直欠着，因为演示脚本走直连 urllib，
`h3_video_check` 用假渲染器，通道从没真跑过）。

### 结果

```
ok = True   耗时 505.2s   产物 7.95 MB
容器 mp4 · 视频轨 ✓ · 音轨 ✓
```

### 拦住它的两个 bug

| # | Bug | 表现 |
|---|---|---|
| ① | `body_template` 写 `{duration}`，值给 `seconds` | 「未替换的占位符 ['duration']」，**第一秒就失败** |
| ② | **轮询紧循环，没有 sleep** | `WinError 10048`（地址只能使用一次） |

**② 是本项目最像"外部故障"的一个自造故障**：报错指向网络、指向服务端，
而实际是**我们自己把 Windows 临时端口打光了** —— 130 秒发了 5725 次连接（44/秒），
`TIME_WAIT` 堆到 21427，超过 16384 的端口池。

修法：`_await` 里 `poll` 前加 `time.sleep(self.poll_interval_s)`
（`poll_interval_s` 原本存了却从未使用）。

### 文件

| 文件 | 内容 |
|---|---|
| `async-probe/*.mp4` | 产物（**带音轨**，7.95 MB） |
| `async-probe.json` | 入参、耗时、容器探测结果、断言结论 |
| `probe_async_channel.py` | 可复跑（约 8 分钟） |

⚠️ `async-probe/cache/` **不入库** —— 那是 `CachingRenderer` 的分层内容缓存，
一次探针就会带进 3 份同一个 mp4（工作副本 + t1 + t2 = 23.84 MB）。
证据留在产物与 json 里。
