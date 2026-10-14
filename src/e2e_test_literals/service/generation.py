"""On-demand generation of the per-revision case-index sqlite db.

Reuses the existing pipeline exactly as-is -- `index_from_git` (bootstrap + parse +
Pass A/B) and `write_sqlite` (schema + writer) are untouched library calls, per
`e2e_test_literals/__init__.py`'s own docstring noting they're kept as plain
functions "so a future HTTP wrapper can call [them] directly". This module is only
the on-demand-caching glue around that pipeline: resolve a ref to a SHA, skip
regenerating a SHA that's already built, and serialize concurrent builds of the same
SHA with a file lock.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
import uuid

import filelock

from .. import index_from_git
from ..db import write_sqlite
from ..repo_checkout import resolve_commit_sha
from . import config
from .profiling import POPULATING_TEST_DB, RESOLVING_TEST_REVISION, PhaseTracker, track


def repo_cache_path(repo_url: str):
    """A per-repo bare-clone cache dir under `config.REPO_CACHE_DIR`, named by a hash
    of the URL -- mirrors changed-literals' `_repo_cache_path`, so one configured base
    directory serves every distinct repo this service is ever asked to index."""
    digest = hashlib.sha256(repo_url.encode()).hexdigest()[:24]
    return config.REPO_CACHE_DIR / digest


def db_path(sha: str):
    """Where a fully-generated revision's sqlite db lives once built."""
    return config.DB_DIR / f"{sha}.sqlite"


def _is_current_schema(path) -> bool:
    """Whether a previously-built db has the current schema: the routing tables' `meta`
    marker, and the `literals.string_kind` column. Dbs are retained forever and keyed by
    SHA, so ones built before either existed are still on disk; they get rebuilt in place
    once (a db built where the routing files were absent or failed still has `meta`, so
    that doesn't loop)."""
    conn = sqlite3.connect(path)
    try:
        has_meta = (
            conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone() is not None
        )
        has_string_kind = any(row[1] == "string_kind" for row in conn.execute("PRAGMA table_info(literals)"))
        return has_meta and has_string_kind
    finally:
        conn.close()


def _lock_path(sha: str):
    return config.LOCK_DIR / f"{sha}.lock"


def _repo_lock_path(repo_url: str):
    digest = hashlib.sha256(repo_url.encode()).hexdigest()[:24]
    return config.LOCK_DIR / f"repo-{digest}.lock"


def resolve_and_generate(
    repo_url: str, ref: str, *, skip_templated: bool = False, tracker: PhaseTracker | None = None
) -> str:
    """Resolve `ref` to a commit SHA, ensure `db_path(sha)` exists (building it if this
    is the first request for that SHA), and return the SHA.

    Caching/identity is keyed by the *resolved* SHA, not the raw `ref`, deliberately --
    a mutable ref like a branch name can resolve to a different SHA on a later call,
    and each distinct SHA gets its own db, retained forever (see
    DATASETTE-WRAPPER-PLAN.md Q4/Q5).

    A per-SHA `filelock` serializes concurrent generation requests for the same
    revision (Q6): the loser of the race blocks on the lock, then finds `db_path(sha)`
    already there and does no redundant work. The db itself is built into a
    uniquely-named temp file and atomically renamed into place, so a concurrent reader
    (a `datasette serve` subprocess, or another caller's fast-path existence check)
    never observes a partially-written file.

    A separate per-*repo* lock guards SHA resolution itself: `repo_checkout.
    ensure_repo_cache`'s check-then-clone has a check-then-act race if two threads
    both hit a not-yet-populated cache dir for the same repo at once (confirmed --
    both try `git clone --bare` into the same target and one fails outright). That
    race is in `ensure_repo_cache`/`fetch_ref`, shared, single-threaded-CLI-oriented
    code this module doesn't own, so rather than changing its locking behavior for
    every caller, concurrent callers here are simply serialized through resolution.
    Resolution (a fetch + rev-parse) is cheap next to the actual build, so this costs
    little even when two different SHAs of the same repo are requested at once.

    With a `tracker` the two stages are its `resolving_test_revision` and
    `populating_test_db` phases, noting the caches they consulted: the repo's bare clone
    (`repo_clone`: hit/miss) and the sha's db (`test_db`: hit, miss, or invalidated -- built
    under an older schema). The latter also notes `lock_wait_seconds`, how long this call
    waited on another build of the same sha, which a "hit" can owe all its time to.
    """
    cache = repo_cache_path(repo_url)

    config.LOCK_DIR.mkdir(parents=True, exist_ok=True)
    config.DB_DIR.mkdir(parents=True, exist_ok=True)

    with track(tracker, RESOLVING_TEST_REVISION) as phase:
        phase.caches["repo_clone"] = "hit" if cache.exists() and any(cache.iterdir()) else "miss"
        with filelock.FileLock(str(_repo_lock_path(repo_url))):
            sha = resolve_commit_sha(repo_url, ref, cache)

    with track(tracker, POPULATING_TEST_DB) as phase:
        lock_requested = time.monotonic()
        with filelock.FileLock(str(_lock_path(sha))):
            phase.extra["lock_wait_seconds"] = round(time.monotonic() - lock_requested, 3)
            out_path = db_path(sha)
            if not out_path.exists():
                phase.caches["test_db"] = "miss"
            elif not _is_current_schema(out_path):
                phase.caches["test_db"] = "invalidated"
            else:
                phase.caches["test_db"] = "hit"
            if phase.caches["test_db"] != "hit":
                result = index_from_git(repo_url, sha, repo_cache=cache, skip_templated=skip_templated)
                tmp_path = config.DB_DIR / f"{sha}.sqlite.tmp-{uuid.uuid4().hex}"
                write_sqlite(result.cases, tmp_path, routing=result.routing_entries, routing_error=result.routing_error)
                os.replace(tmp_path, out_path)

    return sha
