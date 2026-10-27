#!/usr/bin/env bash
set -Eeuo pipefail

REMOTE="${1:-matrix.remote}"
REMOTE_DIR="${2:-Services/telegram-downloader}"
RETRY_SECONDS="${RETRY_SECONDS:-15}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cd "$PROJECT_DIR"

echo "Checking ${REMOTE}:~/${REMOTE_DIR} ..."
until ssh "$REMOTE" "mkdir -p \"\$HOME/${REMOTE_DIR}\""; do
  echo "SSH check failed; retrying in ${RETRY_SECONDS}s..."
  sleep "$RETRY_SECONDS"
done

echo "Stopping the local Telegram downloader..."
docker compose down

attempt=1
while true; do
  echo "Rsync attempt ${attempt}..."
  if rsync -aP --partial --no-owner --no-group \
    --exclude '.venv/' \
    --exclude '.pytest_cache/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude '*.egg-info/' \
    --exclude '.DS_Store' \
    "$PROJECT_DIR/" "$REMOTE:${REMOTE_DIR}/"; then
    break
  fi

  echo "Rsync failed; retrying in ${RETRY_SECONDS}s..."
  attempt=$((attempt + 1))
  sleep "$RETRY_SECONDS"
done

echo "Migration completed: ${REMOTE}:~/${REMOTE_DIR}"
echo "Next on the remote host:"
echo "  cd ~/${REMOTE_DIR}"
echo "  sudo chown -R 10001:10001 data downloads"
echo "  docker compose build && docker compose up -d"
