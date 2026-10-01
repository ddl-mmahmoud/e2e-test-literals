"""Manages a pool of `datasette serve` subprocesses, one per revision.

Each process serves exactly one, already-built, immutable revision db (see
generation.py -- a revision's sqlite file is content-addressed by SHA and never
modified again once written), bound to a Unix domain socket rather than a TCP port
(the Domino infrastructure hosting this can be strict about ports a hosted app opens),
and reverse-proxied by app.py. Processes are started eagerly, right after a revision's
db finishes building, and idle-evicted after `config.IDLE_TIMEOUT_SECONDS` of no
proxied request; the underlying sqlite file is untouched by eviction, so a later
request just cheaply re-spawns a fresh process against it.

Deliberately *not* Datasette-as-a-library: plain `datasette serve` subprocesses, one
per revision, need no integration with Datasette's internals at all -- see
DATASETTE-WRAPPER-PLAN.md Q1/Q9 for the reasoning.
"""

from __future__ import annotations

import dataclasses
import subprocess
import threading
import time
from pathlib import Path

import httpx

from . import config
from .generation import db_path

_STARTUP_TIMEOUT_SECONDS = 10.0
_STARTUP_POLL_INTERVAL_SECONDS = 0.05
_REAP_INTERVAL_SECONDS = 60.0


class DatasetteStartupError(RuntimeError):
    """A `datasette serve` subprocess exited, or never became healthy, before the
    startup timeout."""


@dataclasses.dataclass
class _Process:
    sha: str
    socket_path: Path
    popen: subprocess.Popen
    last_used: float


def _check_health(socket_path: Path) -> bool:
    try:
        transport = httpx.HTTPTransport(uds=str(socket_path))
        with httpx.Client(transport=transport, base_url="http://datasette", timeout=1.0) as client:
            return client.get("/").status_code == 200
    except (httpx.HTTPError, OSError):
        return False


class DatasettePool:
    def __init__(self) -> None:
        self._processes: dict[str, _Process] = {}
        self._processes_lock = threading.Lock()
        self._spawn_locks: dict[str, threading.Lock] = {}
        self._spawn_locks_guard = threading.Lock()
        self._reaper_stop = threading.Event()
        self._reaper_thread: threading.Thread | None = None

    # ── lifecycle ────────────────────────────────────────────────────────

    def start_reaper(self) -> None:
        self._reaper_stop.clear()
        self._reaper_thread = threading.Thread(target=self._reap_loop, daemon=True)
        self._reaper_thread.start()

    def shutdown(self) -> None:
        """Stop the reaper and every live subprocess -- called from app.py's FastAPI
        lifespan shutdown, so a wrapper restart doesn't orphan `datasette serve`
        processes still bound to now-stale socket files."""
        self._reaper_stop.set()
        with self._processes_lock:
            shas = list(self._processes)
        for sha in shas:
            self._stop(sha)

    def _reap_loop(self) -> None:
        while not self._reaper_stop.wait(_REAP_INTERVAL_SECONDS):
            self.reap_idle()

    def reap_idle(self) -> None:
        cutoff = time.time() - config.IDLE_TIMEOUT_SECONDS
        with self._processes_lock:
            idle_shas = [sha for sha, proc in self._processes.items() if proc.last_used < cutoff]
        for sha in idle_shas:
            self._stop(sha)

    # ── starting / finding a revision's process ─────────────────────────

    def _spawn_lock(self, sha: str) -> threading.Lock:
        with self._spawn_locks_guard:
            return self._spawn_locks.setdefault(sha, threading.Lock())

    def ensure_started(self, sha: str) -> Path:
        """Return the Unix socket path of a live `datasette serve` process serving
        revision `sha`, starting one (health-checked before returning) if none is
        currently running. Safe to call repeatedly/concurrently for the same `sha`:
        the common case (already running) takes only a quick lock-guarded read; a
        cold start is serialized per-`sha` so two concurrent callers don't race to
        spawn two processes for the same revision."""
        with self._processes_lock:
            proc = self._processes.get(sha)
        if proc is not None and proc.popen.poll() is None:
            proc.last_used = time.time()
            return proc.socket_path

        with self._spawn_lock(sha):
            with self._processes_lock:
                proc = self._processes.get(sha)
            if proc is not None and proc.popen.poll() is None:
                proc.last_used = time.time()
                return proc.socket_path
            return self._spawn(sha)

    def touch(self, sha: str) -> None:
        with self._processes_lock:
            proc = self._processes.get(sha)
        if proc is not None:
            proc.last_used = time.time()

    def _spawn(self, sha: str) -> Path:
        db = db_path(sha)
        if not db.exists():
            raise FileNotFoundError(f"no generated db for revision {sha!r} at {db}")

        config.SOCKET_DIR.mkdir(parents=True, exist_ok=True)
        socket_path = config.SOCKET_DIR / f"{sha}.sock"
        if socket_path.exists():
            # Stale from a killed/crashed prior process -- datasette refuses to bind
            # over an existing socket file.
            socket_path.unlink()

        # base_url is Datasette's *mount point*, not just link-generation cosmetics --
        # confirmed empirically: a request path that literally starts with the
        # configured base_url string has that prefix stripped before Datasette's own
        # db-name/table-name routing runs (conditionally -- a request that arrives
        # *without* that prefix, like every internal proxied request this wrapper
        # actually sends, is left alone and still routes normally; base_url only
        # changes the mount point, it doesn't require it). It must therefore never be
        # `{...}data/{sha}/` -- the db name derived from this file is itself `sha` (see
        # db_path's `<sha>.sqlite` naming), so a base_url containing `sha` would
        # collide with and swallow that same segment out of every real request
        # (verified: it turns `/data/{sha}/literals.json` into a lookup for a database
        # literally named "literals", 404ing).
        #
        # It's `config.EXTERNAL_DATA_PREFIX`, not `config.DATA_PREFIX`: this value only
        # drives Datasette's *self-generated* absolute links (static assets,
        # pagination, the HTML UI, ...), which a real browser fetches directly -- they
        # must be rooted at the prefix the browser's origin (service_ui, reverse-
        # proxying this process) is actually reachable under, which can differ from
        # this process's own internal PREFIX (always "/" in production, see app.sh and
        # config.py's EXTERNAL_PREFIX docstring). Every request this wrapper proxies
        # *into* Datasette is still sent unprefixed (app.py's proxy forwards the `sha`
        # segment through unchanged), which is exactly what the "conditional" stripping
        # above means still works no matter what base_url is set to.
        base_url = config.EXTERNAL_DATA_PREFIX
        argv = [
            "datasette",
            "serve",
            "--immutable",
            str(db),
            "--uds",
            str(socket_path),
            "--setting",
            "base_url",
            base_url,
        ]
        popen = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        deadline = time.time() + _STARTUP_TIMEOUT_SECONDS
        healthy = False
        while time.time() < deadline:
            if popen.poll() is not None:
                raise DatasetteStartupError(
                    f"datasette serve for revision {sha} exited during startup "
                    f"(code {popen.returncode})"
                )
            if socket_path.exists() and _check_health(socket_path):
                healthy = True
                break
            time.sleep(_STARTUP_POLL_INTERVAL_SECONDS)

        if not healthy:
            popen.kill()
            popen.wait()
            raise DatasetteStartupError(f"datasette serve for revision {sha} did not become healthy in time")

        proc = _Process(sha=sha, socket_path=socket_path, popen=popen, last_used=time.time())
        with self._processes_lock:
            self._processes[sha] = proc
        return socket_path

    def _stop(self, sha: str) -> None:
        with self._processes_lock:
            proc = self._processes.pop(sha, None)
        if proc is None:
            return
        proc.popen.terminate()
        try:
            proc.popen.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.popen.kill()
            proc.popen.wait()
        proc.socket_path.unlink(missing_ok=True)

    def is_running(self, sha: str) -> bool:
        with self._processes_lock:
            proc = self._processes.get(sha)
        return proc is not None and proc.popen.poll() is None


pool = DatasettePool()
