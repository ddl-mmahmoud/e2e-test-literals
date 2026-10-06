"""Environment-variable configuration, read once at import time -- same convention as
changed-literals' app.py. Every other module in this package imports this module and
reads `config.NAME` rather than doing `from .config import NAME`, so tests can
monkeypatch a single attribute here (e.g. `monkeypatch.setattr(config, "DB_DIR", ...)`)
and have every call site see the change, the same way changed-literals' tests
monkeypatch attributes directly on `app`.
"""

from __future__ import annotations

import os
from pathlib import Path

# Resolved to absolute immediately: this is persistent, server-managed state, and a
# relative path here would be reinterpreted against whatever directory the process
# happens to be launched from. That previously let a git subprocess launched against
# an under-populated cache dir walk up into this very deploy checkout's own `.git`
# (found live: git fetch refusing to fetch into the checked-out `main` branch here).
DATA_DIR = Path(os.environ.get("E2E_TEST_LITERALS_SERVICE_DATA_DIR", "./e2e-test-literals-service-data")).resolve()

# Per-revision sqlite files, named `<sha>.sqlite`, retained forever (see
# DATASETTE-WRAPPER-PLAN.md Q4 -- cleanup is a deliberate non-goal for now).
DB_DIR = DATA_DIR / "db"

# filelock files, one per revision SHA, guarding concurrent generation (Q6).
LOCK_DIR = DATA_DIR / "locks"

# Bare-clone cache, one subdirectory per distinct repo URL (hashed -- see
# service/generation.py:repo_cache_path), analogous to changed-literals'
# CHANGED_LITERALS_REPO_CACHE_DIR. Also retained forever; git itself packs/GCs it.
REPO_CACHE_DIR = Path(
    os.environ.get("E2E_TEST_LITERALS_SERVICE_REPO_CACHE_DIR", str(DATA_DIR / "repo-cache"))
).resolve()

# Unix domain socket paths have a short max length (~100 bytes on Linux, in the
# kernel's sockaddr_un struct) -- kept short and independent of DATA_DIR, which
# callers may point somewhere with a long absolute path.
SOCKET_DIR = Path(os.environ.get("E2E_TEST_LITERALS_SERVICE_SOCKET_DIR", "/tmp/e2e-test-literals-service-sockets"))

# This process's own externally-facing API is bound to a Unix domain socket too, not
# just the per-revision datasette subprocesses above -- the Domino app hosting this
# exposes exactly one port through its ingress, and service_ui (a separate process in
# the same app, see repo-root app.sh) is this API's only caller, so the two never need
# the real network stack to talk to each other. Kept alongside the per-revision sockets
# in SOCKET_DIR for the same short-path reason noted above.
API_SOCKET_PATH = Path(
    os.environ.get("E2E_TEST_LITERALS_SERVICE_API_SOCKET_PATH", str(SOCKET_DIR / "api.sock"))
)

# How long a finished job is kept in memory before being pruned (mirrors
# changed-literals' CHANGED_LITERALS_JOB_RETENTION_SECONDS). Default 1 hour.
JOB_RETENTION_SECONDS = float(os.environ.get("E2E_TEST_LITERALS_SERVICE_JOB_RETENTION_SECONDS", "3600"))

# How long a per-revision `datasette serve` subprocess may sit idle (no proxied
# request) before it's killed -- the underlying sqlite file is untouched, so the next
# request just cheaply re-spawns a fresh process against it (Q10). Default 10 minutes.
IDLE_TIMEOUT_SECONDS = float(os.environ.get("E2E_TEST_LITERALS_SERVICE_IDLE_TIMEOUT_SECONDS", "600"))

# Domino's proxy may or may not strip this prefix before forwarding the request to the
# app, depending on deployment mode -- same handling as changed-literals' app.py, every
# route is registered under both the bare path and this prefixed one.
PREFIX = os.environ.get("DOMINO_RUN_HOST_PATH", "/")
if not PREFIX.endswith("/"):
    PREFIX += "/"

# Revision/Datasette-proxy routes live under this sub-path of PREFIX rather than
# directly at PREFIX -- keeps PREFIX's own root free for other static files the
# wrapper might need to serve, and keeps a request for Datasette's own shared
# `-/static/...` asset namespace (not tied to any single revision) from colliding with
# the sha-based catch-all proxy routes, which would otherwise treat the leading `-`
# segment as a bogus revision sha (see app.py/pool.py).
DATA_SEGMENT = "data/"
DATA_PREFIX = PREFIX + DATA_SEGMENT

# The path prefix at which this service's /data/ routes are ultimately reachable by a
# browser -- almost always just PREFIX, *except* in the real deployment (app.sh), where
# this process deliberately runs with DOMINO_RUN_HOST_PATH unset (so PREFIX is "/",
# keeping its internal, never-through-Domino traffic with service_ui unprefixed) while
# still being reverse-proxied, for actual browser traffic, by service_ui, which *does*
# carry the real Domino host path. Datasette's own self-generated absolute links
# (static assets, table/query/pagination links, ...) are rooted via its `base_url`
# setting (see pool.py's `_spawn`) and must match that real external prefix, or a
# browser fetching one of those links -- e.g. `-/static/app.css` -- 404s or never even
# reaches this app, since Domino's gateway routes by host path prefix. Set this
# independently of DOMINO_RUN_HOST_PATH (see app.sh) rather than threading the browser-
# facing prefix through service_ui's API responses: that would mean teaching service_ui
# about every URL shape Datasette can emit, instead of the one `base_url`-style knob
# Datasette already exposes for exactly this. Defaults to PREFIX, so a standalone
# deployment of this service (no separate UI reverse-proxying it) needs nothing extra.
EXTERNAL_PREFIX = os.environ.get("E2E_TEST_LITERALS_SERVICE_EXTERNAL_PREFIX", PREFIX)
if not EXTERNAL_PREFIX.endswith("/"):
    EXTERNAL_PREFIX += "/"

EXTERNAL_DATA_PREFIX = EXTERNAL_PREFIX + DATA_SEGMENT

# Unix domain socket of the sibling `changed_literals` process (see
# CHANGED-LITERALS-IMPACT-PLAN.md and changed_literals/app.py) -- another process
# within this same repo/Domino app, reached over a UDS like every other inter-process
# call here (service_ui/client.py, pool.py, proxy.py), not a separate Domino App over
# the real network anymore. Must match whatever path that process was actually
# started with (see repo-root app.sh, which sets both from one value).
CHANGED_LITERALS_SOCKET_PATH = Path(
    os.environ.get(
        "E2E_TEST_LITERALS_SERVICE_CHANGED_LITERALS_SOCKET_PATH",
        str(SOCKET_DIR / "changed-literals.sock"),
    )
)
