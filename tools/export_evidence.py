#!/usr/bin/env python3
"""把一集的证据导出成**可独立复核的包**（2026-11-06）。

## 为什么这是差异化

竞品能生成，但**不能证明"这一集是怎么被判定合格的"**。
本工具把一个 episode 的判定记录、产物哈希、配额审计、复验脚本
打成一个自包含目录 —— 拿到包的人**不需要我们的代码**就能复核。

## 包里有什么

```
<out>/
  README.md            说明每个文件是什么、怎么复核
  manifest.json        包内每个文件的 sha256（防夹带）
  verify.py            独立校验脚本（只用标准库，不 import 本项目）
  delivery/*.json      证据：判定 / 缺陷 / 配额 / 逐轮剧本 / 审片
  artifacts/           产物（可选，--with-images）
```

## 两条纪律

1 **导出时就复核一次**：`verify.py` 跑不通的包不导出（`--force` 才跳过）
2 **manifest 覆盖全部文件**：README 也在里面 —— 否则"说明"可以被替换
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERIFY = HERE / "verify_evidence.py"


def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _collect(workdir: Path, with_images: bool) -> tuple[list[Path], list[Path]]:
    """返回 (要复制的文件[相对 workdir], 产物文件)"""
    delivery = workdir / "delivery"
    docs = sorted(p for p in delivery.glob("*.json")) if delivery.is_dir() else []
    arts: list[Path] = []
    if with_images:
        for p in sorted(workdir.rglob("*.png")):
            if p.is_file():
                arts.append(p)
    return docs, arts


def build(workdir: Path, out: Path, *, with_images: bool = False) -> dict:
    docs, arts = _collect(workdir, with_images)
    if not docs:
        raise SystemExit(f"  没有找到证据文件：{workdir}/delivery/*.json")
    if out.exists():
        shutil.rmtree(out)
    (out / "delivery").mkdir(parents=True)
    if arts:
        (out / "artifacts").mkdir()

    copied: list[Path] = []
    for p in docs:
        shutil.copyfile(p, out / "delivery" / p.name)
        copied.append(out / "delivery" / p.name)
    for p in arts:
        dst = out / "artifacts" / p.name
        shutil.copyfile(p, dst)
        copied.append(dst)

    if VERIFY.is_file():
        shutil.copyfile(VERIFY, out / "verify.py")
        copied.append(out / "verify.py")

    # README（也在 manifest 里）
    readme = out / "README.md"
    readme.write_text(_readme(workdir.name, with_images, len(arts)), encoding="utf-8")
    copied.append(readme)

    manifest = {
        "kind": "evidence.bundle",
        "episodeWorkdir": str(workdir),
        "files": {str(p.relative_to(out)).replace("\\", "/"): sha256_of(p)
                  for p in sorted(copied)},
        "count": len(copied),
        "withImages": bool(with_images),
    }
    # ---- provider 指纹摘要（2026-11-08）----
    #
    # 为什么要单独存一份摘要：文件 sha256 只能证明"这个文件没被换"，
    # **证明不了记录本身是否完整/一致**。这里对"规范化后的记录"再算一次哈希，
    # 复核时可以重算 —— 改一条记录，摘要就对不上。
    _fp = out / "delivery" / "provider_fingerprint.json"
    if _fp.is_file():
        try:
            _j = json.loads(_fp.read_text(encoding="utf-8"))
            _recs = _j.get("records") or []
            manifest["fingerprint"] = {
                "file": "delivery/provider_fingerprint.json",
                "total": len(_recs),
                "providers": _j.get("providers") or [],
                "missingServedModel": int(_j.get("missingServedModel") or 0),
                "digest": "sha256:" + hashlib.sha256(
                    json.dumps(_recs, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")).encode("utf-8")).hexdigest(),
            }
        except Exception as _e:                                # noqa: BLE001
            manifest["fingerprint"] = {"error": f"{type(_e).__name__}: {_e}"}
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _readme(name: str, with_images: bool, n_art: int) -> str:
    return f"""# 证据包：{name}

> 这个目录可以**脱离本项目**独立复核 —— 只需要 Python 标准库。

## 怎么验

```bash
python verify.py .
```

它会：
1 按 `manifest.json` 重算每个文件的 sha256 —— **发现被改过的文件**
2 读 `delivery/delivery.evidence.json` 里的产物哈希，重算 `artifacts/` 下对应文件
   （若本包未含产物，则只做第 1 步）

## 里面是什么

| 文件 | 内容 |
|---|---|
| `manifest.json` | 包内每个文件的 sha256 |
| `verify.py` | 独立校验脚本（只用标准库，不 import 本项目） |
| `delivery/delivery.evidence.json` | 产物哈希链 + IR 哈希 + 缓存与门禁统计 |
| `delivery/interface_defects.json` | 接口缺陷清单（越权 / 预算偏差 / 单镜超时） |
| `delivery/scene_quota.json` | 场级配额与**事后遵守审计** |
| `delivery/script.rounds.json` | 剧本每一轮的裁定与缺陷原文 |
| `delivery/scene_check.json` | 场景主体检查的逐镜结果 |
| `delivery/image_review.json` | 渲染产物的可数审片（含"没审什么"） |
| `artifacts/` | 产物图（{"已包含 " + str(n_art) + " 个" if with_images else "本包未包含产物，只含哈希"}） |

## 边界（别把它当质量证明）

- **它证明的是"记录与产物一致、没有被改过"**，不是"画面好看"
- `image_review.json` 里的 `notReviewed` 明确列出**没有审的项目**（画面质量/角色相似度/构图）
- 本包的判定全部来自可数规则 + 人工盲判记录，**没有视觉模型参与**
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="导出一集的可独立复核证据包")
    ap.add_argument("workdir", help="某一集的输出目录（含 delivery/）")
    ap.add_argument("out", help="导出目录")
    ap.add_argument("--with-images", action="store_true", help="把产物图一并打进包")
    ap.add_argument("--force", action="store_true", help="即使自检不过也导出")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")            # type: ignore[attr-defined]
    except Exception:                                       # noqa: BLE001
        pass

    workdir, out = Path(args.workdir), Path(args.out)
    m = build(workdir, out, with_images=args.with_images)
    print(f"  导出 {m['count']} 个文件 -> {out}")

    ok = True
    if (out / "verify.py").is_file():
        r = subprocess.run([sys.executable, str(out / "verify.py"), str(out)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        tail = (r.stdout or "").strip().splitlines()
        print("  导出即复核：" + (tail[-1] if tail else f"exit={r.returncode}"))
        ok = r.returncode == 0
    if not ok and not args.force:
        print("  ❌ 自检未过，包不导出（加 --force 可强制）")
        shutil.rmtree(out, ignore_errors=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
