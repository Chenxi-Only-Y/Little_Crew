"""把**音色设定**物化成可复用音色（LLM 设计 -> TTS 试音 -> 存成 .breeze）。

## 一次成型

```
设定（七项）-> instruction（一句描述）
   -> 用这条描述合成一句**固定试音台词**
   -> 立刻 `--save-voice <角色>` 存成音色
   -> 之后所有台词用 `--voice <角色>` + 各自语气
```

**试音台词故意固定**：换台词会让音色带上下文差异，失去"锚点"的意义。

## 两条纪律

1 **失败不静默**：任何一步失败都返回 `ok=False` 与原因，**不留半成品**
2 **可复现**：试音用固定 seed，同设定两次应得到同一音色文件（字节可能因
   TTS 内部实现不同，但**设定与 seed 固定**是我们能控制的全部）
"""
from __future__ import annotations

from pathlib import Path

#: 固定试音台词（中性、无情绪，避免把语气烤进音色）
AUDITION = "今天天气不错，我出门走了一圈，回来的时候天已经黑了。"


def forge(name: str, spec, *, synth, voices_dir=None, seed: int = 42,
          save=None, workdir=None) -> dict:
    """把一个角色音色造出来。

    `synth(text, path, voice=..., instruction=..., seed=...)` —— 复用现有后端
    `save(ref_audio, name, ref_text)` —— 存音色（默认走 `tools/save_voice.py`）
    返回 `{ok, name, voiceFile, instruction, seconds, error}`
    """
    from little_crew.governance.voice_design import instruction as _ins
    from little_crew.governance.voice_design import normalize, slug

    who = str(name or "").strip()
    if not who:
        return _fail("", "角色名为空")
    ins = _ins(spec)
    if not ins:
        return _fail(who, "音色设定无法生成描述")

    wd = Path(str(workdir or voices_dir or "outputs/_voices"))
    wd.mkdir(parents=True, exist_ok=True)
    audition = wd / f"audition_{who}.wav"

    r = synth(AUDITION, audition, instruction=ins, seed=int(seed))
    if not r.get("ok"):
        return _fail(who, f"试音合成失败：{r.get('error') or 'unknown'}")
    if not audition.is_file():
        return _fail(who, "试音文件没落盘")

    if save is None:
        import importlib.util

        # parents[0]=runtime, [1]=little_crew, [2]=src, [3]=项目根
        root = Path(__file__).resolve().parents[3]
        spec_path = root / "tools" / "save_voice.py"
        mod = importlib.util.spec_from_file_location("save_voice", spec_path)
        save_mod = importlib.util.module_from_spec(mod)
        mod.loader.exec_module(save_mod)             # type: ignore[union-attr]
        save = save_mod.save

    # 音色名必须是 ASCII（TTS 限制），中文角色名走 slug 映射
    sv = save(str(audition), slug(who), AUDITION)
    if not sv.get("ok"):
        return _fail(who, f"存音色失败：{sv.get('error') or 'unknown'}")
    return {"ok": True, "name": who, "voiceName": slug(who),
            "voiceFile": sv.get("path", ""),
            "instruction": ins, "seconds": float(r.get("seconds") or 0),
            "seed": int(seed), "error": ""}


def forge_all(specs: dict, *, synth, voices_dir=None, seed: int = 42,
              save=None, workdir=None) -> dict:
    """批量造音色（一个失败不影响其它）。"""
    ok, failed = [], []
    for who, spec in (specs or {}).items():
        r = forge(who, spec, synth=synth, voices_dir=voices_dir, seed=seed,
                  save=save, workdir=workdir)
        (ok if r["ok"] else failed).append(r)
    return {"kind": "voice.forge", "forged": ok, "failed": failed,
            "count": len(ok), "requested": len(specs or {})}


def _fail(who: str, why: str) -> dict:
    return {"ok": False, "name": who, "voiceFile": "", "instruction": "",
            "seconds": 0.0, "seed": 0, "error": str(why)[:200]}
