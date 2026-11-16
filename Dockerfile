# 平台 API 容器（2026-11-12，2026-11-16 明确外部服务边界）
#
# 说明（重要）：**这个镜像只包含平台本体**，不含任何生成/语音服务与模型权重。
#
# 外置服务（**不计入平台交付物**，按环境变量指过去）：
#   local-gen（本地出图）   LITTLE_CREW_IMAGE_ENDPOINT=http://host.docker.internal:8100
#   ComfyUI（可选）        COMFYUI_ENDPOINT=http://host.docker.internal:8188
#   Breeze TTS（可选）     BREEZE_TTS_ENDPOINT=http://host.docker.internal:8110
#   （视频模型原生音频时不需要 TTS；云端渲染只需 DASHSCOPE/SUDORELAY key）
#
# 为什么这样切：模型与服务动辄几十 GB，塞进镜像既不可维护，也和
# "本地/云端可切换"的通道设计冲突 —— 平台本体必须能独立部署与升级。
# 这样做是有意的：模型动辄几十 GB，塞进镜像既不可维护，也和"本地/云端可切换"的
# 通道设计冲突 —— 平台本体应当能独立部署与升级。
#
# 构建：  docker build -t little-crew:dev .
# 运行：  docker run --rm -p 8787:8787 \
#           -e COMFYUI_ENDPOINT=http://host.docker.internal:8188 \
#           -e BREEZE_TTS_ENDPOINT=http://host.docker.internal:8110 \
#           -e DEEPSEEK_API_KEY=sk-... \
#           -v %cd%/outputs:/app/outputs \
#           little-crew:dev
#
# 注意：Windows 宿主上用 host.docker.internal 访问本机服务。

# 构建参数（国内网络可换镜像源；默认官方源）：
#   docker build --build-arg APK_MIRROR=mirrors.aliyun.com \
#                --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple .
# 基础镜像用 Alpine：平台是纯 Python（零第三方运行依赖），Alpine 体积更小、apk 源更稳。
ARG BASE_IMAGE=python:3.12-alpine
FROM ${BASE_IMAGE}

LABEL org.opencontainers.image.title="Little Crew" \
      org.opencontainers.image.description="AIGC 漫剧生成平台（API 与 CLI）" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    LITTLE_CREW_RUNS_DIR=/app/outputs/.api-runs

WORKDIR /app

# 只装运行期依赖（零第三方依赖的核心层不需要额外包）
ARG PIP_INDEX_URL=""
COPY pyproject.toml README.md LICENSE /app/
COPY src/ /app/src/
RUN if [ -n "$PIP_INDEX_URL" ]; then \
        pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -e . ; \
    else \
        pip install --no-cache-dir -e . ; \
    fi && python -c "import little_crew; print('little_crew import OK')"

# 可选：把 ffmpeg 带上（成片合成需要）。
# `APK_MIRROR` 用于国内网络（默认官方源）：只替换源域名，不改任何依赖版本。
ARG APK_MIRROR=""
RUN if [ -n "$APK_MIRROR" ]; then \
        sed -i "s|dl-cdn.alpinelinux.org|$APK_MIRROR|g" /etc/apk/repositories ; \
    fi && \
    apk add --no-cache ffmpeg

COPY tools/ /app/tools/
RUN mkdir -p /app/outputs

# 健康检查（平台本体不依赖外置服务，容器能起就算健康）
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3).status==200 else 1)"

EXPOSE 8787

# 默认只跑 API；跑整集用 `docker run ... little-crew episode --help`
CMD ["python", "-m", "little_crew.cli", "serve", "--host", "0.0.0.0", "--port", "8787"]
