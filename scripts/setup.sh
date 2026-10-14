#!/usr/bin/env bash
# Loads credentials from an env file into n8n, imports the workflow and publishes it.
#   scripts/setup.sh            # uses .env
#   scripts/setup.sh .env.test  # used by the mock tests
# Set COMPOSE_FILE=docker-compose.yml:compose.mock.yml to run against the mock.
set -euo pipefail
cd "$(dirname "$0")/.."
ENV_FILE=${1:-.env}
[[ -f "$ENV_FILE" ]] || { echo "missing $ENV_FILE (copy .env.example)" >&2; exit 1; }
compose() { docker compose --env-file "$ENV_FILE" "$@"; }

set -a; source "$ENV_FILE"; set +a
# Common names work too.
DECISION_API_KEY=${DECISION_API_KEY:-${OPENROUTER_API_KEY:-}}
LLM_API_KEY=${LLM_API_KEY:-${GEMINI_API_KEY:-${GOOGLE_API_KEY:-}}}
export DECISION_API_KEY LLM_API_KEY
for v in GITLAB_WEBHOOK_SECRET GITLAB_TOKEN DECISION_API_KEY LLM_API_KEY POSTGRES_PASSWORD; do
  [[ -n "${!v:-}" ]] || { echo "set $v in $ENV_FILE" >&2; exit 1; }
done

# n8n encrypts the plain values below with N8N_ENCRYPTION_KEY on import.
credentials_json() {
  python3 - <<'PY'
import json, os
e = os.environ
def header(id_, name, header, value):
    return {"id": id_, "name": name, "type": "httpHeaderAuth", "data": {"name": header, "value": value}}
print(json.dumps([
    header("glHookSecret0001", "GitLab webhook secret", "X-Gitlab-Token", e["GITLAB_WEBHOOK_SECRET"]),
    header("glApiToken000001", "GitLab API token", "PRIVATE-TOKEN", e["GITLAB_TOKEN"]),
    header("decisionKey00001", "Decision model API key", "Authorization", "Bearer " + e["DECISION_API_KEY"]),
    header("llmApiKey0000001", "LLM API key (OpenAI-compatible)", "Authorization", "Bearer " + e["LLM_API_KEY"]),
    {"id": "triageDb00000001", "name": "Triage DB", "type": "postgres", "data": {
        "host": "postgres", "port": 5432, "database": "n8n", "user": "n8n",
        "password": e["POSTGRES_PASSWORD"], "ssl": "disable"}},
]))
PY
}

compose up -d --wait postgres
# Schema is idempotent, so this is safe on every run.
compose exec -T -e PGOPTIONS="-c client_min_messages=warning" postgres psql -q -v ON_ERROR_STOP=1 -U n8n -d n8n < db/init.sql
credentials_json | compose run --rm -T --no-deps --entrypoint sh n8n -c \
  'umask 077; cat > /tmp/creds.json && n8n import:credentials --input=/tmp/creds.json; rc=$?; rm -f /tmp/creds.json; exit $rc'
compose run --rm -T --no-deps --entrypoint sh n8n -c \
  'cat > /tmp/workflow.json && n8n import:workflow --input=/tmp/workflow.json && n8n publish:workflow --id=GitlabTriage0001' \
  < workflows/issue-triage.json
compose up -d --force-recreate n8n

# n8n reports ready before it registers the workflow's webhook, so wait for the
# webhook itself: 404 means not registered yet, 403 (no token) means it is live.
echo -n "waiting for the webhook"
for _ in $(seq 1 90); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:5678/webhook/gitlab/issues || true)
  if [[ "$code" != "000" && "$code" != "404" && "$code" != "503" ]]; then echo " live ($code)"; exit 0; fi
  echo -n "."; sleep 2
done
echo " webhook did not come up" >&2
compose logs --tail 50 n8n >&2
exit 1
