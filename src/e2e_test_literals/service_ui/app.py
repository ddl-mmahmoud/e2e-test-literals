#!/usr/bin/env python3
"""Browser-facing management UI for e2e-test-literals-service: lists revisions already
built and links to their Datasette landing pages, plus a form to kick off building a
new one.

A deliberately separate app from `service/app.py` -- it knows nothing about
e2e_test_literals' generation pipeline, job threading, or Datasette subprocess pool.
Everything it needs from that service it gets by calling its public HTTP API
(`client.py`), the same way any other caller of that API would; the two apps share no
Python state and run as entirely separate processes (see `main()` below,
`pyproject.toml`'s separate console script, and repo-root `app.sh`, which runs both --
the upstream API over a Unix domain socket this app alone talks to).

Routes:
  GET  /                                  the HTML page (page.py, static)
  GET  /api/revisions                     -> {"revisions": [...]}, passthrough from the
                                              upstream API
  POST /api/revisions   body {repo, ref}  -> {"job_id", "status", "status_url",
                                              "result_url"}, with status_url/result_url
                                              pointing back at this app's own routes
                                              below (not the upstream service's)
  GET  /api/revisions/jobs/{job_id}        upstream job status, passthrough
  GET  /api/revisions/jobs/{job_id}/result upstream result, passthrough

  <anything else>                         reverse-proxied unchanged to the upstream API
                                              (see proxy.py) -- chiefly each revision's
                                              Datasette pages, whatever path shape the
                                              upstream's `datasette_url` happens to use
                                              (`/data/<sha>/...` today). This is what
                                              lets a `datasette_url` from the two
                                              endpoints above stay a plain same-origin
                                              path for the browser to open, even though
                                              the upstream process isn't itself exposed
                                              in this deployment.

Environment variables: see config.py.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import client, config
from .page import PAGE_HTML
from .proxy import proxy_to_api

app = FastAPI(title="e2e-test-literals-service-ui")


class RevisionRequest(BaseModel):
    repo: str
    ref: str = "main"


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except client.APIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def index() -> HTMLResponse:
    return HTMLResponse(PAGE_HTML)


def api_list_revisions() -> dict:
    return {"revisions": _call(client.list_revisions)}


def api_create_revision(payload: RevisionRequest) -> dict:
    upstream = _call(client.create_revision, payload.repo, payload.ref)
    job_id = upstream["job_id"]
    return {
        "job_id": job_id,
        "status": upstream["status"],
        "status_url": f"{config.PREFIX}api/revisions/jobs/{job_id}",
        "result_url": f"{config.PREFIX}api/revisions/jobs/{job_id}/result",
    }


def api_job_status(job_id: str) -> dict:
    upstream = _call(client.get_job_status, job_id)
    body = {"job_id": upstream["job_id"], "status": upstream["status"]}
    if upstream["status"] == "error":
        body["error"] = upstream.get("error")
    if upstream["status"] == "done":
        body["result_url"] = f"{config.PREFIX}api/revisions/jobs/{job_id}/result"
    return body


def api_job_result(job_id: str) -> dict:
    return _call(client.get_job_result, job_id)


async def proxy_passthrough(path: str, request: Request):
    """Anything not matched by a route above is forwarded unchanged to the upstream
    API -- `path` already has this app's own routing prefix stripped by Starlette's
    path converter, so it's exactly the path the upstream API itself would recognize
    (see proxy.py)."""
    return await proxy_to_api(request, f"/{path}")


_PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

# Specific routes are registered first (both bare and, if configured, PREFIX-prefixed)
# so they're matched before the generic catch-all(s) below -- Starlette matches route
# patterns in registration order, and `/{path:path}` matches literally anything.
app.add_api_route("/", index, methods=["GET"], response_class=HTMLResponse)
app.add_api_route("/api/revisions", api_list_revisions, methods=["GET"])
app.add_api_route("/api/revisions", api_create_revision, methods=["POST"], status_code=202)
app.add_api_route("/api/revisions/jobs/{job_id}", api_job_status, methods=["GET"])
app.add_api_route("/api/revisions/jobs/{job_id}/result", api_job_result, methods=["GET"])

if config.PREFIX != "/":
    app.add_api_route(config.PREFIX, index, methods=["GET"], response_class=HTMLResponse)
    app.add_api_route(config.PREFIX + "api/revisions", api_list_revisions, methods=["GET"])
    app.add_api_route(config.PREFIX + "api/revisions", api_create_revision, methods=["POST"], status_code=202)
    app.add_api_route(config.PREFIX + "api/revisions/jobs/{job_id}", api_job_status, methods=["GET"])
    app.add_api_route(config.PREFIX + "api/revisions/jobs/{job_id}/result", api_job_result, methods=["GET"])
    # Registered before the bare catch-all below: a prefixed request must be matched
    # here (where Starlette's path converter strips the literal PREFIX before handing
    # `path` to proxy_passthrough) rather than falling through to the bare pattern,
    # which would forward the PREFIX segment upstream verbatim and 404.
    app.add_api_route(config.PREFIX + "{path:path}", proxy_passthrough, methods=_PROXY_METHODS)

app.add_api_route("/{path:path}", proxy_passthrough, methods=_PROXY_METHODS)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8890)


if __name__ == "__main__":
    main()
