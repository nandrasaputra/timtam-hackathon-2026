#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "${DIR}/out" "${DIR}/workspace"
"${DIR}/bin/assemble-prompt" > "${DIR}/workspace/AGENTS.md"
if [[ ! -f "${DIR}/out/opencode.json" || "${DIR}/opencode.json.sample" -nt "${DIR}/out/opencode.json" ]]; then
  cp "${DIR}/opencode.json.sample" "${DIR}/out/opencode.json"
fi

test -f "${DIR}/.env"

exec podman run --rm --entrypoint bash \
  -v "${DIR}/bin/mobox-glab:/usr/local/bin/mobox-glab:ro" \
  -v "${DIR}/workspace:/workspace:ro" \
  -v "${DIR}/plugins:/workspace/.opencode/plugins:ro" \
  -v "${DIR}/out/opencode.json:/config/opencode.json:ro" \
  --env-file "${DIR}/.env" \
  -e OPENCODE_CONFIG=/config/opencode.json \
  -p 127.0.0.1:4096:4096 \
  -w /workspace \
  mobox-poc \
  -c 'exec opencode serve --hostname 0.0.0.0 --port 4096'
