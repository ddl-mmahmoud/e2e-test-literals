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

DATA_DIR = Path(os.environ.get("E2E_TEST_LITERALS_SERVICE_DATA_DIR", "./e2e-test-literals-service-data"))

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
)

# Unix domain socket paths have a short max length (~100 bytes on Linux, in the
# kernel's sockaddr_un struct) -- kept short and independent of DATA_DIR, which
# callers may point somewhere with a long absolute path.
SOCKET_DIR = Path(os.environ.get("E2E_TEST_LITERALS_SERVICE_SOCKET_DIR", "/tmp/e2e-test-literals-service-sockets"))

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
