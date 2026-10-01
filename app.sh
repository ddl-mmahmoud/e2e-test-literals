#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# Runs both services in one Domino App: the API (service/app.py) bound to a Unix
# domain socket nothing external ever talks to, and the UI (service_ui/app.py) on the
# port Domino actually exposes. The UI reverse-proxies everything it doesn't have its
# own route for straight through to the API over that socket (see service_ui/proxy.py)
# -- including each revision's Datasette pages -- so only one port needs to be exposed
# at all, and the Domino app hosting this (which can be strict about ports a hosted
# app opens) never sees the API process open one in the first place.
API_SOCKET_PATH="${E2E_TEST_LITERALS_SERVICE_API_SOCKET_PATH:-/tmp/e2e-test-literals-service-sockets/api.sock}"
UI_PORT=8888

mkdir -p "$(dirname "$API_SOCKET_PATH")"
rm -f "$API_SOCKET_PATH"

# The API process must not apply DOMINO_RUN_HOST_PATH itself: it's never reached
# directly, only via the UI's internal, unprefixed calls (client.py / proxy.py).
env -u DOMINO_RUN_HOST_PATH \
    E2E_TEST_LITERALS_SERVICE_API_SOCKET_PATH="$API_SOCKET_PATH" \
    uv run uvicorn src.e2e_test_literals.service.app:app --uds "$API_SOCKET_PATH" &
API_PID=$!
trap 'kill "$API_PID" 2>/dev/null || true' EXIT

# uvicorn's --uds bind isn't synchronous from this script's point of view: the socket
# file doesn't exist until the API process has actually gotten around to binding it,
# which races against the UI exec'd right below. Without this wait, a UI request that
# lands before that bind finishes hits a socket path that doesn't exist yet and fails
# with "httpx.ConnectError: [Errno 2] No such file or directory" (mirrors pool.py's
# own startup wait for each per-revision datasette socket, via _check_health).
for _ in $(seq 1 100); do
    if [ -S "$API_SOCKET_PATH" ]; then
        break
    fi
    if ! kill -0 "$API_PID" 2>/dev/null; then
        echo "API process exited before binding $API_SOCKET_PATH" >&2
        exit 1
    fi
    sleep 0.1
done
if [ ! -S "$API_SOCKET_PATH" ]; then
    echo "API process did not bind $API_SOCKET_PATH in time" >&2
    exit 1
fi

export E2E_TEST_LITERALS_SERVICE_UI_API_SOCKET_PATH="$API_SOCKET_PATH"
exec uv run uvicorn src.e2e_test_literals.service_ui.app:app --host 0.0.0.0 --port "$UI_PORT"
