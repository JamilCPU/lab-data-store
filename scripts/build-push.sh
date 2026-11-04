#!/usr/bin/env bash
# Build the ingest-api image for the lab's architectures and push it to the lab registry.
# Run on the primary node from the repo root.
set -euo pipefail

REGISTRY="${REGISTRY:-registry.lab:5000}"
IMAGE="${REGISTRY}/lab/ingest-api"
TAG="${TAG:-$(git rev-parse --short HEAD)}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"

if [[ "${ALLOW_DIRTY:-0}" != "1" && -n "$(git status --porcelain)" ]]; then
  echo "working tree is dirty; commit or stash, or set ALLOW_DIRTY=1" >&2
  exit 1
fi

docker buildx build --builder default --platform "$PLATFORMS" \
  -t "$IMAGE:$TAG" -t "$IMAGE:latest" --push ingest-api

echo "TAG=$TAG"
