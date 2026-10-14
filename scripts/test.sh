#!/usr/bin/env bash
# Starts a fresh stack against the mock, loads the workflow and runs the end-to-end tests.
#   scripts/test.sh          # leaves the stack running for inspection
#   scripts/test.sh --down   # removes containers and volumes afterwards
set -euo pipefail
cd "$(dirname "$0")/.."
export COMPOSE_FILE=docker-compose.yml:compose.mock.yml

docker compose --env-file .env.test down -v --remove-orphans >/dev/null 2>&1 || true
docker compose --env-file .env.test up -d --build mock
scripts/setup.sh .env.test

status=0
python3 scripts/e2e_test.py || status=$?
if [[ "${1:-}" == "--down" ]]; then
  docker compose --env-file .env.test down -v >/dev/null 2>&1
fi
exit $status
