#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "${DIR}/out" "${DIR}/workspace"
"${DIR}/bin/assemble-prompt" > "${DIR}/workspace/AGENTS.md"

test -f "${DIR}/.env"

exec podman run --rm \
  -v "${DIR}/bin/mobox-glab:/usr/local/bin/mobox-glab:ro" \
  -v "${DIR}/cursor_agent.py:/opt/mobox/cursor_agent.py:ro" \
  -v "${DIR}/cursor_agent_server.py:/opt/mobox/cursor_agent_server.py:ro" \
  -v "${DIR}/workspace:/workspace:ro" \
  --env-file "${DIR}/.env" \
  -e CURSOR_AGENT_CWD=/workspace \
  -p 127.0.0.1:4096:4096 \
  -w /workspace \
  mobox-poc
