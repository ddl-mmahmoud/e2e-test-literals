"""Async job queue for `POST /changed-literals-impact`.

Same in-memory job/thread shape as `jobs.py` (no separate worker process/broker, no
persistence across a restart, `status` only ever moves forward: pending -> running ->
done|error). Kept as its own registry rather than reusing `jobs.py`'s, since these jobs
carry a different result payload (a full impact-check `dict`, see `impact.py`) than a
revision-build job (just a `sha`).
"""

from __future__ import annotations

import dataclasses
import threading
import time
import traceback
import uuid

from ..repo_checkout import GitError
from . import config
from .changed_literals_client import ChangedLiteralsError
from .impact import compute_impact


@dataclasses.dataclass
class ImpactJob:
    id: str
    status: str  # "pending" | "running" | "done" | "error"
    created_at: float
    result: dict | None = None
    error: str | None = None


_jobs_lock = threading.Lock()
_jobs: dict[str, ImpactJob] = {}


def prune_expired_jobs() -> None:
    cutoff = time.time() - config.JOB_RETENTION_SECONDS
    with _jobs_lock:
        expired = [job_id for job_id, job in _jobs.items() if job.created_at < cutoff]
        for job_id in expired:
            del _jobs[job_id]


def _run_job(
    job_id: str,
    test_repo: str,
    test_ref: str,
    literals_repo: str,
    base_ref: str,
    updated_ref: str,
    min_removal_confidence: float,
    auth_header: str | None,
) -> None:
    with _jobs_lock:
        _jobs[job_id].status = "running"

    try:
        result = compute_impact(
            test_repo, test_ref, literals_repo, base_ref, updated_ref, min_removal_confidence, auth_header
        )
    except (GitError, ChangedLiteralsError) as exc:
        error = str(exc)
    except Exception:
        # Anything else is a bug, but this runs on a detached thread with no caller to
        # propagate to -- surfacing it as a failed job (with the traceback logged) beats
        # leaving the job stuck at "running" forever (same reasoning as jobs.py).
        traceback.print_exc()
        error = "internal error"
    else:
        with _jobs_lock:
            job = _jobs[job_id]
            job.status = "done"
            job.result = result
        return

    with _jobs_lock:
        job = _jobs[job_id]
        job.status = "error"
        job.error = error


def create_job(
    test_repo: str,
    test_ref: str,
    literals_repo: str,
    base_ref: str,
    updated_ref: str,
    min_removal_confidence: float,
    auth_header: str | None = None,
) -> tuple[ImpactJob, threading.Thread]:
    """Register a new pending job and return it together with its (not-yet-started)
    worker thread. The caller should snapshot whatever response it wants to build from
    `job`'s "pending" state *before* calling `thread.start()` (same race as
    `jobs.create_job`).

    `auth_header` is the original `/changed-literals-impact` caller's own `Authorization`
    header value (see app.py), forwarded through to every changed-literals call -- see
    impact.compute_impact's docstring for why that's required."""
    prune_expired_jobs()

    job_id = uuid.uuid4().hex
    job = ImpactJob(id=job_id, status="pending", created_at=time.time())
    with _jobs_lock:
        _jobs[job_id] = job

    thread = threading.Thread(
        target=_run_job,
        args=(job_id, test_repo, test_ref, literals_repo, base_ref, updated_ref, min_removal_confidence, auth_header),
        daemon=True,
    )
    return job, thread


def get_job(job_id: str) -> ImpactJob | None:
    with _jobs_lock:
        return _jobs.get(job_id)
