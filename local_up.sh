#!/usr/bin/env bash
# Boots changed-literals and e2e-test-literals-service locally, wired to each other,
# so check_impact.sh (pointed at 127.0.0.1 instead of the Domino-hosted BASE) can be
# exercised end-to-end without either app actually being deployed.
#
# Mirrors this repo's own app.sh in spirit (background a process, wait for it to
# actually be reachable before starting the thing that depends on it, clean up on
# exit) but across the two separate services rather than within one.
#
# Both services default to TCP port 8888 when run standalone (changed-literals'
# app.py hardcodes it; this repo's app.sh hardcodes UI_PORT=8888) -- that's fine in
# their real, separately-hosted Domino Apps, but the two can't both claim 8888 on one
# machine. So here changed-literals runs on CHANGED_LITERALS_PORT (default 8081,
# bypassing its own app.sh/__main__ by invoking uvicorn directly against its
# module-level `app`) and e2e-test-literals-service keeps its normal 8888 default,
# pointed at the other service via E2E_TEST_LITERALS_SERVICE_CHANGED_LITERALS_URL.
set -euo pipefail

E2E_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHANGED_LITERALS_DIR="${CHANGED_LITERALS_DIR:-$HOME/deploy/changed-literals}"

CHANGED_LITERALS_PORT="${CHANGED_LITERALS_PORT:-8081}"
E2E_UI_PORT="${E2E_UI_PORT:-8888}"

CHANGED_LITERALS_URL="http://127.0.0.1:${CHANGED_LITERALS_PORT}"
E2E_URL="http://127.0.0.1:${E2E_UI_PORT}"

LOG_DIR="${LOG_DIR:-/tmp/e2e-test-literals-local-run}"
mkdir -p "$LOG_DIR"

if [[ ! -d "$CHANGED_LITERALS_DIR" ]]; then
    echo "changed-literals checkout not found at $CHANGED_LITERALS_DIR (set CHANGED_LITERALS_DIR)" >&2
    exit 1
fi

PIDS=()
cleanup() {
    for pid in "${PIDS[@]:-}"; do
        # Each service was launched via setsid, so its pid is also its process
        # group id -- killing -pid takes down every process it spawned (e.g.
        # e2e-test-literals-service's own API subprocess, started inside app.sh)
        # too, not just the top-level shell.
        kill -- "-$pid" 2>/dev/null || true
    done
}
trap cleanup EXIT INT TERM

wait_for() {
    local name="$1" url="$2" pid="$3"
    for _ in $(seq 1 150); do
        if curl -sS -o /dev/null -f "$url" 2>/dev/null; then
            return 0
        fi
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "$name exited before becoming reachable -- see $LOG_DIR/$name.log" >&2
            exit 1
        fi
        sleep 0.2
    done
    echo "$name did not become reachable at $url in time -- see $LOG_DIR/$name.log" >&2
    exit 1
}

echo "== starting changed-literals on :$CHANGED_LITERALS_PORT ==" >&2
setsid uv run --project "$CHANGED_LITERALS_DIR" \
    uvicorn --app-dir "$CHANGED_LITERALS_DIR" app:app --host 127.0.0.1 --port "$CHANGED_LITERALS_PORT" \
    >"$LOG_DIR/changed-literals.log" 2>&1 &
CHANGED_LITERALS_PID=$!
PIDS+=("$CHANGED_LITERALS_PID")
wait_for changed-literals "$CHANGED_LITERALS_URL/" "$CHANGED_LITERALS_PID"
echo "   changed-literals up (pid $CHANGED_LITERALS_PID)" >&2

echo "== starting e2e-test-literals-service on :$E2E_UI_PORT ==" >&2
setsid env \
    E2E_TEST_LITERALS_SERVICE_CHANGED_LITERALS_URL="$CHANGED_LITERALS_URL" \
    bash -c "cd '$E2E_DIR' && exec ./app.sh" \
    >"$LOG_DIR/e2e-test-literals-service.log" 2>&1 &
E2E_PID=$!
PIDS+=("$E2E_PID")
wait_for e2e-test-literals-service "$E2E_URL/" "$E2E_PID"
echo "   e2e-test-literals-service up (pid $E2E_PID)" >&2

cat >&2 <<EOF

Both services are up:
  changed-literals           -> $CHANGED_LITERALS_URL  (log: $LOG_DIR/changed-literals.log)
  e2e-test-literals-service  -> $E2E_URL  (log: $LOG_DIR/e2e-test-literals-service.log)

To run check_impact.sh against this local pair, point it at:
  BASE="$E2E_URL"
and drop the "Authorization: Bearer \$CLOUD_DOGFOOD_PAT" headers, or just export
CLOUD_DOGFOOD_PAT=anything -- neither service enforces auth when run standalone like
this (the real Bearer/SSO check happens in Domino's app-proxy gateway in front of
them, which isn't in play here). GITHUB_LITERALS_PAT still needs to be a real,
working GitHub PAT if you want the test/literals repo checkouts to succeed.

Ctrl-C to stop both.
EOF

wait "$CHANGED_LITERALS_PID" "$E2E_PID"
