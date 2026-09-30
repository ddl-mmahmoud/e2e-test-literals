"""Unit tests for service/jobs.py -- the job registry/lifecycle, with
`resolve_and_generate` monkeypatched out (generation.py's own tests cover the real
pipeline against a real repo)."""

from __future__ import annotations

import threading
import time

import pytest

from e2e_test_literals.repo_checkout import GitError
from e2e_test_literals.service import config, jobs


@pytest.fixture(autouse=True)
def _clear_jobs():
    with jobs._jobs_lock:
        jobs._jobs.clear()
    yield
    with jobs._jobs_lock:
        jobs._jobs.clear()


def _create_and_start(repo: str = "irrelevant", ref: str = "main") -> jobs.Job:
    job, thread = jobs.create_job(repo, ref)
    thread.start()
    return job


def _await_done(job_id: str, *, timeout: float = 5.0) -> jobs.Job:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = jobs.get_job(job_id)
        if job.status in ("done", "error"):
            return job
        time.sleep(0.005)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


def test_create_job_starts_pending_before_the_thread_is_started(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(jobs, "resolve_and_generate", lambda *a, **k: release.wait(5) and "deadbeef")

    job, thread = jobs.create_job("repo", "main")
    try:
        assert job.status == "pending"
        assert jobs.get_job(job.id) is job
    finally:
        thread.start()
        release.set()
        _await_done(job.id)


def test_job_reaches_done_with_the_resolved_sha(monkeypatch):
    monkeypatch.setattr(jobs, "resolve_and_generate", lambda repo, ref: "cafef00d")

    job = _create_and_start()
    done = _await_done(job.id)

    assert done.status == "done"
    assert done.sha == "cafef00d"
    assert done.error is None


def test_job_reports_git_error(monkeypatch):
    monkeypatch.setattr(jobs, "resolve_and_generate", lambda *a, **k: (_ for _ in ()).throw(GitError("boom")))

    job = _create_and_start()
    done = _await_done(job.id)

    assert done.status == "error"
    assert "boom" in done.error


def test_job_reports_internal_error_without_crashing_the_worker(monkeypatch):
    monkeypatch.setattr(
        jobs, "resolve_and_generate", lambda *a, **k: (_ for _ in ()).throw(ValueError("kaboom"))
    )

    job = _create_and_start()
    done = _await_done(job.id)

    assert done.status == "error"
    assert done.error == "internal error"


def test_get_job_returns_none_for_unknown_id():
    assert jobs.get_job("does-not-exist") is None


def test_prune_expired_jobs_removes_only_old_ones(monkeypatch):
    monkeypatch.setattr(config, "JOB_RETENTION_SECONDS", 10.0)
    with jobs._jobs_lock:
        jobs._jobs["old"] = jobs.Job(id="old", status="done", created_at=time.time() - 100)
        jobs._jobs["fresh"] = jobs.Job(id="fresh", status="done", created_at=time.time())

    jobs.prune_expired_jobs()

    assert jobs.get_job("old") is None
    assert jobs.get_job("fresh") is not None
