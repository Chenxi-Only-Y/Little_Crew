# 小剧组 (Little Crew) — 后端镜像
#
# 只打后端。导演台（React + Vite）见 web/Dockerfile（多阶段 + nginx）。
#
# ⚠️ 本仓库所在环境**无法访问 registry-1.docker.io**（实测超时），
#   所以本机 `docker compose build` 会卡在拉取基础镜像。
#   需要配置镜像加速器/代理后再构建。
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# 安装依赖（langgraph / fastapi / pillow / imageio-ffmpeg 等）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝项目代码（.env、data、.venv 等由 .dockerignore / 卷注入排除）
COPY . .

EXPOSE 8000

# 默认命令：后端 API
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
