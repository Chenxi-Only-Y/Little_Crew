"""部署边界自检（2026-11-16）：**镜像只装平台本体，外置服务不计入交付物**。

锁住五件事：
1 Dockerfile 只装平台（Python/FFmpeg/源码），**不含** local-gen / ComfyUI / TTS / 模型权重
2 `.dockerignore` 把配置与密钥挡在构建上下文外
3 Makefile 有 build / deploy-check / doctor / test / report，episode 走编制参数
4 文档 `docs/21` 写明边界；docs/README 索引里有它
5 doctor：外置服务（local-gen/ComfyUI/TTS）默认**不阻塞**；按需开关 / 云端路径才阻塞
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 68)
    print("部署边界（Docker / doctor / Makefile / 文档）—— 自检")
    print("=" * 68)

    root = Path(__file__).resolve().parents[3]
    docker = (root / "Dockerfile").read_text(encoding="utf-8")
    ignore = (root / ".dockerignore").read_text(encoding="utf-8")
    make = (root / "Makefile").read_text(encoding="utf-8")
    doc = root / "docs" / "21-部署与外部服务.md"
    docs_readme = (root / "docs" / "README.md").read_text(encoding="utf-8")

    print(); print("A. Dockerfile：只装平台本体")
    check("A1 ★★★ 基础镜像与入口是平台 API",
          "FROM python:" in docker and "little_crew.cli" in docker
          and "serve" in docker and "EXPOSE 8787" in docker, "")
    check("A2 ★★★ 装 FFmpeg（成片合成的平台依赖）",
          "ffmpeg" in docker, "")
    _copied = [ln for ln in docker.splitlines()
               if ln.strip().upper().startswith(("COPY", "ADD", "RUN"))]
    check("A3 ★★★ 镜像里**没有**外置服务（不 COPY/不安装 local-gen/ComfyUI/TTS）",
          all(k not in " ".join(_copied)
              for k in ("local-ai", "breeze-server", "ComfyUI", "local-gen")), "")
    check("A4 ★★★ 镜像里**没有**模型权重",
          all(k not in docker for k in ("safetensors", ".gguf", "model.safetensors")), "")
    check("A5 ★★★ 健康检查打平台自己的 /healthz（不依赖外置服务）",
          "HEALTHCHECK" in docker and "/healthz" in docker, "")
    check("A6 ★★ 注释里写明外置服务边界（不计入平台交付物）",
          "外置服务" in docker and "不计入平台交付物" in docker, "")

    print(); print("B. .dockerignore：密钥/数据不进构建上下文")
    check("B1 ★★★ 排除 config 与 .env",
          "\nconfig\n" in ignore and ".env" in ignore, "")
    check("B2 ★★★ 排除 outputs / research / tests",
          "outputs" in ignore and "research" in ignore and "tests" in ignore, "")

    print(); print("C. Makefile：部署入口")
    for target in ("doctor", "deploy-check", "build", "test", "report"):
        check(f"C {target}", f"\n{target}:" in make, "")
    check("C7 ★★ episode 走编制参数（--crew-profile）",
          "--crew-profile" in make, "")
    check("C8 ★★ help 里标明外置服务不阻塞",
          "外置服务" in make, "")

    print(); print("D. 文档边界")
    check("D1 ★★★ docs/21 存在且写明边界",
          doc.is_file() and "不计入平台交付物" in doc.read_text(encoding="utf-8"), "")
    check("D2 ★★★ docs/README 索引包含 docs/21",
          "21-部署与外部服务" in docs_readme, "")
    check("D3 ★★ 文档列出 local-gen 为外置服务",
          "local-gen" in doc.read_text(encoding="utf-8"), "")

    print(); print("E. doctor：外置服务默认不阻塞")
    old = {k: os.environ.get(k) for k in (
        "COMFYUI_ENDPOINT", "BREEZE_TTS_ENDPOINT", "LITTLE_CREW_IMAGE_ENDPOINT",
        "LITTLE_CREW_NEED_VOICE", "LITTLE_CREW_NEED_LLM",
        "LITTLE_CREW_VIDEO_MODEL", "LITTLE_CREW_RENDER_PATH")}
    try:
        os.environ["COMFYUI_ENDPOINT"] = "http://127.0.0.1:59999"
        os.environ["BREEZE_TTS_ENDPOINT"] = "http://127.0.0.1:59998"
        os.environ["LITTLE_CREW_IMAGE_ENDPOINT"] = "http://127.0.0.1:59997"
        os.environ.pop("LITTLE_CREW_NEED_VOICE", None)
        os.environ.pop("LITTLE_CREW_NEED_LLM", None)
        os.environ["LITTLE_CREW_VIDEO_MODEL"] = "wan22-5b-ti2v"
        os.environ.pop("LITTLE_CREW_RENDER_PATH", None)
        from little_crew.cli.doctor import run_checks

        checks = run_checks()
        ext = [c for c in checks if c.get("external")]
        # 平台本体必须都在
        names = [c["name"] for c in checks]
        check("E1 ★★★ doctor 有平台本体检查项",
              {"平台导入", "名册契约", "授权目录", "输出目录", "FFmpeg"} <= set(names),
              str(names))
        check("E2 ★★★ local-gen / ComfyUI / TTS 标记为外置",
              {"local-gen", "ComfyUI", "TTS"} <= {c["name"] for c in ext},
              str([c["name"] for c in ext]))
        check("E3 ★★★ 外置服务不通 -> 不阻塞（默认）",
              all(not c["blocking"] for c in ext),
              str([(c["name"], c["blocking"]) for c in ext]))
        check("E4 ★★ 平台本体检查全部通过（本机）",
              all(c["ok"] for c in checks if not c.get("external")
                  and c["name"] in {"平台导入", "名册契约", "授权目录", "输出目录", "FFmpeg"}),
              str([(c["name"], c["ok"]) for c in checks
                   if c["name"] in {"平台导入", "名册契约", "输出目录", "FFmpeg"}]))

        # 按需：显式要求配音 + 非原生音频视频模型 -> TTS 阻塞
        os.environ["LITTLE_CREW_NEED_VOICE"] = "1"
        checks2 = run_checks()
        tts = next(c for c in checks2 if c["name"] == "TTS")
        check("E5 ★★★ 需要外部配音且无原生音频 -> TTS 变成阻塞项",
              tts["blocking"] is True, str(tts["blocking"]))
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

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
