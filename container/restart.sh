#!/usr/bin/env bash
# Rebuild the image, refresh mounted config, restart the container and the Lark bridge.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="${DIR}/out/lark_bridge.log"
bridge_pid=""

cleanup() {
  if [[ -n "${bridge_pid}" ]] && kill -0 "${bridge_pid}" 2>/dev/null; then
    kill "${bridge_pid}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

test -x "${DIR}/.venv/bin/python"
test -f "${DIR}/.env"

podman build -t mobox-poc -f "${DIR}/Containerfile" "${DIR}"

mkdir -p "${DIR}/out"
if [[ ! -f "${DIR}/out/opencode.json" || "${DIR}/opencode.json.sample" -nt "${DIR}/out/opencode.json" ]]; then
  cp "${DIR}/opencode.json.sample" "${DIR}/out/opencode.json"
fi

ids="$(podman ps --format '{{.ID}} {{.Image}} {{.Ports}}' | awk '/mobox-poc/ || /:4096->/ { print $1 }')"
if [[ -n "${ids}" ]]; then
  podman stop ${ids}
fi

pids="$(pgrep -f '[l]ark_bridge.py' || true)"
if [[ -n "${pids}" ]]; then
  kill ${pids} 2>/dev/null || true
  sleep 1
  pids="$(pgrep -f '[l]ark_bridge.py' || true)"
  if [[ -n "${pids}" ]]; then
    kill -9 ${pids} 2>/dev/null || true
  fi
fi

: > "${LOG}"
"${DIR}/.venv/bin/python" "${DIR}/lark_bridge.py" "${DIR}/.env" >"${LOG}" 2>&1 &
bridge_pid=$!

ready=0
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40; do
  if grep -q "lark bridge listening" "${LOG}"; then
    ready=1
    break
  fi
  if ! kill -0 "${bridge_pid}" 2>/dev/null; then
    echo "lark bridge exited before listening" >&2
    cat "${LOG}" >&2
    exit 1
  fi
  sleep 0.5
done
if [[ "${ready}" -ne 1 ]]; then
  echo "lark bridge did not print listening" >&2
  cat "${LOG}" >&2
  exit 1
fi

echo "lark bridge listening"
echo "bridge log: ${LOG}"
"${DIR}/up.sh"
