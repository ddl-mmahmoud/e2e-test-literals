"""A plain HTTP client for changed-literals' public API (`POST /jobs`,
`GET /jobs/{id}`, `GET /jobs/{id}/result`), confirmed against its own code -- see
CHANGED-LITERALS-IMPACT-PLAN.md.

Unlike every other inter-process call in this codebase (`service_ui/client.py`,
`pool.py`, `proxy.py` -- all of which stay within one Domino app over a Unix domain
socket), changed-literals is a genuinely separate Domino App reached over the real
network. There's no auth of its own to carry (confirmed: it's an unauthenticated
FastAPI app; reachability is whatever Domino's app-proxy access control provides).
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


def _request(method: str, path: str, **kwargs) -> httpx.Response:
    if not config.CHANGED_LITERALS_URL:
        raise ChangedLiteralsError(
            "E2E_TEST_LITERALS_SERVICE_CHANGED_LITERALS_URL is not configured; cannot reach changed-literals"
        )
    with httpx.Client(base_url=config.CHANGED_LITERALS_URL, transport=_transport, timeout=30.0) as http_client:
        resp = http_client.request(method, path, **kwargs)
    if resp.is_success:
        return resp
    try:
        detail = resp.json().get("detail", resp.text)
    except ValueError:
        detail = resp.text
    raise ChangedLiteralsError(str(detail))


def create_job(repo: str, base: str, updated: str) -> dict:
    return _request("POST", "/jobs", json={"repo": repo, "base": base, "updated": updated}).json()


def get_job_status(job_id: str) -> dict:
    return _request("GET", f"/jobs/{job_id}").json()


def get_result_page(job_id: str, *, offset: int = 0, limit: int = 2000) -> dict:
    return _request("GET", f"/jobs/{job_id}/result", params={"offset": offset, "limit": limit}).json()
