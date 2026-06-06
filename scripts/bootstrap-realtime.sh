#!/usr/bin/env bash
# Register the realtime channel patterns the dashboard subscribes to.
#
# Why this exists: Insforge realtime subscribes will fail with an opaque
# `{code: "", message: ""}` error if the channel pattern isn't registered
# ahead of time, even though Postgres triggers can publish to those names
# fine. This script idempotently POSTs the patterns we need to
# /api/realtime/channels.
#
# Run after `npx @insforge/cli db migrations up --all`.

set -euo pipefail

# Channel patterns this app expects. Add new entries here if you add features
# that publish to a new channel namespace.
PATTERNS=(
  "intent:%"
)

# Find the repo-root .env. We support running from anywhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "error: .env not found at ${ENV_FILE}" >&2
  exit 1
fi

# Source only the keys we need so we don't leak random vars into the shell.
INSFORGE_PROJECT_URL="$(grep -E '^INSFORGE_PROJECT_URL=' "${ENV_FILE}" | head -1 | cut -d= -f2-)"
INSFORGE_SERVICE_ROLE_KEY="$(grep -E '^INSFORGE_SERVICE_ROLE_KEY=' "${ENV_FILE}" | head -1 | cut -d= -f2-)"

if [[ -z "${INSFORGE_PROJECT_URL}" || -z "${INSFORGE_SERVICE_ROLE_KEY}" ]]; then
  echo "error: INSFORGE_PROJECT_URL or INSFORGE_SERVICE_ROLE_KEY missing from ${ENV_FILE}" >&2
  exit 1
fi

BASE="${INSFORGE_PROJECT_URL%/}"
AUTH="Authorization: Bearer ${INSFORGE_SERVICE_ROLE_KEY}"

echo "→ bootstrapping realtime channels on ${BASE}"

existing="$(curl -fsSL -H "${AUTH}" "${BASE}/api/realtime/channels")"

for pattern in "${PATTERNS[@]}"; do
  if echo "${existing}" | grep -q "\"pattern\":\"${pattern}\""; then
    echo "  ✓ ${pattern} already registered"
    continue
  fi
  body=$(printf '{"pattern":"%s","enabled":true}' "${pattern}")
  if curl -fsSL -X POST -H "${AUTH}" -H "Content-Type: application/json" \
        -d "${body}" "${BASE}/api/realtime/channels" >/dev/null; then
    echo "  + ${pattern} registered"
  else
    echo "  ✗ ${pattern} failed to register" >&2
    exit 1
  fi
done

echo "done."
