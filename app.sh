#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# Runs both services in one Domino App: the API (service/app.py) on a localhost-only
# port nothing external ever talks to, and the UI (service_ui/app.py) on the port
# Domino actually exposes. The UI reverse-proxies everything it doesn't have its own
# route for straight through to the API (see service_ui/proxy.py) -- including each
# revision's Datasette pages -- so only one port needs to be exposed at all.
API_PORT=8889
UI_PORT=8888

# The API process must not apply DOMINO_RUN_HOST_PATH itself: it's never reached
# directly, only via the UI's internal, unprefixed calls (client.py / proxy.py).
env -u DOMINO_RUN_HOST_PATH uv run uvicorn src.e2e_test_literals.service.app:app \
    --host 127.0.0.1 --port "$API_PORT" &
API_PID=$!
trap 'kill "$API_PID" 2>/dev/null || true' EXIT

export E2E_TEST_LITERALS_SERVICE_UI_API_BASE_URL="http://127.0.0.1:${API_PORT}"
exec uv run uvicorn src.e2e_test_literals.service_ui.app:app --host 0.0.0.0 --port "$UI_PORT"
