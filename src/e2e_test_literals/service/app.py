#!/usr/bin/env python3
"""
FastAPI wrapper exposing on-demand, per-revision `e2e_test_literals` case-index
generation through a raw, standard HTTP query interface -- Datasette -- rather than
any bespoke query endpoints of our own. See DATASETTE-WRAPPER-PLAN.md at the repo root
for the design.

Headless, no UI of its own. Generating a revision's db is potentially expensive (a
clone/fetch, a full feature-file parse, an AST walk of templatized steps), so this is a
small job-management interface rather than one endpoint that blocks the caller until
it's done -- mirrors changed-literals' app.py:

  POST /revisions
             body: {"repo": ..., "ref": "main" (optional, default "main")}
             -> 202 Accepted, body has job_id/status_url/result_url

  GET /revisions/jobs/<job_id>
             -> job status: pending | running | done | error

  GET /revisions/jobs/<job_id>/result
             -> once done: {"sha": ..., "datasette_url": ...} -- datasette_url is the
                raw Datasette instance for that revision: its own JSON table API
                (`.../literals.json?value__contains=...`), its arbitrary read-only SQL
                endpoint (`...?sql=SELECT ...`), and anything else Datasette exposes.
                409 while still pending/running, same as changed-literals.

  <anything>/{sha}[.json]?sql=...      the raw arbitrary-SQL endpoint, and the db index
  <anything>/{sha}/{table}.json         one table, with Datasette's usual filter DSL
  <anything>/{sha}/...                  anything else Datasette exposes (canned
                                         queries, CSV export, the HTML UI, ...)

             -> reverse-proxied straight into that revision's `datasette serve`
                subprocess (see pool.py), started on demand if it isn't already
                running. This is the "raw and standard as possible" interface itself --
                everything under this path is Datasette's own API, untouched. Two
                wrapper routes are needed (not one) because Datasette's own URL scheme
                has both a bare, single-segment form (`/{sha}` / `/{sha}.json`, used by
                the db index *and* the `?sql=` endpoint) and a multi-segment form
                (`/{sha}/{table}.json`) -- see proxy_revision_top/proxy_revision below.

Environment variables: see service/config.py.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import config
from .jobs import Job, create_job, get_job
from .pool import pool
from .proxy import proxy_request

DEFAULT_REF = "main"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    pool.start_reaper()
    try:
        yield
    finally:
        pool.shutdown()


app = FastAPI(title="e2e-test-literals-service", lifespan=_lifespan)


class RevisionRequest(BaseModel):
    repo: str
    ref: str = DEFAULT_REF


def _job_status_path(job_id: str) -> str:
    return f"{config.PREFIX}revisions/jobs/{job_id}"


def _job_result_path(job_id: str) -> str:
    return f"{config.PREFIX}revisions/jobs/{job_id}/result"


def _datasette_url(sha: str) -> str:
    # Bare, no trailing slash -- Datasette's own canonical single-segment db-index
    # URL (its self-generated links use this exact shape too, e.g. a table's `path`
    # field). `{url}.json` is the db index as JSON and doubles as the raw SQL endpoint
    # (`{url}.json?sql=...`); `{url}/{table}.json` is a single table.
    return f"{config.PREFIX}{sha}"


def _job_body(job: Job) -> dict:
    body = {"job_id": job.id, "status": job.status, "status_url": _job_status_path(job.id)}
    if job.status == "error":
        body["error"] = job.error
    if job.status == "done":
        body["result_url"] = _job_result_path(job.id)
    return body


async def create_revision(payload: RevisionRequest) -> JSONResponse:
    job, thread = create_job(payload.repo, payload.ref)
    # Snapshot the body before starting the thread -- it races to flip job.status to
    # "running" immediately, and the 202 response should reliably report the "pending"
    # state the job was just created in (see jobs.create_job's docstring).
    body = _job_body(job)
    thread.start()
    return JSONResponse(status_code=202, content=body, headers={"Location": _job_status_path(job.id)})


def _get_job_or_404(job_id: str) -> Job:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no such job {job_id!r}")
    return job


def revision_job_status(job_id: str) -> dict:
    return _job_body(_get_job_or_404(job_id))


def revision_job_result(job_id: str) -> dict:
    job = _get_job_or_404(job_id)

    if job.status in ("pending", "running"):
        raise HTTPException(
            status_code=409,
            detail=f"job {job_id} is {job.status}; poll {_job_status_path(job_id)} until done",
        )
    if job.status == "error":
        raise HTTPException(status_code=500, detail=job.error)

    assert job.sha is not None  # status == "done" guarantees this
    pool.ensure_started(job.sha)  # eager per DATASETTE-WRAPPER-PLAN.md Q11 -- warm
    # before handing the URL back, so the very first proxied request isn't a cold start.
    return {"sha": job.sha, "datasette_url": _datasette_url(job.sha)}


async def proxy_revision_top(sha_and_ext: str, request: Request):
    """Handles Datasette's bare, single-path-segment URLs: the db index
    (`/{sha}` / `/{sha}.json`) and the raw SQL endpoint, which is the db index plus a
    `?sql=` query param on the same path -- there is no separate "/sql" route."""
    sha = sha_and_ext[: -len(".json")] if sha_and_ext.endswith(".json") else sha_and_ext
    socket_path = await run_in_threadpool(pool.ensure_started, sha)
    # `sha_and_ext` is already exactly Datasette's own native path for this request
    # (see pool.py's `_spawn` on why base_url is set so no further rewriting is
    # needed here).
    return await proxy_request(request, socket_path, sha_and_ext)


async def proxy_revision(sha: str, path: str, request: Request):
    socket_path = await run_in_threadpool(pool.ensure_started, sha)
    # Reconstruct the `sha` segment rather than stripping it: it's simultaneously our
    # own routing segment *and* Datasette's own db-name-from-filename segment (the db
    # is literally named `sha`, see generation.py's `<sha>.sqlite` naming) -- Datasette
    # needs it once, e.g. `/{sha}/literals.json`, not zero times (see pool.py's
    # `_spawn` for why base_url can't be used to add it back instead).
    return await proxy_request(request, socket_path, f"{sha}/{path}")


_PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

app.add_api_route("/revisions", create_revision, methods=["POST"], status_code=202)
app.add_api_route("/revisions/jobs/{job_id}", revision_job_status, methods=["GET"])
app.add_api_route("/revisions/jobs/{job_id}/result", revision_job_result, methods=["GET"])
# Registered last: both proxy routes below are catch-alls (one single-segment, one
# multi-segment -- see proxy_revision_top's docstring for why both are needed), and
# Starlette matches routes in registration order, so every more specific route above
# must come first.
app.add_api_route("/{sha_and_ext}", proxy_revision_top, methods=_PROXY_METHODS)
app.add_api_route("/{sha}/{path:path}", proxy_revision, methods=_PROXY_METHODS)

if config.PREFIX != "/":
    app.add_api_route(config.PREFIX + "revisions", create_revision, methods=["POST"], status_code=202)
    app.add_api_route(config.PREFIX + "revisions/jobs/{job_id}", revision_job_status, methods=["GET"])
    app.add_api_route(config.PREFIX + "revisions/jobs/{job_id}/result", revision_job_result, methods=["GET"])
    app.add_api_route(config.PREFIX + "{sha_and_ext}", proxy_revision_top, methods=_PROXY_METHODS)
    app.add_api_route(config.PREFIX + "{sha}/{path:path}", proxy_revision, methods=_PROXY_METHODS)


@app.get("/")
def index() -> dict:
    return {
        "service": "e2e-test-literals-service",
        "usage": (
            f'POST {config.PREFIX}revisions  body: {{"repo": ..., "ref": "main"}}'
            " -> 202 with status_url/result_url; result.datasette_url is that "
            "revision's raw Datasette query interface (JSON table API + SQL endpoint)"
        ),
    }


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8889)


if __name__ == "__main__":
    main()
