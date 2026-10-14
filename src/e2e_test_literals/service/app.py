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

  GET /revisions
             -> {"revisions": [
                    {"sha", "datasette_url", "built_at", "running", "status": "ready"},
                    {"sha": null, "datasette_url": null, "built_at": null, "running":
                     false, "status": "building", "job_id", "started_at"},
                    ...
                ]}, newest first -- every revision that has finished building,
                discovered directly from the sqlite files on disk, plus every revision
                still mid-build, from the in-flight job registry (see `list_revisions`
                below). Lets a caller (e.g. a separate UI, see Next steps in
                DATASETTE-WRAPPER-PLAN.md) list everything in flight or already
                available without tracking job ids of its own.

  GET /revisions/jobs/<job_id>
             -> job status: pending | running | done | error

  GET /revisions/jobs/<job_id>/result
             -> once done: {"sha": ..., "datasette_url": ...} -- datasette_url is the
                raw Datasette instance for that revision: its own JSON table API
                (`.../literals.json?value__contains=...`), its arbitrary read-only SQL
                endpoint (`...?sql=SELECT ...`), and anything else Datasette exposes.
                409 while still pending/running, same as changed-literals.

  POST /changed-literals-impact
             body: {"test_repo": ..., "test_ref": "main", "literals_repo": ...,
                    "base_ref": ..., "updated_ref": ..., "min_removal_confidence": 0.85,
                    (or "pr_number": <GitHub PR of literals_repo> in place of
                    base_ref/updated_ref: diffs the PR's base against its branch, via
                    the same GITHUB_LITERALS_PAT used for cloning; the result reports
                    the SHAs it resolved to as base_ref/updated_ref)
                    "min_literal_match_confidence": 0.7,
                    "min_invocation_confidence": 0.0, "invocation_max_length": 300,
                    "extra_cases_allowed": null, "missing_cases_allowed": null}
             -> 202 Accepted, same job_id/status_url/result_url shape as /revisions (a
                done job's status also has report_url: result_url with ?report=1).
                Builds the test revision if needed, asks changed-literals to diff
                literals_repo between base_ref/updated_ref, and checks which of its
                REMOVED findings (at/above min_removal_confidence) contain a literal
                the test revision depends on (at/above min_literal_match_confidence).
                How a literal is compared depends on what kind of string it is
                (its `string_kind`): button/link/tab/... labels must equal the removed
                string, freeform text only has to occur in it (graded by a fuzzy score),
                test-data names match exactly at a flat 0.3 (so only when the floor is
                lowered), and opaque values (usernames, ids, ...) never match. See
                step_kinds.py, matching.py and CHANGED-LITERALS-IMPACT-PLAN.md. The result also carries `recommended_invocations`: the
                `/test-e2e` lines (as strings, and as structured dicts) covering every
                matched case whose confidence is at least min_invocation_confidence
                (default 0.0 = all), each line at most invocation_max_length chars
                (0 = no limit). See invocations.py. If extra_cases_allowed and/or
                missing_cases_allowed is given (the other is then unbounded), shared
                scenario tags replace testrail(N) tags within those bounds, each
                structured entry reporting the `extra_cases` its tags also run; if
                neither is given tags are never substituted. If the bounds can't be
                met, plain tags are returned with an explanation in `warnings`.

  GET /changed-literals-impact/jobs/<job_id>[/result]
             -> same job-status/result shape as /revisions/jobs/<job_id>[/result], except that
                the status also says what the job is doing and how long each part has taken:
                "phase" (queued | resolving_test_revision | populating_test_db |
                loading_test_literals | resolving_pull_request | checking_findings_cache |
                fetching_literals_repo | analyzing_literals_diff | matching_literals | computing_invocations |
                optimizing_invocation_tags | done | error) and "profiling": {"total_seconds",
                "phases": {<phase>: null if not run (yet), else {"state": running | done |
                failed, "seconds", "caches": {<cache>: hit | miss | expired | invalidated |
                bypassed | disabled}}}} -- see profiling.py. A running phase's seconds are
                its time so far; on a done job they are the final figures. The result
                holds every detected change (removed findings annotated with their matched
                test literals and `replacements`, added ones as they are). With
                `/result?report=1` it is served as the HTML impact report instead
                (report_page.py).

  POST /changed-literals-impact/invocations
             body: {"test_sha": ..., "case_ids": [...], "invocation_max_length": 300,
                    "extra_cases_allowed": null, "missing_cases_allowed": null}
             -> the `recommended_invocations` stanza for exactly those cases, from the
                already-built revision's sqlite alone (404 if it isn't there). What the
                report page calls when entries are toggled.

  <anything>/data/{sha}[.json|.db]?sql=...   the raw arbitrary-SQL endpoint, the db
                                              index, and the raw sqlite download
  <anything>/data/{sha}/{table}.json          one table, with Datasette's usual filter
                                               DSL
  <anything>/data/{sha}/...                   anything else Datasette exposes (canned
                                               queries, CSV export, the HTML UI, ...)

             -> reverse-proxied straight into that revision's `datasette serve`
                subprocess (see pool.py), started on demand if it isn't already
                running. This is the "raw and standard as possible" interface itself --
                everything under this path is Datasette's own API, untouched. Two
                wrapper routes are needed (not one) because Datasette's own URL scheme
                has both a bare, single-segment form (`/data/{sha}` / `/data/{sha}.json`
                / `/data/{sha}.db`, used by the db index, the `?sql=` endpoint, and the
                raw sqlite download) and a multi-segment form (`/data/{sha}/{table}.json`)
                -- see proxy_revision_top/proxy_revision below.

                Revision routes live under a fixed `/data/` segment rather than
                directly at the root so that the root stays free for actual static
                files (favicons, a future UI, ...) and so Datasette's own shared
                `-/static/...` asset namespace -- not tied to any one revision -- can't
                be swallowed by the sha-based catch-alls below (see
                `datasette_static_asset`, and config.py's DATA_PREFIX).

Environment variables: see service/config.py.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import datasette as _datasette_package
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, model_validator
from starlette.concurrency import run_in_threadpool

from . import config, impact, impact_jobs
from .generation import db_path
from .invocations import DEFAULT_MAX_LENGTH as DEFAULT_INVOCATION_MAX_LENGTH
from .invocations import recommend_invocations
from .jobs import Job, create_job, get_job, list_active_jobs
from .pool import pool
from .proxy import proxy_request
from .report_page import render_report

DEFAULT_REF = "main"

# Datasette's own bundled static assets (app.css, table.js, codemirror, the SQL
# formatter, ...) -- identical across every per-revision subprocess since they come
# from the installed `datasette` package, not from any revision's db. See
# `datasette_static_asset` below for why the wrapper serves these directly.
_DATASETTE_STATIC_DIR = Path(_datasette_package.__file__).parent / "static"


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


class ImpactRequest(BaseModel):
    test_repo: str
    test_ref: str = DEFAULT_REF
    literals_repo: str
    base_ref: str | None = None
    updated_ref: str | None = None
    pr_number: int | None = Field(None, ge=1)
    min_removal_confidence: float = impact.DEFAULT_MIN_REMOVAL_CONFIDENCE
    min_literal_match_confidence: float = impact.DEFAULT_MIN_LITERAL_MATCH_CONFIDENCE
    min_invocation_confidence: float = impact.DEFAULT_MIN_INVOCATION_CONFIDENCE
    invocation_max_length: int = Field(impact.DEFAULT_INVOCATION_MAX_LENGTH, ge=0)
    extra_cases_allowed: int | None = Field(None, ge=0)
    missing_cases_allowed: int | None = Field(None, ge=0)

    @model_validator(mode="after")
    def _exactly_one_way_to_name_the_diff(self) -> ImpactRequest:
        if self.pr_number is None and not (self.base_ref and self.updated_ref):
            raise ValueError("give either base_ref and updated_ref, or pr_number")
        if self.pr_number is not None and (self.base_ref or self.updated_ref):
            raise ValueError("pr_number can't be combined with base_ref/updated_ref")
        return self


class InvocationsRequest(BaseModel):
    """Recompute `recommended_invocations` for a hand-picked set of cases (the report page's
    toggles) against an already-built test revision."""

    test_sha: str = Field(pattern=r"^[0-9a-f]{7,64}$")  # a db filename: never a path
    case_ids: list[int]
    invocation_max_length: int = Field(DEFAULT_INVOCATION_MAX_LENGTH, ge=0)
    extra_cases_allowed: int | None = Field(None, ge=0)
    missing_cases_allowed: int | None = Field(None, ge=0)


_logged_origins: set[str] = set()


def _external_origin(request: Request) -> str:
    """scheme://host a client reaches this service at, or "" if unknown (callers then
    emit path-only URLs). `config.EXTERNAL_ORIGIN` wins; otherwise the first value of
    the `X-Forwarded-Host`/`X-Forwarded-Proto` headers service_ui sets. Deliberately
    not the plain `Host` header: over the UI's unix socket that is a placeholder."""
    if config.EXTERNAL_ORIGIN:
        origin, source = config.EXTERNAL_ORIGIN, "E2E_TEST_LITERALS_SERVICE_EXTERNAL_ORIGIN"
    else:
        host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
        if not host:
            origin, source = "", "no x-forwarded-host header"
        else:
            proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip() or "https"
            origin, source = f"{proto}://{host}", "x-forwarded-host/-proto headers"
    # Once per distinct result, so a client polling a job doesn't flood the log.
    if origin not in _logged_origins:
        _logged_origins.add(origin)
        print(f"external origin for job URLs: {origin or '(none, path-only URLs)'} -- from {source}", flush=True)
    return origin


def _external_url(request: Request, path: str) -> str:
    """`path` (relative to the service root, no leading slash) as an absolute URL where
    the origin is known. Rooted at EXTERNAL_PREFIX rather than PREFIX: they differ in
    the real deployment (see config.py), and a client only ever sees the external one."""
    return f"{_external_origin(request)}{config.EXTERNAL_PREFIX}{path}"


def _job_status_path(job_id: str) -> str:
    return f"{config.PREFIX}revisions/jobs/{job_id}"


def _datasette_url(sha: str) -> str:
    # Bare, no trailing slash -- Datasette's own canonical single-segment db-index
    # URL (its self-generated links use this exact shape too, e.g. a table's `path`
    # field), rooted under DATA_PREFIX (see config.py). `{url}.json` is the db index
    # as JSON and doubles as the raw SQL endpoint (`{url}.json?sql=...`);
    # `{url}/{table}.json` is a single table.
    return f"{config.DATA_PREFIX}{sha}"


def _job_body(job: Job, request: Request) -> dict:
    body = {
        "job_id": job.id,
        "status": job.status,
        "status_url": _external_url(request, f"revisions/jobs/{job.id}"),
    }
    if job.status == "error":
        body["error"] = job.error
    if job.status == "done":
        body["result_url"] = _external_url(request, f"revisions/jobs/{job.id}/result")
    return body


async def create_revision(payload: RevisionRequest, request: Request) -> JSONResponse:
    job, thread = create_job(payload.repo, payload.ref)
    # Snapshot the body before starting the thread -- it races to flip job.status to
    # "running" immediately, and the 202 response should reliably report the "pending"
    # state the job was just created in (see jobs.create_job's docstring).
    body = _job_body(job, request)
    thread.start()
    return JSONResponse(status_code=202, content=body, headers={"Location": _job_status_path(job.id)})


def list_revisions() -> dict:
    """Every already-built revision, discovered directly from `config.DB_DIR` -- the db
    file's existence on disk *is* the source of truth for "this revision is built"
    (generation.py writes it via a temp-file-then-`os.replace`, so a partially-built
    revision never shows up here; see its docstring) -- plus every revision still
    mid-build, from `jobs.list_active_jobs()`. The latter have no `sha`/`datasette_url`
    yet (the job itself hasn't resolved a sha), so callers must treat `status` as the
    field to branch on, not presence of those other fields."""
    config.DB_DIR.mkdir(parents=True, exist_ok=True)
    built = [
        {
            "sha": db_file.stem,
            "datasette_url": _datasette_url(db_file.stem),
            "built_at": datetime.fromtimestamp(db_file.stat().st_mtime, tz=timezone.utc).isoformat(),
            "running": pool.is_running(db_file.stem),
            "status": "ready",
        }
        for db_file in config.DB_DIR.glob("*.sqlite")
    ]
    building = [
        {
            "sha": None,
            "datasette_url": None,
            "built_at": None,
            "running": False,
            "status": "building",
            "job_id": job.id,
            "started_at": datetime.fromtimestamp(job.created_at, tz=timezone.utc).isoformat(),
        }
        for job in list_active_jobs()
    ]
    revisions = built + building
    # Both `built_at` and `started_at` are isoformat with an explicit timezone, so
    # lexical sort order matches chronological order for either.
    revisions.sort(key=lambda r: r["built_at"] or r["started_at"], reverse=True)
    return {"revisions": revisions}


def _get_job_or_404(job_id: str) -> Job:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no such job {job_id!r}")
    return job


def revision_job_status(job_id: str, request: Request) -> dict:
    return _job_body(_get_job_or_404(job_id), request)


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


def _impact_job_status_path(job_id: str) -> str:
    return f"{config.PREFIX}changed-literals-impact/jobs/{job_id}"


def _impact_job_phase(job: impact_jobs.ImpactJob) -> str:
    if job.status in ("done", "error"):
        return job.status
    return job.tracker.current or "queued"


def _impact_job_body(job: impact_jobs.ImpactJob, request: Request) -> dict:
    body = {
        "job_id": job.id,
        "status": job.status,
        "phase": _impact_job_phase(job),
        "status_url": _external_url(request, f"changed-literals-impact/jobs/{job.id}"),
        "profiling": job.tracker.snapshot(),
    }
    if job.status == "error":
        body["error"] = job.error
    if job.status == "done":
        body["result_url"] = _external_url(request, f"changed-literals-impact/jobs/{job.id}/result")
        body["report_url"] = f"{body['result_url']}?report=1"
    return body


async def create_impact_job(payload: ImpactRequest, request: Request) -> JSONResponse:
    job, thread = impact_jobs.create_job(
        payload.test_repo,
        payload.test_ref,
        payload.literals_repo,
        payload.base_ref,
        payload.updated_ref,
        payload.min_removal_confidence,
        payload.min_literal_match_confidence,
        payload.min_invocation_confidence,
        payload.invocation_max_length,
        payload.extra_cases_allowed,
        payload.missing_cases_allowed,
        payload.pr_number,
    )
    # Snapshot before starting the thread -- same race as create_revision above.
    body = _impact_job_body(job, request)
    thread.start()
    return JSONResponse(status_code=202, content=body, headers={"Location": _impact_job_status_path(job.id)})


def _get_impact_job_or_404(job_id: str) -> impact_jobs.ImpactJob:
    job = impact_jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no such job {job_id!r}")
    return job


def impact_job_status(job_id: str, request: Request) -> dict:
    return _impact_job_body(_get_impact_job_or_404(job_id), request)


def impact_job_result(job_id: str, report: bool = False):
    """The job's result as JSON, or -- with `?report=1` -- as the HTML impact report (see
    report_page.py)."""
    job = _get_impact_job_or_404(job_id)

    if job.status in ("pending", "running"):
        raise HTTPException(
            status_code=409,
            detail=f"job {job_id} is {job.status}; poll {_impact_job_status_path(job_id)} until done",
        )
    if job.status == "error":
        raise HTTPException(status_code=500, detail=job.error)

    assert job.result is not None  # status == "done" guarantees this
    if report:
        return HTMLResponse(render_report(job.result))
    return job.result


def impact_invocations(payload: InvocationsRequest) -> dict:
    """Only reads the revision's sqlite -- no fetch, clone or analysis -- so it is cheap enough
    to call on every toggle of the report page."""
    db_file = db_path(payload.test_sha)
    if not db_file.exists():
        raise HTTPException(
            status_code=404,
            detail=f"no built test revision {payload.test_sha}; run the impact check again to rebuild it",
        )
    return recommend_invocations(
        db_file,
        payload.case_ids,
        max_length=payload.invocation_max_length,
        extra_cases_allowed=payload.extra_cases_allowed,
        missing_cases_allowed=payload.missing_cases_allowed,
    )


_SHA_EXTENSIONS = (".json", ".db")


def _strip_sha_extension(sha_and_ext: str) -> str:
    """Datasette's single-segment URL allows a `.json` suffix (db index / raw-SQL
    endpoint as JSON) or a `.db` suffix (download the raw sqlite file) after the db
    name -- strip whichever is present to recover the bare sha for pool lookup.
    `sha_and_ext` itself (forwarded to Datasette unchanged) is untouched."""
    for ext in _SHA_EXTENSIONS:
        if sha_and_ext.endswith(ext):
            return sha_and_ext[: -len(ext)]
    return sha_and_ext


async def proxy_revision_top(sha_and_ext: str, request: Request):
    """Handles Datasette's bare, single-path-segment URLs: the db index
    (`/data/{sha}` / `/data/{sha}.json`), the raw SQL endpoint (the db index plus a
    `?sql=` query param on the same path -- there is no separate "/sql" route), and the
    raw sqlite download (`/data/{sha}.db`)."""
    sha = _strip_sha_extension(sha_and_ext)
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
    # needs it once, e.g. `/data/{sha}/literals.json`, not zero times (see pool.py's
    # `_spawn` for why base_url can't be used to add it back instead).
    return await proxy_request(request, socket_path, f"{sha}/{path}")


def datasette_static_asset(path: str) -> FileResponse:
    """Serves Datasette's own bundled static assets directly rather than proxying them
    to a per-revision subprocess: a request for one of these (`-/static/app.css`,
    `-/static/table.js`, ...) isn't tied to any particular revision -- it's generated
    by Datasette's `base_url`-relative links (see pool.py's `_spawn`) and is byte-for-
    byte identical regardless of which subprocess would have served it, so there's no
    single "right" subprocess to proxy it to, and no need to spin one up just for this."""
    target = (_DATASETTE_STATIC_DIR / path).resolve()
    if _DATASETTE_STATIC_DIR not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(target)


_PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

app.add_api_route("/revisions", create_revision, methods=["POST"], status_code=202)
app.add_api_route("/revisions", list_revisions, methods=["GET"])
app.add_api_route("/revisions/jobs/{job_id}", revision_job_status, methods=["GET"])
app.add_api_route("/revisions/jobs/{job_id}/result", revision_job_result, methods=["GET"])
app.add_api_route("/changed-literals-impact", create_impact_job, methods=["POST"], status_code=202)
app.add_api_route("/changed-literals-impact/jobs/{job_id}", impact_job_status, methods=["GET"])
app.add_api_route("/changed-literals-impact/jobs/{job_id}/result", impact_job_result, methods=["GET"])
app.add_api_route("/changed-literals-impact/invocations", impact_invocations, methods=["POST"])
# Registered before the catch-alls below (Starlette matches routes in registration
# order): a request for Datasette's shared static-asset namespace would otherwise be
# swallowed by the sha-based catch-alls, which would treat the leading `-` segment as
# a bogus revision sha.
app.add_api_route(f"/{config.DATA_SEGMENT}-/static/{{path:path}}", datasette_static_asset, methods=["GET", "HEAD"])
# Both proxy routes below are catch-alls (one single-segment, one multi-segment -- see
# proxy_revision_top's docstring for why both are needed), rooted under DATA_SEGMENT
# rather than directly at "/" so the root itself is never shadowed by them (see
# config.py's DATA_PREFIX).
app.add_api_route(f"/{config.DATA_SEGMENT}{{sha_and_ext}}", proxy_revision_top, methods=_PROXY_METHODS)
app.add_api_route(f"/{config.DATA_SEGMENT}{{sha}}/{{path:path}}", proxy_revision, methods=_PROXY_METHODS)

if config.PREFIX != "/":
    app.add_api_route(config.PREFIX + "revisions", create_revision, methods=["POST"], status_code=202)
    app.add_api_route(config.PREFIX + "revisions", list_revisions, methods=["GET"])
    app.add_api_route(config.PREFIX + "revisions/jobs/{job_id}", revision_job_status, methods=["GET"])
    app.add_api_route(config.PREFIX + "revisions/jobs/{job_id}/result", revision_job_result, methods=["GET"])
    app.add_api_route(config.PREFIX + "changed-literals-impact", create_impact_job, methods=["POST"], status_code=202)
    app.add_api_route(config.PREFIX + "changed-literals-impact/jobs/{job_id}", impact_job_status, methods=["GET"])
    app.add_api_route(
        config.PREFIX + "changed-literals-impact/jobs/{job_id}/result", impact_job_result, methods=["GET"]
    )
    app.add_api_route(config.PREFIX + "changed-literals-impact/invocations", impact_invocations, methods=["POST"])
    app.add_api_route(config.DATA_PREFIX + "-/static/{path:path}", datasette_static_asset, methods=["GET", "HEAD"])
    app.add_api_route(config.DATA_PREFIX + "{sha_and_ext}", proxy_revision_top, methods=_PROXY_METHODS)
    app.add_api_route(config.DATA_PREFIX + "{sha}/{path:path}", proxy_revision, methods=_PROXY_METHODS)


@app.get("/")
def index() -> dict:
    return {
        "service": "e2e-test-literals-service",
        "usage": (
            f'POST {config.PREFIX}revisions  body: {{"repo": ..., "ref": "main"}}'
            " -> 202 with status_url/result_url; result.datasette_url is that "
            "revision's raw Datasette query interface (JSON table API + SQL endpoint). "
            f"GET {config.PREFIX}revisions lists revisions already built. "
            f"POST {config.PREFIX}changed-literals-impact  body: {{\"test_repo\": ..., "
            '"test_ref": "main", "literals_repo": ..., "base_ref": ..., "updated_ref": ...,'
            ' (or "pr_number": ... in place of base_ref/updated_ref)'
            ' "min_removal_confidence": 0.85, "min_literal_match_confidence": 0.7} -> 202, '
            "same job shape, result is the annotated changed-literals report."
        ),
    }


def main() -> None:
    import uvicorn

    # Bound to a Unix domain socket, not a TCP port -- see config.API_SOCKET_PATH's
    # docstring. uvicorn's own uds bind doesn't clear a stale socket file left behind
    # by a prior process (unlike pool.py's per-revision spawn, which does this itself),
    # so this must too, or a restart fails to bind with "address already in use".
    config.API_SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.API_SOCKET_PATH.unlink(missing_ok=True)
    uvicorn.run(app, uds=str(config.API_SOCKET_PATH))


if __name__ == "__main__":
    main()
