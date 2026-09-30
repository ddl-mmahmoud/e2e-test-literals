"""Async job queue for on-demand revision generation.

Same shape as changed-literals' `app.py`: one `threading.Thread` per job (no separate
worker process/broker -- deliberately, for the same reason changed-literals made that
call), `status` only ever moves forward (pending -> running -> done|error), and readers
take `_jobs_lock` just long enough to read/copy a consistent snapshot.
"""

from __future__ import annotations

import dataclasses
import threading
import time
import traceback
import uuid

from ..repo_checkout import GitError
from . import config
from .generation import resolve_and_generate


@dataclasses.dataclass
class Job:
    id: str
    status: str  # "pending" | "running" | "done" | "error"
    created_at: float
    sha: str | None = None
    error: str | None = None


_jobs_lock = threading.Lock()
_jobs: dict[str, Job] = {}


def prune_expired_jobs() -> None:
    cutoff = time.time() - config.JOB_RETENTION_SECONDS
    with _jobs_lock:
        expired = [job_id for job_id, job in _jobs.items() if job.created_at < cutoff]
        for job_id in expired:
            del _jobs[job_id]


def _run_job(job_id: str, repo: str, ref: str) -> None:
    with _jobs_lock:
        _jobs[job_id].status = "running"

    try:
        sha = resolve_and_generate(repo, ref)
    except GitError as exc:
        error = str(exc)
    except Exception:
        # Anything else is a bug, but this runs on a detached thread with no caller to
        # propagate to -- surfacing it as a failed job (with the traceback logged) beats
        # leaving the job stuck at "running" forever.
        traceback.print_exc()
        error = "internal error"
    else:
        with _jobs_lock:
            job = _jobs[job_id]
            job.status = "done"
            job.sha = sha
        return

    with _jobs_lock:
        job = _jobs[job_id]
        job.status = "error"
        job.error = error


def create_job(repo: str, ref: str) -> tuple[Job, threading.Thread]:
    """Register a new pending job and return it together with its (not-yet-started)
    worker thread. The caller should snapshot whatever response it wants to build from
    `job`'s "pending" state *before* calling `thread.start()` -- the thread races to
    flip `job.status` to "running" immediately, and a 202 response should reliably
    report the "pending" state the job was just created in (same reasoning as
    changed-literals' `create_job`)."""
    prune_expired_jobs()

    job_id = uuid.uuid4().hex
    job = Job(id=job_id, status="pending", created_at=time.time())
    with _jobs_lock:
        _jobs[job_id] = job

    thread = threading.Thread(target=_run_job, args=(job_id, repo, ref), daemon=True)
    return job, thread


def get_job(job_id: str) -> Job | None:
    with _jobs_lock:
        return _jobs.get(job_id)
