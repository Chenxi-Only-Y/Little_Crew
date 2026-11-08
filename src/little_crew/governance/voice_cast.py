"""配音的两个纯函数：**音色绑定角色** 与 **时长对齐规则**（2026-11-06，§3.11）。

## 为什么先做纯函数

§3.11 列了四个必须解决的问题，其中三个是**规则问题**，不是"接一个 TTS"：

| 问题 | 本质 |
|---|---|
| 音色绑定角色 | 一张**确定性映射表**（同角色永远同一音色，跨集也一样） |
| 时长对齐 | 一个**判定规则**（拉长镜头 还是 调语速） |
| 跨集一致 | 映射表要能写进交付件，可复查 |

**规则不定，接多少引擎都会漂。**

## 本机可用音色（SAPI，离线）

    Microsoft Huihui Desktop   zh-CN  女
    Microsoft Huihui           zh-CN  女
    Microsoft Yaoyao           zh-CN  女
    Microsoft Kangkang         zh-CN  男
    Microsoft Zira             en-US  女

**男声只有一个** —— 这是硬约束，两个男性角色就得共用或改用女声演。
"""
from __future__ import annotations

#: 本机 TTS 现状（2026-11-06 实测）
#:
#: `breeze-tts-2`（GGUF + Vulkan）已跑通：24kHz 单声道，同种子**字节相同**。
#: 它有三种区分角色的手段：`--voice`（已存音色，本机只有 1 个）/
#: `--ref-audio`（要样本）/ **`--instruction`（文字描述，不要样本）**。
#:
#: 所以**角色区分走 `instruction`**，`--voice` 只作兜底。
#: SAPI 的预设音色保留在下面的池子里，作为降级后端用。
SAPI_POOL_NOTE = "SAPI 预设音色（降级后端用）"

#: 按性别 + 年龄分组的**声音描述**（给 breeze 的 `--instruction`）
VOICE_DESCRIPTIONS: dict[str, str] = {
    "少女": "年轻少女的声音，轻而快",
    "少年": "年轻少年的声音，清亮",
    "成年女": "成年女性的声音，沉稳",
    "成年男": "中年男人的声音，低沉而慢",
    "年长女": "年迈老妇人的声音，沙哑，说话很慢",
    "年长男": "年迈老人的声音，低沉沙哑",
}

#: 按性别分组的候选音色（按优先级排；索引靠前的更常用）
VOICE_POOL: dict[str, tuple[str, ...]] = {
    "f": ("Microsoft Huihui Desktop", "Microsoft Yaoyao", "Microsoft Huihui"),
    "m": ("Microsoft Kangkang",),
    "?": ("Microsoft Huihui Desktop",),
}

#: 对齐容差（秒）：差在这个范围内算"对上了"
ALIGN_TOLERANCE = 0.5
#: 允许的最大语速调整比例（超过就改拉镜头，不硬压语速）
MAX_SPEEDUP = 0.2


#: 本机 breeze 目前**只有一个**可保存音色（`voices/gentle_sister.breeze`）。
#: 这是硬约束：想"音色稳定"就所有人都得用它；想"角色音色不同"就必须再存几个。
#: 2026-11-08：用户反馈「音色一直在变」—— 优先**稳定**，角色差异改由语气承担。
LOCAL_VOICE_HINT = "gentle_sister"


def pick_voice(gender: str, index: int = 0, available=()) -> str:
    """按性别挑音色。`index` 是**同性别里的第几个角色**（让两个女角色不同音色）。

    `available`: 本机实际装了的音色（为空表示不校验）。
    """
    g = "m" if str(gender).lower() in ("m", "男", "男性") else "f"
    pool = [v for v in VOICE_POOL[g]
            if not available or v in set(available)]
    if not pool:                                   # 本机没装 -> 退回反性别
        pool = [v for v in VOICE_POOL["f"] + VOICE_POOL["m"]
                if not available or v in set(available)]
    if not pool:
        # 传入的是**本机已存音色**（不在 SAPI 池里）—— 全组共用一个，锁住音色
        avail = [str(x) for x in (available or ()) if str(x)]
        if avail:
            return avail[0]
        return ""
    return pool[index % len(pool)]


#: 情绪 -> 语气描述（拼在 instruction 后面）。
#:
#: 为什么需要它：**同一个角色在不同场景语气应该不同**（质问 vs 妥协 vs 哽咽），
#: 但音色**不能变**。所以音色由 `--voice` 锁死，语气靠这段文字调。
EMOTION_NOTES: dict[str, str] = {
    "平静": "语气平稳",
    "质问": "语气质问、带压迫感",
    "隐忍": "语气压着、很克制",
    "哽咽": "带哭腔、断续",
    "叹息": "带叹息、很累",
    "急切": "语速偏快、着急",
    "冷淡": "语气冷淡、不带情绪",
    "温柔": "语气温柔、放轻",
    "愤怒": "压抑着怒意",
    "迟疑": "边说边犹豫",
}


def emotion_note(emotion: str) -> str:
    """情绪 -> 语气短语。**认不出就返回空**（不猜、不乱加）。"""
    return EMOTION_NOTES.get(str(emotion or "").strip(), "")


def seed_for(name: str, base: int = 42) -> int:
    """给每个角色一个**固定种子**。

    目的：同一角色跨句的韵律抖动小一点（音色已由 `--voice` 锁死，
    种子管的是**韵律细节**）。**稳定**比"随机"重要得多。
    """
    s = str(name or "").strip()
    if not s:
        return base
    h = 0
    for ch in s:
        h = (h * 131 + ord(ch)) % 100000
    return base + h % 97


def describe(gender: str, age_band: str = "") -> str:
    """由性别 + 年龄段推出声音描述（给 `--instruction`）。

    年龄段用我们定妆表里已有的词（少女/青少年/中年/年长…）：
    **不新造一套词表**，避免两处不一致。
    """
    g = "m" if str(gender).lower() in ("m", "男", "男性") else "f"
    a = str(age_band or "")
    young = any(k in a for k in ("少女", "少年", "青少年", "年轻", "儿童"))
    old = any(k in a for k in ("年长", "老年", "老", "奶奶", "爷爷"))
    if old:
        return VOICE_DESCRIPTIONS["年长女" if g == "f" else "年长男"]
    if young:
        return VOICE_DESCRIPTIONS["少女" if g == "f" else "少年"]
    return VOICE_DESCRIPTIONS["成年女" if g == "f" else "成年男"]


def cast(characters, sheets=None, available=()) -> dict:
    """角色 -> 音色。**确定性**：同一批角色永远得到同一组音色。

    `characters`: 角色名列表（顺序无关，内部排序后分配）
    `sheets`: `{角色名: sheet字典}`，若含 `voice` 则优先（人工指定 > 自动）
    """
    names = sorted({str(c).strip() for c in characters or () if str(c).strip()})
    used: dict[str, int] = {"f": 0, "m": 0}
    out = {}
    for name in names:
        sheet = (sheets or {}).get(name) or {}
        gender = str(sheet.get("gender") or "")
        forced = str(sheet.get("voice") or "")
        desc = str(sheet.get("voiceDescription") or "")
        _desc_source = "sheet" if desc else "auto"
        if not desc:
            desc = describe(gender, str(sheet.get("ageBand") or ""))
        if forced:
            out[name] = {"name": name, "voice": forced, "gender": gender,
                         "instruction": desc, "seed": seed_for(name),
                         "source": "sheet" if desc else "auto"}
            continue
        g = "m" if gender in ("m", "男", "男性") else "f"
        idx = used[g]
        used[g] = idx + 1
        out[name] = {"name": name, "voice": pick_voice(g, idx, available),
                     "gender": gender, "instruction": desc,
                     "seed": seed_for(name),
                     "source": _desc_source if not forced else "sheet"}
    return out


def align(audio_seconds: float, shot_seconds: float,
          *, tolerance: float = ALIGN_TOLERANCE,
          max_speedup: float = MAX_SPEEDUP) -> dict:
    """台词长度 vs 镜头长度 —— **定一条规则，别每次拍脑袋**。

    规则（按顺序）：

    1 差在 ±tolerance 内 -> `ok`，不动
    2 **音频比镜头长**：
       - 超出 ≤ max_speedup -> `rate`：调快语速（Rate 越小越快，见 SAPI）
       - 超出更多 -> `extend`：**拉长镜头**到音频 + 0.3 秒（不硬压语速）
    3 **音频比镜头短** -> `trim`：镜头可缩到音频 + 0.5 秒，但不缩到 1 秒以下

    **为什么优先调语速而不是拉镜头**：拉镜头会改整集节奏（那是导演的决定）；
    小幅语速变化对观感影响更小。但**超过 20% 就不许再压** —— 那会听见"赶"。
    """
    a, s = float(audio_seconds), float(shot_seconds)
    err = round(a - s, 3)
    if abs(err) <= tolerance:
        return {"action": "ok", "error": err, "rate": 0.0,
                "newShotSeconds": round(s, 2), "reason": "差异在容差内"}
    if err > 0:
        over = err / s if s > 0 else 1.0
        if over <= max_speedup:
            # SAPI Rate: -10..10，负值更快；百分比 -> 档位
            rate = max(-10, min(10, -int(round(over * 100 / 5))))
            return {"action": "rate", "error": err, "rate": float(rate),
                    "newShotSeconds": round(s, 2),
                    "reason": f"音频长 {err:.2f}s（{over*100:.0f}%），调快语速"}
        return {"action": "extend", "error": err, "rate": 0.0,
                "newShotSeconds": round(a + 0.3, 2),
                "reason": f"音频长 {err:.2f}s（{over*100:.0f}%），拉长镜头而不是硬压语速"}
    return {"action": "trim", "error": err, "rate": 0.0,
            "newShotSeconds": round(max(1.0, a + 0.5), 2),
            "reason": f"音频短 {-err:.2f}s，镜头可收紧"}
