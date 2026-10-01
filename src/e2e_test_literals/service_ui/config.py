"""Environment-variable configuration for the revision-builder UI.

Deliberately its own tiny config, independent of `service/config.py`: this UI is a
separate app that knows nothing about e2e_test_literals' generation pipeline, job
threading, or Datasette subprocess pool -- everything it needs from
`e2e-test-literals-service` it gets by calling that service's public HTTP API
(see client.py), the same way any other caller of that API would.
"""

from __future__ import annotations

import os

# Base URL of the e2e-test-literals-service API this UI is a front end for. Only needs
# to be reachable from this process -- every request this UI can't handle itself,
# including a revision's Datasette pages (datasette_url), is reverse-proxied through to
# it (see proxy.py/app.py's `proxy_passthrough`), so the browser only ever talks to
# this UI's own origin. In the default single-Domino-App deployment (see repo-root
# app.sh) that API process is started on a localhost-only port and never exposed
# directly.
API_BASE_URL = os.environ.get("E2E_TEST_LITERALS_SERVICE_UI_API_BASE_URL", "http://localhost:8889").rstrip("/")

# Same convention as service/config.py's PREFIX: Domino's proxy may or may not strip
# this prefix before forwarding the request to the app, so every route this app serves
# is registered under both the bare path and this prefixed one.
PREFIX = os.environ.get("DOMINO_RUN_HOST_PATH", "/")
if not PREFIX.endswith("/"):
    PREFIX += "/"
