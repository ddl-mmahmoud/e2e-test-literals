"""A plain HTTP client for changed-literals' public API (`POST /changed-literals/jobs`,
`GET /changed-literals/jobs/{id}`, `GET /changed-literals/jobs/{id}/result`), reached
over its own Unix domain socket (`config.CHANGED_LITERALS_SOCKET_PATH`) rather than a
TCP port -- same convention as every other inter-process call in this codebase
(`service_ui/client.py`, `pool.py`, `proxy.py`): changed-literals is now a sibling
process within this same repo/Domino app (see repo-root `app.sh`), not a genuinely
separate Domino App reached over the real network, so there's no app-proxy gateway or
SSO login page in front of it to work around anymore -- a non-2xx response here is
always changed-literals' own.
"""

from __future__ import annotations

import httpx

from . import config

# Overridable by tests (`monkeypatch.setattr(changed_literals_client, "_transport",
# httpx.MockTransport(...))`) so request/response handling can be exercised against a
# fake server with no real socket needed -- `None` means "connect to
# config.CHANGED_LITERALS_SOCKET_PATH over a UDS transport", built fresh per call so a
# test's `monkeypatch.setattr(config, "CHANGED_LITERALS_SOCKET_PATH", ...)` is honored.
_transport: httpx.BaseTransport | None = None


class ChangedLiteralsError(RuntimeError):
    """Either the HTTP call to changed-literals returned a non-2xx response, or its
    own job reached status "error". `detail` carries changed-literals' own error
    message/detail so it can be surfaced to our caller unchanged."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _request(method: str, path: str, **kwargs) -> httpx.Response:
    # base_url's host is a placeholder -- the UDS transport is what actually routes
    # the connection, matching service_ui/client.py's and pool.py's own UDS client
    # pattern.
    transport = _transport if _transport is not None else httpx.HTTPTransport(uds=str(config.CHANGED_LITERALS_SOCKET_PATH))
    with httpx.Client(base_url="http://changed-literals", transport=transport, timeout=30.0) as http_client:
        resp = http_client.request(method, path, **kwargs)
    if resp.is_success:
        return resp
    try:
        detail = resp.json().get("detail", resp.text)
    except ValueError:
        detail = resp.text
    raise ChangedLiteralsError(str(detail))


def create_job(repo: str, base: str, updated: str) -> dict:
    return _request("POST", "/changed-literals/jobs", json={"repo": repo, "base": base, "updated": updated}).json()


def get_job_status(job_id: str) -> dict:
    return _request("GET", f"/changed-literals/jobs/{job_id}").json()


def get_result_page(job_id: str, *, offset: int = 0, limit: int = 2000) -> dict:
    return _request(
        "GET", f"/changed-literals/jobs/{job_id}/result", params={"offset": offset, "limit": limit}
    ).json()
