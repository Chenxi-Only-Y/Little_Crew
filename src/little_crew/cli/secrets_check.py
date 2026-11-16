"""密钥与同步边界自检（2026-11-16）。

规则（v0.4.1 起）：

- **真实 API key 只放仓库外的 `D:\\AI\\_secrets\\little-crew.env`**（或 `LC_SECRETS_FILE` 指定）
- `config/.env` 只放**非密钥**配置（端点/路径/模型名）；密钥行保留键名、值留空
- `.env.example` 只放模板；`.gitignore`/`.dockerignore` 挡住 `config/`
- 镜像里没有 `config/`；日志/截图/文档不出现 key 值（doctor 只打印 key 名）

锁住 10 条：不在受控文件、不在 .env.example、不在镜像构建上下文、
密钥文件在仓库外、`config/.env` 无真实值、工具链配置一致等。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


_SECRET_NAME = re.compile(r"(API_KEY|_TOKEN|SECRET|PASSWORD)$", re.I)


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception:                                         # noqa: BLE001
        return ""


def _env_pairs(text: str) -> list[tuple[str, str]]:
    out = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out.append((k.strip(), v.strip().strip('"').strip("'")))
    return out


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 68)
    print("密钥与同步边界 —— 自检")
    print("=" * 68)

    root = Path(__file__).resolve().parents[3]
    from little_crew import config as C

    env_path = Path(C.env_path())
    secrets_path = Path(C.secrets_path())
    gitignore = _read(root / ".gitignore")
    dockerignore = _read(root / ".dockerignore")
    dockerfile = _read(root / "Dockerfile")

    print(); print("A. 仓库边界（密钥不入库）")
    tracked = subprocess.run(["git", "ls-files"], cwd=str(root),
                             capture_output=True, text=True,
                             encoding="utf-8").stdout.splitlines()
    check("A1 ★★★ config/.env 不在受控文件里",
          "config/.env" not in tracked, "")
    check("A2 ★★★ .gitignore 排除 config/.env",
          "config/.env" in gitignore, "")
    ignored = subprocess.run(["git", "check-ignore", "-v", "config/.env"],
                             cwd=str(root), capture_output=True, text=True,
                             encoding="utf-8")
    check("A3 ★★★ git 确认 config/.env 被忽略",
          ignored.returncode == 0, (ignored.stdout or "").strip()[:80])

    print(); print("B. 密钥只在仓库外（可换机手动拷）")
    in_repo = False
    try:
        secrets_path.resolve().relative_to(root.resolve())
        in_repo = True
    except ValueError:
        in_repo = False
    check("B1 ★★★ secrets 文件在仓库外", not in_repo, str(secrets_path))
    check("B2 ★★ 支持 LC_SECRETS_FILE 覆盖路径",
          "LC_SECRETS_FILE" in _read(root / "src/little_crew/config.py"), "")

    print(); print("C. config/.env 里不能有真实密钥值")
    env_pairs = _env_pairs(_read(env_path)) if env_path.is_file() else []
    leaked = [k for k, v in env_pairs if v and _SECRET_NAME.search(k)]
    check("C1 ★★★ config/.env 无「非空密钥值」", not leaked, str(leaked))
    example = _read(root / "config/.env.example")
    # ⚠️ 只拿**密钥名**对应的值比对：URL/模型名在模板里本来就该出现
    real_vals = [v for k, v in _env_pairs(_read(secrets_path))
                 if len(v) >= 16 and _SECRET_NAME.search(k)] \
        if secrets_path.is_file() else []
    ex_leak = [k for k, v in _env_pairs(example) if v in real_vals and v]
    check("C2 ★★★ .env.example 不含真实值", not ex_leak, str(ex_leak))

    print(); print("D. 受控文件不出现真实密钥")
    hits: list[str] = []
    if real_vals:
        for rel in tracked:
            if rel == "config/.env":
                continue
            s = _read(root / rel)
            if not s:
                continue
            for v in real_vals:
                if v in s:
                    hits.append(rel)
                    break
    check("D1 ★★★ 受控文件中没有真实密钥值", not hits, str(hits[:5]))
    check("D2 ★★ 密钥文件存在时能被读取（本机）",
          (not secrets_path.is_file()) or secrets_path.stat().st_size > 0,
          f"{secrets_path.stat().st_size if secrets_path.is_file() else 0} 字节")

    print(); print("E. 镜像构建上下文不带配置")
    check("E1 ★★★ .dockerignore 排除 config/", "config" in dockerignore, "")
    copy_lines = [ln for ln in dockerfile.splitlines()
                  if ln.strip().upper().startswith(("COPY", "ADD"))]
    check("E2 ★★★ Dockerfile 不 COPY 配置/密钥",
          all("config" not in ln and ".env" not in ln for ln in copy_lines),
          str([ln for ln in copy_lines if "config" in ln or ".env" in ln]))

    print(); print("F. 文档口径（换机/同步要说清）")
    doc = _read(root / "docs" / "21-部署与外部服务.md")
    check("F1 ★★★ docs/21 写明密钥只在仓库外",
          "密钥" in doc and ("仓库外" in doc or "_secrets" in doc), "")
    check("F2 ★★ docs/21 写明同步/换机注意事项",
          "同步" in doc or "换机" in doc, "")

    n = sum(1 for _, ok, _ in _r if ok)
    print()
    for name, ok, detail in _r:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 68)
    print(f"结果：{n}/{len(_r)} 通过")
    return 0 if n == len(_r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
