#!/bin/sh
set -eu
VERSION=${1:?Usage: ./build-image.sh VERSION}
case "$VERSION" in ''|*[!a-zA-Z0-9_.-]*) echo "Invalid image tag" >&2; exit 2;; esac
cd "$(dirname "$0")"

IMAGE=shiningth/tgbot

if docker image inspect "$IMAGE:$VERSION" >/dev/null 2>&1; then
  echo "Tag already exists: $IMAGE:$VERSION; choose a new version" >&2
  exit 2
fi

DOCKER_BUILDKIT=1 docker buildx build --load --progress=plain \
  --label "org.opencontainers.image.version=$VERSION" \
  -t "$IMAGE:$VERSION" \
  -t "$IMAGE:local" \
  -t "$IMAGE:latest" .

docker run --rm --network none --entrypoint python "$IMAGE:$VERSION" -m compileall -q /app/telegram_bot

ID=$(docker image inspect "$IMAGE:$VERSION" --format '{{.Id}}' | cut -c8-19)
echo "Built $IMAGE:$VERSION (+ local/latest, same ID: $ID)"
echo "Portainer stack 239 uses $IMAGE:local -- recreate the tgbot container to apply."
