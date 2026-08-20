#!/usr/bin/env sh
# Run Docker Compose with either the v2 plugin or legacy standalone binary.
set -eu

if docker compose version >/dev/null 2>&1; then
    exec docker compose "$@"
fi

if command -v docker-compose >/dev/null 2>&1; then
    exec docker-compose "$@"
fi

echo "Docker Compose is required. Install the Docker Compose plugin (preferred) or docker-compose." >&2
exit 127
