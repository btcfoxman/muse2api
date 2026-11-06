FROM python:3.11-slim-bookworm

# 基础环境与 Chromium 安装
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    MUSE2API_HOME=/app \
    MUSE2API_HOST=0.0.0.0 \
    MUSE2API_PORT=18610 \
    MUSE2API_CHROMIUM=/usr/bin/chromium \
    MUSE2API_CDP_PORT=19210 \
    MUSE2API_PROFILE_ROOT=/app/data/chrome-profiles

WORKDIR /app

# 安装 Chromium、中文字体与核心依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    ffmpeg \
    fonts-wqy-zenhei \
    fonts-freefont-ttf \
    procps \
    curl \
    openssl \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码与必要目录
COPY admin.html login.html admin_auth.py app.py cdp.py config.py engine.py store.py proxy.py result_storage.py result_delivery.py version.json ./
COPY deploy/ ./deploy/
COPY extension/ ./extension/
COPY tools/ ./tools/

# 创建持久化数据目录
RUN mkdir -p /app/data/media /app/data/downloads /app/data/chrome-profiles

# Commit metadata follows dependency layers so each revision can reuse them.
ARG APP_GIT_SHA=unknown
LABEL org.opencontainers.image.source="https://github.com/btcfoxman/muse2api" \
      org.opencontainers.image.revision="${APP_GIT_SHA}"
ENV APP_GIT_SHA=${APP_GIT_SHA}

VOLUME ["/app/data"]

EXPOSE 18610

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18610/healthz', timeout=3)"

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "18610"]
