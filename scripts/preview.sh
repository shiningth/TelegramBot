#!/bin/sh
# 本地预览容器：同一个镜像底座 + 源码挂载，模块全关，只看界面。
# 用法：scripts/preview.sh start|stop|status|logs
set -eu

NAME=tgbot-dev
IMAGE=shiningth/tgbot:local
REPO=/vol3/1000/SSD/github/TelegramBot/TelegramBot-main
DATA=/vol3/1000/SSD/Docker/telegrambot-dev
PORT=18088

case "${1:-status}" in
  start)
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker run -d --name "$NAME" \
      -p "${PORT}:${PORT}" \
      -v "${REPO}:/app" \
      -v "${DATA}:/data" \
      -v "${DATA}/sessions:/app/sessions" \
      -v "${DATA}/runtime:/app/runtime" \
      -e TZ=Asia/Shanghai \
      -e TGBOT_ADMIN_HOST=0.0.0.0 \
      -e TGBOT_ADMIN_PORT="${PORT}" \
      -e TGBOT_ADMIN_USERNAME=dev \
      -e TGBOT_ADMIN_PASSWORD=devpass \
      --restart=no \
      "$IMAGE" python -m telegram_bot --config /data/config/config.json >/dev/null
    sleep 4
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/login" || true)
    echo "preview started on http://<NAS-IP>:${PORT} (login ${code:-fail})"
    ;;
  stop)
    docker stop "$NAME" >/dev/null 2>&1 || true
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    echo "preview stopped"
    ;;
  logs)
    docker logs --tail 60 "$NAME"
    ;;
  status)
    if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
      echo "preview running: http://<NAS-IP>:${PORT}"
    else
      echo "preview not running (scripts/preview.sh start)"
    fi
    ;;
  *)
    echo "usage: scripts/preview.sh start|stop|status|logs" >&2
    exit 2
    ;;
esac
