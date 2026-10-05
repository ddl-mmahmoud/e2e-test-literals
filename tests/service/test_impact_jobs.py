"""Unit tests for service/impact_jobs.py -- mirrors test_jobs.py's shape, with
`compute_impact` monkeypatched out (impact.py's own orchestration logic is covered by
test_impact.py)."""

from __future__ import annotations

import threading
import time

import pytest

from e2e_test_literals.repo_checkout import GitError
from e2e_test_literals.service import config, impact_jobs
from e2e_test_literals.service.changed_literals_client import ChangedLiteralsError

_ARGS = dict(
    test_repo="test-repo",
    test_ref="main",
    literals_repo="product-repo",
    base_ref="base",
    updated_ref="updated",
    min_removal_confidence=0.85,
    min_literal_match_confidence=0.9,
    match_method="substring_and_partial_ratio",
)


@pytest.fixture(autouse=True)
def _clear_jobs():
    with impact_jobs._jobs_lock:
        impact_jobs._jobs.clear()
    yield
    with impact_jobs._jobs_lock:
        impact_jobs._jobs.clear()


def _create_and_start() -> impact_jobs.ImpactJob:
    job, thread = impact_jobs.create_job(**_ARGS)
    thread.start()
    return job


def _await_done(job_id: str, *, timeout: float = 5.0) -> impact_jobs.ImpactJob:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = impact_jobs.get_job(job_id)
        if job.status in ("done", "error"):
            return job
        time.sleep(0.005)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


def test_create_job_starts_pending_before_the_thread_is_started(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(impact_jobs, "compute_impact", lambda *a, **k: release.wait(5) and {})

    job, thread = impact_jobs.create_job(**_ARGS)
    try:
        assert job.status == "pending"
        assert impact_jobs.get_job(job.id) is job
    finally:
        thread.start()
        release.set()
        _await_done(job.id)


def test_job_reaches_done_with_the_computed_result(monkeypatch):
    fake_result = {"total": 0, "findings": []}
    monkeypatch.setattr(impact_jobs, "compute_impact", lambda *a, **k: fake_result)

    job = _create_and_start()
    done = _await_done(job.id)

    assert done.status == "done"
    assert done.result == fake_result
    assert done.error is None


def test_job_reports_git_error(monkeypatch):
    monkeypatch.setattr(impact_jobs, "compute_impact", lambda *a, **k: (_ for _ in ()).throw(GitError("boom")))

    job = _create_and_start()
    done = _await_done(job.id)

    assert done.status == "error"
    assert "boom" in done.error


def test_job_reports_changed_literals_error(monkeypatch):
    monkeypatch.setattr(
        impact_jobs, "compute_impact", lambda *a, **k: (_ for _ in ()).throw(ChangedLiteralsError("remote boom"))
    )

    job = _create_and_start()
    done = _await_done(job.id)

    assert done.status == "error"
    assert "remote boom" in done.error


def test_job_reports_internal_error_without_crashing_the_worker(monkeypatch):
    monkeypatch.setattr(impact_jobs, "compute_impact", lambda *a, **k: (_ for _ in ()).throw(ValueError("kaboom")))

    job = _create_and_start()
    done = _await_done(job.id)

    assert done.status == "error"
    assert done.error == "internal error"


def test_get_job_returns_none_for_unknown_id():
    assert impact_jobs.get_job("does-not-exist") is None


def test_prune_expired_jobs_removes_only_old_ones(monkeypatch):
    monkeypatch.setattr(config, "JOB_RETENTION_SECONDS", 10.0)
    with impact_jobs._jobs_lock:
        impact_jobs._jobs["old"] = impact_jobs.ImpactJob(id="old", status="done", created_at=time.time() - 100)
        impact_jobs._jobs["fresh"] = impact_jobs.ImpactJob(id="fresh", status="done", created_at=time.time())

    impact_jobs.prune_expired_jobs()

    assert impact_jobs.get_job("old") is None
    assert impact_jobs.get_job("fresh") is not None
