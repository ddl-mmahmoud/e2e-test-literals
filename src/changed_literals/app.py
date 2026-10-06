#!/usr/bin/env python3
"""
FastAPI service wrapping changed_literals.extractor.extract() as an HTTP API.

Headless by design -- no UI. The extraction itself is expensive (a clone,
a diff, a full AST walk), so this is a small worker-management interface
rather than one endpoint that blocks the caller until it's done:

  POST /changed-literals/jobs
             body: {"repo": ..., "base": ..., "updated": ...,
                     "languages": "ts,tsx" (optional), "refresh": false (optional)}
             -> 202 Accepted, body has job_id/status_url/result_url

  GET /changed-literals/jobs/<job_id>
             -> job status: pending | running | done | error

  GET /changed-literals/jobs/<job_id>/result
             [?offset=0&limit=200]
             -> once done: {"offset": ..., "limit": ..., "total": ...,
                "findings": [...]} -- one Finding per entry, sliced to the
                requested offset/limit window, with `total` reporting the
                full resultset size regardless of the window. 409 while
                still pending/running.

Routes live under the fixed `/changed-literals` prefix -- this process shares
one Domino app (and one deploy/e2e-test-literals repo) with
`e2e_test_literals.service`, reached over its own Unix domain socket (see
`main()` and repo-root `app.sh`), so the prefix is what distinguishes its API
namespace from that sibling service's rather than any Domino app-proxy path
rewriting (this process is never reached directly through Domino's ingress --
see e2e_test_literals/service/changed_literals_client.py, the only caller).

Submitting a job just starts a plain `threading.Thread` -- there's no
separate worker process or broker. That's deliberate: this tool isn't
mission-critical enough to justify that operational complexity, and a
thread forked from the main process is enough to keep one slow request
from blocking every other caller.

Environment variables:
  CHANGED_LITERALS_SOCKET_PATH
    Unix domain socket this process binds its API to (see `main()`).
    Default /tmp/changed-literals-service-sockets/changed-literals.sock.
    Must match whatever path e2e_test_literals.service.config's
    CHANGED_LITERALS_SOCKET_PATH is pointed at (see repo-root app.sh, which
    sets both from one value).
  CHANGED_LITERALS_REPO_CACHE_DIR
    Base directory for reusable bare-clone repo caches (see extract()'s
    repo_cache argument). Each distinct repo gets its own subdirectory
    under here, named by a hash of its URL, so one configured directory
    serves every repo this service is asked to look at. If unset, every
    job clones fresh into a temp dir that's discarded once the job
    completes -- correct but slow for repeated hits against the same repo.
  CHANGED_LITERALS_CACHE_TTL_SECONDS
    How long an extraction result is reused across jobs for the same
    (repo, base, updated, languages) before being recomputed, so two jobs
    submitted back to back against the same diff don't redo the whole
    clone/diff/AST-walk. Default 300 (5 minutes). Pass "refresh": true in
    a job's body to bypass this early.
  CHANGED_LITERALS_JOB_RETENTION_SECONDS
    How long a finished job (and its findings) is kept in memory before
    it's pruned, so long-running deployments don't accumulate jobs
    forever. Default 3600 (1 hour).
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import threading
import time
import traceback
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from changed_literals.extractor import extract
from changed_literals.finding import Finding
from changed_literals.git_ops import GitError
from changed_literals.languages import PATHSPECS

# Fixed, not DOMINO_RUN_HOST_PATH-derived -- unlike e2e_test_literals.service's own
# PREFIX, this process is never reached directly through Domino's ingress (only
# changed_literals_client.py, over a UDS internal to this same app, ever calls it), so
# there's no app-proxy path-stripping behavior to accommodate here. This is purely the
# namespace prefix distinguishing this API from its sibling e2e-test-literals-service
# one (see module docstring).
PREFIX = "/changed-literals/"

SOCKET_PATH = Path(
    os.environ.get("CHANGED_LITERALS_SOCKET_PATH", "/tmp/changed-literals-service-sockets/changed-literals.sock")
)

REPO_CACHE_DIR = os.environ.get("CHANGED_LITERALS_REPO_CACHE_DIR")
CACHE_TTL_SECONDS = float(os.environ.get("CHANGED_LITERALS_CACHE_TTL_SECONDS", "300"))
JOB_RETENTION_SECONDS = float(os.environ.get("CHANGED_LITERALS_JOB_RETENTION_SECONDS", "3600"))
DEFAULT_PAGE_SIZE = 200
MAX_PAGE_SIZE = 2000

app = FastAPI(title="changed-literals")


def _repo_cache_path(repo_url: str) -> Path | None:
    """A per-repo subdirectory under REPO_CACHE_DIR, or None if unset.

    extract()'s repo_cache is a single bare clone of one repo, but this
    service is asked about many repos over its lifetime, so the one
    configured base directory is split into per-repo subdirectories, named
    by a hash of the URL to keep it filesystem-safe.
    """
    if not REPO_CACHE_DIR:
        return None
    digest = hashlib.sha256(repo_url.encode()).hexdigest()[:24]
    return Path(REPO_CACHE_DIR) / digest


def _parse_languages(languages: str | None) -> set[str] | None:
    if not languages:
        return None
    names = {name.strip() for name in languages.split(",") if name.strip()}
    unknown = names - set(PATHSPECS)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"unknown language(s) {sorted(unknown)}; choose from {sorted(PATHSPECS)}",
        )
    return names


# ── Findings cache ───────────────────────────────────────────────────────
#
# Keyed by (repo, base, updated, languages), so two jobs submitted against
# the same extraction request reuse one computed result instead of
# re-cloning and re-walking the diff twice. Entries expire after
# CACHE_TTL_SECONDS; a job's `refresh: true` bypasses this and recomputes
# regardless of age.

_cache_lock = threading.Lock()
_cache: dict[tuple, dict] = {}


def _cache_key(repo: str, base: str, updated: str, languages: set[str] | None) -> tuple:
    return (repo, base, updated, tuple(sorted(languages)) if languages else None)


def _cached_findings(
    repo: str, base: str, updated: str, languages: set[str] | None, *, force_refresh: bool
) -> list[Finding]:
    key = _cache_key(repo, base, updated, languages)

    if not force_refresh:
        with _cache_lock:
            entry = _cache.get(key)
        if entry is not None and time.time() - entry["computed_at"] < CACHE_TTL_SECONDS:
            return entry["findings"]

    findings = extract(repo, base, updated, languages, repo_cache=_repo_cache_path(repo))

    with _cache_lock:
        _cache[key] = {"findings": findings, "computed_at": time.time()}
    return findings


def _paginate(items: list, offset: int, limit: int) -> tuple[list, int]:
    return items[offset : offset + limit], len(items)


# ── Jobs ─────────────────────────────────────────────────────────────────
#
# One Job per POST /changed-literals/jobs, run on its own thread. `status`
# only ever moves forward (pending -> running -> done|error); readers take
# `_jobs_lock` just long enough to read/copy a consistent snapshot.


@dataclasses.dataclass
class Job:
    id: str
    status: str  # "pending" | "running" | "done" | "error"
    created_at: float
    findings: list[Finding] | None = None
    error: str | None = None


_jobs_lock = threading.Lock()
_jobs: dict[str, Job] = {}


def _prune_expired_jobs() -> None:
    cutoff = time.time() - JOB_RETENTION_SECONDS
    with _jobs_lock:
        expired = [job_id for job_id, job in _jobs.items() if job.created_at < cutoff]
        for job_id in expired:
            del _jobs[job_id]


def _run_job(
    job_id: str, repo: str, base: str, updated: str, languages: set[str] | None, refresh: bool
) -> None:
    with _jobs_lock:
        _jobs[job_id].status = "running"

    try:
        findings = _cached_findings(repo, base, updated, languages, force_refresh=refresh)
    except GitError as exc:
        error = str(exc)
    except Exception:
        # Anything else is a bug, but this is running on a detached thread
        # with no caller to propagate to -- surfacing it as a failed job
        # (with the traceback logged) beats leaving the job stuck at
        # "running" forever.
        traceback.print_exc()
        error = "internal error"
    else:
        with _jobs_lock:
            job = _jobs[job_id]
            job.status = "done"
            job.findings = findings
        return

    with _jobs_lock:
        job = _jobs[job_id]
        job.status = "error"
        job.error = error


class JobRequest(BaseModel):
    repo: str
    base: str
    updated: str
    languages: str | None = None
    refresh: bool = False


def _job_status_path(job_id: str) -> str:
    return f"{PREFIX}jobs/{job_id}"


def _job_result_path(job_id: str) -> str:
    return f"{PREFIX}jobs/{job_id}/result"


def _job_body(job: Job) -> dict:
    body = {"job_id": job.id, "status": job.status, "status_url": _job_status_path(job.id)}
    if job.status == "error":
        body["error"] = job.error
    if job.status == "done":
        body["result_url"] = _job_result_path(job.id)
    return body


async def create_job(payload: JobRequest) -> Response:
    parsed_languages = _parse_languages(payload.languages)
    _prune_expired_jobs()

    job_id = uuid.uuid4().hex
    job = Job(id=job_id, status="pending", created_at=time.time())
    with _jobs_lock:
        _jobs[job_id] = job

    # Snapshot the body before starting the thread -- it races to flip
    # `job.status` to "running" immediately, and the 202 response should
    # reliably report the "pending" state the job was just created in.
    body = _job_body(job)

    thread = threading.Thread(
        target=_run_job,
        args=(job_id, payload.repo, payload.base, payload.updated, parsed_languages, payload.refresh),
        daemon=True,
    )
    thread.start()

    return JSONResponse(status_code=202, content=body, headers={"Location": _job_status_path(job_id)})


def _get_job(job_id: str) -> Job:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no such job {job_id!r}")
    return job


def job_status(job_id: str) -> dict:
    return _job_body(_get_job(job_id))


def job_result(
    job_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> dict:
    job = _get_job(job_id)

    if job.status in ("pending", "running"):
        raise HTTPException(
            status_code=409,
            detail=f"job {job_id} is {job.status}; poll {_job_status_path(job_id)} until done",
        )
    if job.status == "error":
        raise HTTPException(status_code=500, detail=job.error)

    assert job.findings is not None  # status == "done" guarantees this
    page_items, total = _paginate(job.findings, offset, limit)

    return {
        "offset": offset,
        "limit": limit,
        "total": total,
        "findings": [dataclasses.asdict(f) for f in page_items],
    }


app.add_api_route(f"{PREFIX}jobs", create_job, methods=["POST"], status_code=202)
app.add_api_route(f"{PREFIX}jobs/{{job_id}}", job_status, methods=["GET"])
app.add_api_route(f"{PREFIX}jobs/{{job_id}}/result", job_result, methods=["GET"])


@app.get("/")
def index() -> dict:
    return {
        "service": "changed-literals",
        "usage": (
            f'POST {PREFIX}jobs  body: {{"repo": ..., "base": ..., "updated": ...}}'
            f" -> 202 with status_url/result_url"
        ),
    }


def main() -> None:
    import uvicorn

    # Bound to a Unix domain socket, not a TCP port -- see SOCKET_PATH's docstring.
    # uvicorn's own uds bind doesn't clear a stale socket file left behind by a prior
    # process, so this must too, or a restart fails to bind with "address already in
    # use" (mirrors e2e_test_literals.service.app.main's identical handling).
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    SOCKET_PATH.unlink(missing_ok=True)
    uvicorn.run(app, uds=str(SOCKET_PATH))


if __name__ == "__main__":
    main()
