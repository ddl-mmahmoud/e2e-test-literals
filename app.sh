#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# Runs all three processes that make up this one Domino App: changed_literals (another
# process entirely, see src/changed_literals/app.py), the e2e-test-literals-service API
# (service/app.py), and the e2e-test-literals-service UI (service_ui/app.py) -- the only
# one bound to the port Domino actually exposes. The UI reverse-proxies everything it
# doesn't have its own route for straight through to the API over a Unix domain socket
# (see service_ui/proxy.py) -- including each revision's Datasette pages -- and the API
# in turn reaches changed_literals over a second, separate Unix domain socket (see
# service/changed_literals_client.py) -- so only one port needs to be exposed at all,
# and the Domino app hosting this (which can be strict about ports a hosted app opens)
# never sees either backing process open one in the first place.
CHANGED_LITERALS_SOCKET_PATH="${CHANGED_LITERALS_SOCKET_PATH:-/tmp/changed-literals-service-sockets/changed-literals.sock}"
API_SOCKET_PATH="${E2E_TEST_LITERALS_SERVICE_API_SOCKET_PATH:-/tmp/e2e-test-literals-service-sockets/api.sock}"
UI_PORT=8888

wait_for_socket() {
    local name="$1" socket_path="$2" pid="$3"
    for _ in $(seq 1 100); do
        if [ -S "$socket_path" ]; then
            return 0
        fi
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "$name exited before binding $socket_path" >&2
            exit 1
        fi
        sleep 0.1
    done
    echo "$name did not bind $socket_path in time" >&2
    exit 1
}

mkdir -p "$(dirname "$CHANGED_LITERALS_SOCKET_PATH")"
rm -f "$CHANGED_LITERALS_SOCKET_PATH"
CHANGED_LITERALS_SOCKET_PATH="$CHANGED_LITERALS_SOCKET_PATH" \
    uv run changed-literals-service &
CHANGED_LITERALS_PID=$!
trap 'kill "$CHANGED_LITERALS_PID" 2>/dev/null || true' EXIT
wait_for_socket changed_literals "$CHANGED_LITERALS_SOCKET_PATH" "$CHANGED_LITERALS_PID"

mkdir -p "$(dirname "$API_SOCKET_PATH")"
rm -f "$API_SOCKET_PATH"

# The API process must not apply DOMINO_RUN_HOST_PATH to its own routing: it's never
# reached directly, only via the UI's internal, unprefixed calls (client.py /
# proxy.py). It still needs to know the real externally-visible prefix separately,
# though (E2E_TEST_LITERALS_SERVICE_EXTERNAL_PREFIX, same value DOMINO_RUN_HOST_PATH
# would have been), so each per-revision `datasette serve` it spawns roots its own
# self-generated links (static assets, table/pagination links, ...) at the prefix a
# browser -- reaching them through the UI's single exposed port, not this socket --
# will actually request them at. See service/config.py's EXTERNAL_PREFIX docstring.
env -u DOMINO_RUN_HOST_PATH \
    E2E_TEST_LITERALS_SERVICE_API_SOCKET_PATH="$API_SOCKET_PATH" \
    E2E_TEST_LITERALS_SERVICE_EXTERNAL_PREFIX="${DOMINO_RUN_HOST_PATH:-/}" \
    E2E_TEST_LITERALS_SERVICE_CHANGED_LITERALS_SOCKET_PATH="$CHANGED_LITERALS_SOCKET_PATH" \
    uv run uvicorn src.e2e_test_literals.service.app:app --uds "$API_SOCKET_PATH" &
API_PID=$!
trap 'kill "$API_PID" "$CHANGED_LITERALS_PID" 2>/dev/null || true' EXIT

# uvicorn's --uds bind isn't synchronous from this script's point of view: the socket
# file doesn't exist until the API process has actually gotten around to binding it,
# which races against the UI exec'd right below. Without this wait, a UI request that
# lands before that bind finishes hits a socket path that doesn't exist yet and fails
# with "httpx.ConnectError: [Errno 2] No such file or directory" (mirrors pool.py's
# own startup wait for each per-revision datasette socket, via _check_health).
wait_for_socket e2e-test-literals-service-api "$API_SOCKET_PATH" "$API_PID"

export E2E_TEST_LITERALS_SERVICE_UI_API_SOCKET_PATH="$API_SOCKET_PATH"
exec uv run uvicorn src.e2e_test_literals.service_ui.app:app --host 0.0.0.0 --port "$UI_PORT"
