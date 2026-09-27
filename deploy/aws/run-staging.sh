#!/usr/bin/env bash
set -euo pipefail

REGION=us-east-1
REGISTRY=958124171224.dkr.ecr.us-east-1.amazonaws.com
export TRACELAB_API_IMAGE="$REGISTRY/tracelab/api@sha256:be3095dc9d75df84a3c95475d29cc167233991f4cf1b2f3e8606b44856558a78"
export TRACELAB_FRONTEND_IMAGE="$REGISTRY/tracelab/frontend@sha256:ccf7759debd897a37826048f839282117da09f234bc1e2d619b09023835b7355"
export TRACELAB_ENV_FILE=/srv/tracelab/deploy/app.env
export TRACELAB_REPOS_DIR=/srv/tracelab/repos
export TRACELAB_UID=1000
export TRACELAB_GID=1000

cd /srv/tracelab
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
trap 'docker logout "$REGISTRY" >/dev/null 2>&1 || true' EXIT
docker pull "$TRACELAB_API_IMAGE" >/dev/null
docker pull "$TRACELAB_FRONTEND_IMAGE" >/dev/null
docker compose -f docker-compose.prod.yml -f docker-compose.staging.yml \
  run --rm --no-deps api alembic upgrade head
docker compose -f docker-compose.prod.yml -f docker-compose.staging.yml up -d
docker compose -f docker-compose.prod.yml -f docker-compose.staging.yml ps
