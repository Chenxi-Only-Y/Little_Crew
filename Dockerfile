# 小剧组 (Little Crew) — 后端/前端镜像
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# 安装依赖（含 langgraph / fastapi / streamlit 等）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝项目代码（.env、data、.venv 等由 .dockerignore / 卷注入排除）
COPY . .

EXPOSE 8000

# 默认命令：后端 API。前端服务在 compose 里用 command 覆盖为 streamlit。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
