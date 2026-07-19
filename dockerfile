# APP_VERSION = "1.0.0"
# https://PeiFeng.li

# 第一阶段：构建阶段
FROM docker.m.daocloud.io/library/python:3.11-slim AS builder

# 安装构建依赖（完成后会清理）
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 复制依赖文件
COPY requirements.txt .

# 安装依赖到虚拟环境
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir -r requirements.txt

# 第二阶段：运行阶段。与构建阶段保持同一 Debian 发行版，避免 glibc/musl C 扩展不兼容。
FROM docker.m.daocloud.io/library/python:3.11-slim

ARG APP_VERSION=1.0.0
LABEL org.opencontainers.image.title="Emby Notifier Bot WeCom" \
      org.opencontainers.image.version="${APP_VERSION}"

# 从构建阶段复制虚拟环境
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 复制应用代码
COPY . /app
WORKDIR /app

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \
    # 代理配置（按需传递）
    HTTP_PROXY= \
    HTTPS_PROXY= \
    # 核心配置（部署时通过-e传递实际值）
    TELEGRAM_BOT_TOKEN= \
    TELEGRAM_CHAT_ID= \
    WECOM_CORP_ID= \
    WECOM_AGENT_ID= \
    WECOM_CORP_SECRET= \
    WECOM_TO_USER=@all \
    WECOM_PROXY= \
    WECOM_API_URL= \
    WECOM_TOKEN= \
    WECOM_ENCODING_AES_KEY= \
    EMBY_SERVER_URL= \
    EMBY_API_KEY= \
    TZ=Europe/Rome \
    # 固定配置
    WEBHOOK_HOST=0.0.0.0 \
    WEBHOOK_PORT=53211 \
    EMBY_MONITOR_EVENTS=playback.start,playback.stop,playback.pause,user.authenticated,user.logout,library.new,library.deleted,metadata.update \
    ENABLE_COVERS=True \
    OVERVIEW_MAX_LENGTH=300

# 暴露Webhook端口（与WEBHOOK_PORT一致）
EXPOSE 53211

# 启动命令
CMD ["python", "Emby_Notifier_Bot.py"]
