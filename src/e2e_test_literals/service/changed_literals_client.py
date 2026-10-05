"""A plain HTTP client for changed-literals' public API (`POST /jobs`,
`GET /jobs/{id}`, `GET /jobs/{id}/result`), confirmed against its own code -- see
CHANGED-LITERALS-IMPACT-PLAN.md.

Unlike every other inter-process call in this codebase (`service_ui/client.py`,
`pool.py`, `proxy.py` -- all of which stay within one Domino app over a Unix domain
socket), changed-literals is a genuinely separate Domino App reached over the real
network. `changed-literals` itself has no auth of its own -- but Domino's app-proxy
gateway in front of it does: an unauthenticated request never reaches the app at all,
it gets 302-redirected to `/secured?...` (an interactive SSO login page), which shows
up here as a non-JSON, non-2xx response. So every call here must carry whatever
`Authorization` header the original caller of *our* `/changed-literals-impact` sent us
-- see `auth_header` on each function below, threaded through from `app.py`.
"""

from __future__ import annotations

import httpx

from . import config

# Overridable by tests (`monkeypatch.setattr(changed_literals_client, "_transport",
# httpx.MockTransport(...))`) so request/response handling can be exercised against a
# fake server with no real network and no new test dependency -- `None` means "use
# httpx's own default transport" (the real network).
_transport: httpx.BaseTransport | None = None


class ChangedLiteralsError(RuntimeError):
    """Either the HTTP call to changed-literals returned a non-2xx response, or its
    own job reached status "error". `detail` carries changed-literals' own error
    message/detail so it can be surfaced to our caller unchanged."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _request(method: str, path: str, *, auth_header: str | None = None, **kwargs) -> httpx.Response:
    if not config.CHANGED_LITERALS_URL:
        raise ChangedLiteralsError(
            "E2E_TEST_LITERALS_SERVICE_CHANGED_LITERALS_URL is not configured; cannot reach changed-literals"
        )
    if auth_header:
        kwargs.setdefault("headers", {})["Authorization"] = auth_header
    with httpx.Client(base_url=config.CHANGED_LITERALS_URL, transport=_transport, timeout=30.0) as http_client:
        resp = http_client.request(method, path, **kwargs)
    print(f"changed-literals: {method} {resp.request.url} -> {resp.status_code}")
    if resp.is_success:
        return resp
    if resp.is_redirect:
        # Domino's app-proxy gateway, not changed-literals itself -- bouncing an
        # unauthenticated (or wrongly-authenticated) request to its SSO login page
        # rather than ever reaching the app. A missing/stale `auth_header` is the only
        # known cause; surface that directly instead of dumping the redirect's raw
        # nginx HTML body on the caller.
        print(f"changed-literals: redirected to {resp.headers.get('location', '<unknown>')!r}")
        raise ChangedLiteralsError(
            f"changed-literals request was redirected ({resp.status_code}) to "
            f"{resp.headers.get('location', '<unknown>')!r} instead of reaching the app -- "
            "likely a missing or expired Authorization header on the call to changed-literals"
        )
    try:
        detail = resp.json().get("detail", resp.text)
    except ValueError:
        detail = resp.text
    print(f"changed-literals: non-2xx body (truncated): {resp.text[:500]!r}")
    raise ChangedLiteralsError(str(detail))


def create_job(repo: str, base: str, updated: str, *, auth_header: str | None = None) -> dict:
    return _request(
        "POST", "/jobs", json={"repo": repo, "base": base, "updated": updated}, auth_header=auth_header
    ).json()


def get_job_status(job_id: str, *, auth_header: str | None = None) -> dict:
    return _request("GET", f"/jobs/{job_id}", auth_header=auth_header).json()


def get_result_page(job_id: str, *, offset: int = 0, limit: int = 2000, auth_header: str | None = None) -> dict:
    return _request(
        "GET", f"/jobs/{job_id}/result", params={"offset": offset, "limit": limit}, auth_header=auth_header
    ).json()
