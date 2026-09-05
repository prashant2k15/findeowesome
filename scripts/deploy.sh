#!/usr/bin/env bash
# First-time VPS setup + redeploy. Run as a sudo-capable user on Ubuntu 22.04/24.04.
set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/bing-link-finder}"

if ! command -v docker >/dev/null 2>&1; then
  echo "==> installing docker"
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "$USER"
  echo "!! log out and back in so the docker group applies, then re-run"
  exit 0
fi

cd "$REPO_DIR"

if [ ! -f .env ]; then
  echo "==> creating .env from template - edit it before the first real run"
  cp .env.example .env
fi

echo "==> pulling latest code"
git pull --ff-only

echo "==> rebuilding stack"
docker compose pull --ignore-buildable
docker compose up -d --build

echo "==> waiting for the API"
for _ in $(seq 1 30); do
  if curl -fsS localhost:"${API_PORT:-8000}"/healthz >/dev/null 2>&1; then
    echo "==> healthy"
    break
  fi
  sleep 2
done

docker compose ps

echo
echo "==> preflight: verifying every dependency that can fail silently"
docker compose exec -T worker python -m app.cli doctor || {
  echo "!! some checks failed - discovery may not work until they are fixed"
  exit 1
}
