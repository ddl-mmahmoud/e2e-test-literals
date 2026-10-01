"""Environment-variable configuration for the revision-builder UI.

Deliberately its own tiny config, independent of `service/config.py`: this UI is a
separate app that knows nothing about e2e_test_literals' generation pipeline, job
threading, or Datasette subprocess pool -- everything it needs from
`e2e-test-literals-service` it gets by calling that service's public HTTP API
(see client.py), the same way any other caller of that API would.
"""

from __future__ import annotations

import os
from pathlib import Path

# Unix domain socket of the e2e-test-literals-service API this UI is a front end for
# (that process's own `config.API_SOCKET_PATH` -- see its docstring). Communicating
# over a UDS rather than a TCP port means the two processes never touch the real
# network stack to talk to each other, which matters because the Domino app hosting
# both of them (see repo-root app.sh) only allows one port to be reached through its
# ingress at all -- this socket isn't that port, and never needs to be. Every request
# this UI can't handle itself, including a revision's Datasette pages (datasette_url),
# is reverse-proxied through to it (see proxy.py/app.py's `proxy_passthrough`), so the
# browser only ever talks to this UI's own origin. Must match whatever path the API
# process was actually started with.
API_SOCKET_PATH = Path(
    os.environ.get(
        "E2E_TEST_LITERALS_SERVICE_UI_API_SOCKET_PATH",
        "/tmp/e2e-test-literals-service-sockets/api.sock",
    )
)

# Same convention as service/config.py's PREFIX: Domino's proxy may or may not strip
# this prefix before forwarding the request to the app, so every route this app serves
# is registered under both the bare path and this prefixed one.
PREFIX = os.environ.get("DOMINO_RUN_HOST_PATH", "/")
if not PREFIX.endswith("/"):
    PREFIX += "/"
