"""角色音色的**设计层**：LLM 出设定，TTS 物化（2026-11-08）。

## 为什么不能"手工存音色"

上一版我是用 `save_voice.py` 让人给参考音 —— 那是把**创作**变成了**人工配置**。
正确的分工是：

```
导演/角色设计师（LLM）：这个角色是什么声音  ->  结构化设定
TTS（breeze）：按设定造一条试音           ->  音色文件（.breeze）
之后所有台词：用这个音色 + 改语气           ->  音色稳定、语气可变
```

**人只需要做一件事：决定角色性格**（本来就在定妆表里）。

## 设定长什么样（结构化，不是自由文本）

| 字段 | 取值 | 作用 |
|---|---|---|
| `gender` | 女/男 | 决定基频区间 |
| `age` | 少女/青年/中年/年长 | 音色厚度 |
| `texture` | 清亮/沙哑/温柔/冷硬 | 音色质感 |
| `pace` | 慢/中/快 | 语速 |
| `pitch` | 低/中/高 | 音高倾向 |
| `accent` | 标准/方言感 | 咬字 |
| `emotionBase` | 平静/压抑/温和/锋利 | 默认情绪底色 |

**七项就够** —— 再多 TTS 也表达不出来，只是自欺。
"""
from __future__ import annotations

#: 允许取值（不在表里的一律回落到默认，**不猜**）
CHOICES: dict[str, tuple[str, ...]] = {
    "gender": ("女", "男"),
    "age": ("少女", "青年", "中年", "年长"),
    "texture": ("清亮", "沙哑", "温柔", "冷硬"),
    "pace": ("慢", "中", "快"),
    "pitch": ("低", "中", "高"),
    "accent": ("标准", "方言感"),
    "emotionBase": ("平静", "压抑", "温和", "锋利"),
}

DEFAULTS: dict[str, str] = {
    "gender": "女", "age": "青年", "texture": "清亮",
    "pace": "中", "pitch": "中", "accent": "标准", "emotionBase": "平静",
}


def normalize(spec) -> dict:
    """把 LLM 给的东西规整成**合法设定**。非法值回落默认并记录。"""
    raw = spec if isinstance(spec, dict) else {}
    out, fixed = {}, []
    for k, allowed in CHOICES.items():
        v = str(raw.get(k) or "").strip()
        if v in allowed:
            out[k] = v
        else:
            out[k] = DEFAULTS[k]
            if v:
                fixed.append({"field": k, "got": v, "used": DEFAULTS[k]})
    out["_fixed"] = fixed
    return out


def instruction(spec) -> str:
    """设定 -> **一句给 TTS 的音色描述**。

    注意：这句只描述**音色**，不描述情绪 —— 情绪由每句台词单独给，
    否则音色描述里掺了情绪，同一角色不同场景音色又会漂。
    """
    s = normalize(spec)
    pace_word = {"慢": "语速慢", "中": "", "快": "语速偏快"}[s["pace"]]
    pitch_word = {"低": "音高偏低", "中": "", "高": "音高偏高"}[s["pitch"]]
    accent_word = "咬字带一点方言味" if s["accent"] == "方言感" else ""
    bits = [f"{s['age']}{'女性' if s['gender'] == '女' else '男性'}的声音",
            f"音色{s['texture']}"]
    bits += [x for x in (pace_word, pitch_word, accent_word) if x]
    return "，".join(bits)


def from_sheet(sheet, *, personality: str = "") -> dict:
    """**没有 LLM 时的规则兜底**：从定妆表推设定（确定性，不随机）。"""
    sh = sheet if isinstance(sheet, dict) else {}
    gender = "男" if str(sh.get("gender") or "") in ("m", "男", "男性") else "女"
    age_raw = str(sh.get("ageBand") or sh.get("age") or "")
    age = "少女" if any(k in age_raw for k in ("少女", "少年", "儿童", "年轻")) else \
          ("年长" if any(k in age_raw for k in ("年长", "老年", "老")) else \
           ("中年" if "中年" in age_raw else "青年"))
    text = str(personality or sh.get("personality") or "")
    texture = "沙哑" if any(k in text for k in ("老", "病", "哑", "粗")) else (
        "温柔" if any(k in text for k in ("温", "软", "善")) else (
            "冷硬" if any(k in text for k in ("冷", "硬", "厉")) else "清亮"))
    return normalize({"gender": gender, "age": age, "texture": texture})


def prompt_for_llm(characters, sheets=None) -> str:
    """给 LLM 的**音色设计**提示词（结构化输出七项）。"""
    rows = []
    for name in characters or ():
        sh = (sheets or {}).get(name) or {}
        rows.append(f"- {name}：性别={sh.get('gender') or '未定'}，"
                    f"年龄={sh.get('ageBand') or '未定'}，"
                    f"性格={sh.get('personality') or '未定'}")
    keys = "，".join(f"{k}∈{list(v)}" for k, v in CHOICES.items())
    return ("为下列角色设计**音色设定**（不要写台词、不要写剧情）：\n"
            + "\n".join(rows) + "\n\n"
            + f"每个角色输出七项：{keys}\n"
            + '严格 JSON：{"voices": [{"name": "角色名", "gender": "...", ...}]}')


#: TTS 的音色名只接受 `[A-Za-z0-9_-]`（实测报错：
#: "voice names can only use letters, digits, dash and underscore"）。
#: 而我们的角色名是中文 —— 所以必须映射一层。
_NAME_TABLE = {
    "少女": "girl", "少年": "boy", "父亲": "father", "母亲": "mother",
    "旅人": "traveler", "老奶奶": "granny", "老爷爷": "grandpa",
    "阿杏": "axing", "阿薰": "axun", "店主": "shopkeeper",
}


def slug(name: str) -> str:
    """角色名 -> **可写入音色文件的 ASCII 名**。

    优先用人工对照表（可读）；没收录的用 `role_<sha1 前 8 位>`（稳定、无碰撞风险）。
    """
    import hashlib

    s = str(name or "").strip()
    if not s:
        return "role_unknown"
    if s in _NAME_TABLE:
        return _NAME_TABLE[s]
    # 已经是合法名（拼音/英文）就直接用
    if all(c.isascii() and (c.isalnum() or c in "-_") for c in s):
        return s
    return "role_" + hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]
