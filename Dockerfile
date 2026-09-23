FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DATA_DIR=/data

WORKDIR /app

# 系统依赖（Chromium 运行所需）
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# ── 依赖层 ────────────────────────────────────────────────────────
# 🔴 顺序很重要：requirements.txt 与 playwright 浏览器都放在 COPY app 之前。
#    Docker 是按层缓存的 —— 只要 requirements.txt 没变，重建镜像时这层
#    直接命中缓存，不再重跑 pip（那几分钟就是耗在这）。
#    所以更新代码用 `docker compose build` 就够了，**不要加 --no-cache**：
#    --no-cache 会连依赖层一起重建，把「改三行 Python」变成「重装全部依赖」。
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 安装 Playwright Chromium（自动注册 / 网页登录需要）
RUN python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# ── 代码层（经常变，放最后）──────────────────────────────────────
COPY app ./app
COPY static ./static

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# 开发时用 docker-compose.dev.yml 挂载源码覆盖这两份，改代码免重建
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
