# 小剧组 (Little Crew) — 后端镜像
#
# 只打后端。导演台（React + Vite）要容器化需另加 Node 构建阶段，
# 见 docker-compose.yml 的说明。
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
