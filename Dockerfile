# ============================================================
# 构建阶段：在 python 官方镜像里装好全部依赖
# ============================================================
FROM python:3.11-slim-bookworm@sha256:90d117a7f1ef6535dc75831e1a9184725757fede0d4d424d7977fb5429d50610 AS build

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
# 默认走国内镜像源：deb.debian.org 在这台 NAS 上只有约 300 KB/s 且频繁断流，
# 装 ffmpeg/中文字体时构建会随机失败。海外机器可用 --build-arg 换回官方源。
ARG DEBIAN_MIRROR=https://mirrors.tuna.tsinghua.edu.cn
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN set -eux; \
    printf 'deb %s/debian bookworm main\ndeb %s/debian bookworm-updates main\ndeb %s/debian-security bookworm-security main\n' \
        "$DEBIAN_MIRROR" "$DEBIAN_MIRROR" "$DEBIAN_MIRROR" > /etc/apt/sources.list; \
    rm -f /etc/apt/sources.list.d/*; \
    apt-get update; \
    apt-get install -y --no-install-recommends -o Acquire::Retries=5 \
        ffmpeg tesseract-ocr tesseract-ocr-chi-sim fonts-noto-cjk; \
    rm -rf /var/lib/apt/lists/*
COPY requirements.lock ./
# 只用一个索引：同时挂 pypi.org 作为 extra-index 时，pypi.org 的响应会被中间链路截断，
# pip 解析 JSON 直接崩溃（JSONDecodeError: Unterminated string）。
RUN PIP_INDEX_URL="$PIP_INDEX_URL" pip install --no-cache-dir --retries 5 -r requirements.lock
COPY telegram_bot ./telegram_bot
COPY scripts ./scripts
COPY README.md ./README.md
COPY docs/telegram-api.md docs/deployment.md ./docs/
RUN mkdir -p /data/config /app/sessions /app/runtime

# ============================================================
# 运行阶段：从空镜像开始，把构建阶段的文件系统整个复制进来
# 结果是一个自包含的单层镜像，不依赖任何外部 base
# ============================================================
FROM scratch
COPY --from=build / /

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

WORKDIR /app
EXPOSE 8088
ENTRYPOINT ["python", "-m", "telegram_bot.container_entrypoint"]
CMD ["python", "-m", "telegram_bot", "--config", "/data/config/config.json"]
LABEL org.opencontainers.image.title="TelegramBot"
