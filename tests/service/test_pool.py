"""Integration tests for service/pool.py -- these actually spawn a real `datasette
serve` subprocess over a Unix domain socket and talk to it, rather than mocking
subprocess management, since the whole point of this module is "does a real
`datasette serve` process actually come up healthy over a UDS and get proxied to"."""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from e2e_test_literals.db import write_sqlite
from e2e_test_literals.service import config
from e2e_test_literals.service.pool import DatasettePool, DatasetteStartupError


@pytest.fixture
def pool_env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "DB_DIR", tmp_path / "db")
    # NOT under tmp_path: pytest's per-test tmp_path is long (nested under
    # /tmp/pytest-of-<user>/pytest-<n>/<sanitized-test-name>/...), and a 40-char sha
    # pushes the full socket path past Linux's ~108-byte sockaddr_un limit (confirmed
    # by hitting exactly this while writing these tests). Production's real default
    # (config.py's SOCKET_DIR) is deliberately short for the same reason -- mirror
    # that here with our own short scratch dir instead.
    socket_dir = Path(tempfile.mkdtemp(prefix="e2e-svc-test-"))
    monkeypatch.setattr(config, "SOCKET_DIR", socket_dir)
    monkeypatch.setattr(config, "IDLE_TIMEOUT_SECONDS", 0.2)
    config.DB_DIR.mkdir(parents=True)
    pool = DatasettePool()
    try:
        yield pool
    finally:
        pool.shutdown()
        shutil.rmtree(socket_dir, ignore_errors=True)


def _write_fake_revision_db(sha: str) -> None:
    write_sqlite({}, config.DB_DIR / f"{sha}.sqlite")


def _get_over_uds(socket_path: Path, path: str) -> httpx.Response:
    transport = httpx.HTTPTransport(uds=str(socket_path))
    with httpx.Client(transport=transport, base_url="http://datasette", timeout=5.0) as client:
        return client.get(path)


def test_ensure_started_spawns_a_healthy_process(pool_env: DatasettePool):
    _write_fake_revision_db("deadbeef")

    socket_path = pool_env.ensure_started("deadbeef")

    assert socket_path.exists()
    assert pool_env.is_running("deadbeef")
    resp = _get_over_uds(socket_path, "/")
    assert resp.status_code == 200


def test_ensure_started_serves_the_actual_schema(pool_env: DatasettePool):
    _write_fake_revision_db("cafef00d")

    socket_path = pool_env.ensure_started("cafef00d")

    resp = _get_over_uds(socket_path, "/cafef00d/literals.json")
    assert resp.status_code == 200
    assert resp.json()["rows"] == []


def test_ensure_started_is_idempotent_for_a_running_process(pool_env: DatasettePool):
    _write_fake_revision_db("abc123")

    first = pool_env.ensure_started("abc123")
    pid_before = pool_env._processes["abc123"].popen.pid
    second = pool_env.ensure_started("abc123")

    assert first == second
    assert pool_env._processes["abc123"].popen.pid == pid_before


def test_ensure_started_raises_for_unknown_revision(pool_env: DatasettePool):
    with pytest.raises(FileNotFoundError):
        pool_env.ensure_started("never-generated")


def test_stop_kills_the_process_and_removes_the_socket(pool_env: DatasettePool):
    _write_fake_revision_db("feedface")
    socket_path = pool_env.ensure_started("feedface")

    pool_env._stop("feedface")

    assert not pool_env.is_running("feedface")
    assert not socket_path.exists()


def test_reap_idle_stops_processes_past_the_idle_timeout(pool_env: DatasettePool):
    _write_fake_revision_db("0ff1ce")
    pool_env.ensure_started("0ff1ce")

    time.sleep(0.3)  # IDLE_TIMEOUT_SECONDS is 0.2 in pool_env
    pool_env.reap_idle()

    assert not pool_env.is_running("0ff1ce")


def test_reap_idle_leaves_recently_touched_processes_running(pool_env: DatasettePool):
    _write_fake_revision_db("5ea1ed")
    pool_env.ensure_started("5ea1ed")

    time.sleep(0.1)
    pool_env.touch("5ea1ed")
    time.sleep(0.1)
    pool_env.reap_idle()  # 0.1s since touch, still under the 0.2s timeout

    assert pool_env.is_running("5ea1ed")


def test_ensure_started_after_eviction_respawns_from_the_still_on_disk_db(pool_env: DatasettePool):
    _write_fake_revision_db("beefcafe")
    pool_env.ensure_started("beefcafe")
    pool_env._stop("beefcafe")
    assert not pool_env.is_running("beefcafe")

    socket_path = pool_env.ensure_started("beefcafe")

    assert pool_env.is_running("beefcafe")
    assert _get_over_uds(socket_path, "/").status_code == 200


def test_shutdown_stops_every_running_process(pool_env: DatasettePool):
    _write_fake_revision_db("111111")
    _write_fake_revision_db("222222")
    pool_env.ensure_started("111111")
    pool_env.ensure_started("222222")

    pool_env.shutdown()

    assert not pool_env.is_running("111111")
    assert not pool_env.is_running("222222")
