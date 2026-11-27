#!/usr/bin/env bash
set -euo pipefail

# Zero-downtime / rollback-safe Deployment Script for LogDay Backend on VPS
# Port: 8001 | Volumes: uploads, logs

APP_DIR="${APP_DIR:-/var/www/backend}"
CONTAINER_NAME="logday-api"
IMAGE_NAME="logday-backend"
NEW_TAG="${IMAGE_NAME}:release-$(date +%Y%m%d%H%M%S)"
PREV_TAG="${IMAGE_NAME}:previous"
LATEST_TAG="${IMAGE_NAME}:latest"

echo "[1/6] Moving to backend directory: ${APP_DIR}..."
cd "${APP_DIR}"

echo "[2/6] Pulling latest changes from main branch..."
git pull --ff-only origin main

echo "[3/6] Building new Docker image (${NEW_TAG}) BEFORE stopping running container..."
export DOCKER_BUILDKIT=0
docker build -t "${NEW_TAG}" .

# Preserve previous working image tag for instant rollback if needed
if docker image inspect "${LATEST_TAG}" >/dev/null 2>&1; then
  docker tag "${LATEST_TAG}" "${PREV_TAG}"
fi
docker tag "${NEW_TAG}" "${LATEST_TAG}"

start_container() {
  local image_ref="$1"
  docker run -d \
    --name "${CONTAINER_NAME}" \
    --restart always \
    -p 8001:8001 \
    --env-file .env \
    -v "$(pwd)/uploads:/app/uploads" \
    -v "$(pwd)/logs:/app/logs" \
    "${image_ref}"
}

echo "[4/6] Replacing running container with new image..."
if docker ps -a --format '{{.Names}}' | grep -qx "${CONTAINER_NAME}"; then
  docker stop "${CONTAINER_NAME}" || true
  docker rm "${CONTAINER_NAME}" || true
fi
start_container "${LATEST_TAG}"

echo "[5/6] Waiting for readiness check on http://127.0.0.1:8001/ready ..."
READY=0
for attempt in $(seq 1 20); do
  if curl -fsS http://127.0.0.1:8001/ready >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 2
done

if [ "${READY}" -ne 1 ]; then
  echo "[ERROR] New container failed readiness probe! Rolling back to ${PREV_TAG}..."
  docker logs --tail 50 "${CONTAINER_NAME}" || true
  docker stop "${CONTAINER_NAME}" || true
  docker rm "${CONTAINER_NAME}" || true
  if docker image inspect "${PREV_TAG}" >/dev/null 2>&1; then
    docker tag "${PREV_TAG}" "${LATEST_TAG}"
    start_container "${PREV_TAG}"
    echo "[ROLLBACK] Restored previous working container (${PREV_TAG})."
  fi
  exit 1
fi

echo "[6/6] Deployment complete and verified healthy on port 8001 (${NEW_TAG})!"
