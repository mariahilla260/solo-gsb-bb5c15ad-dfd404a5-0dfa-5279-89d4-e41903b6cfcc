FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 仅安装依赖，利用构建缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# shell 形式以便使用 DATABASE_URL 等环境变量；等待由 compose 的 depends_on 健康检查保证
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
