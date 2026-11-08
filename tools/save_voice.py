#!/usr/bin/env python3
"""把一段参考音存成**可复用的音色**（`composer`/配音那边的 `--voice`）。

## 为什么需要它

本机 breeze 只有**一个**预存音色（`gentle_sister`），所以现在所有角色同声。
要"音色稳定 + 角色各异"，就得为每个角色存一个音色 —— 每个只要一段参考音。

## 用法

```bash
# 1) 存音色（给 3~10 秒干净人声，最好单声道、无背景音）
python tools/save_voice.py --ref-audio 父亲.wav --name 父亲 --ref-text "参考音里说的那句话"

# 2) 看一眼现在有哪些
python tools/save_voice.py --list

# 3) 之后跑 `--voice` 时，角色名与音色名相同的会**自动绑定**（见 voice_cast）
```

## 纪律

- **参考音必须是你有权使用的**（自己的录音，或明确授权的样本）
- 存好后 `voices/<名字>.breeze` 就是**确定性音色锚点**，
  同一角色跨集都用它 —— 这是我们能做到的"跨集音色一致"
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from little_crew.runtime import tts_breeze as T  # noqa: E402


def save(ref_audio: str, name: str, ref_text: str = "") -> dict:
    """调用 breeze-cli 存音色。返回 `{ok, name, path, error}`。"""
    exe, model = T.exe_path(), T.model_path()
    if not exe.is_file() or not model.is_file():
        return {"ok": False, "name": name, "path": "",
                "error": "找不到 breeze-cli 或模型（见 BREEZE_TTS_ROOT）"}
    ref = Path(str(ref_audio))
    if not ref.is_file():
        return {"ok": False, "name": name, "path": "",
                "error": f"参考音不存在：{ref}"}
    T.voices_dir().mkdir(parents=True, exist_ok=True)
    cmd = [str(exe), str(model), "--text", ref_text or "占位",
           "--ref-audio", str(ref), "--save-voice", str(name),
           "--voices-dir", str(T.voices_dir())]
    if ref_text:
        cmd += ["--ref-text", str(ref_text)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=600)
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "name": name, "path": "",
                "error": f"{type(e).__name__}: {e}"}
    out = T.voices_dir() / f"{name}.breeze"
    if not out.is_file():
        tail = [x for x in (p.stdout or "").splitlines() if x.strip()]
        return {"ok": False, "name": name, "path": "",
                "error": (tail[-1] if tail else f"exit {p.returncode}")[:200]}
    return {"ok": True, "name": name, "path": str(out), "error": "",
            "bytes": out.stat().st_size}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把一个参考音存成可复用音色")
    ap.add_argument("--ref-audio", default="", help="参考音 wav（3~10 秒干净人声）")
    ap.add_argument("--name", default="", help="音色名（建议与角色名一致）")
    ap.add_argument("--ref-text", default="", help="参考音里说的那句话（越准越好）")
    ap.add_argument("--list", action="store_true", help="列出已有音色")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")            # type: ignore[attr-defined]
    except Exception:                                       # noqa: BLE001
        pass

    if args.list:
        vs = T.list_voices()
        print(f"  已存音色 {len(vs)} 个：{vs or '（空）'}")
        print(f"  目录：{T.voices_dir()}")
        return 0
    if not args.ref_audio or not args.name:
        print("  需要 --ref-audio 与 --name（或 --list 看现状）")
        return 2
    r = save(args.ref_audio, args.name, args.ref_text)
    if r["ok"]:
        print(f"  ✅ 已存音色「{r['name']}」 {r['bytes']} 字节 -> {r['path']}")
        print("     之后角色名与它相同的会自动绑定（voice_cast）")
        return 0
    print(f"  ❌ 存失败：{r['error']}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
