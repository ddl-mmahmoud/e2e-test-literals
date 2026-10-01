"""A plain HTTP client for e2e-test-literals-service's public API (`service/app.py`).

This is the *only* thing this UI shares with that service -- its documented wire
contract (`POST/GET /revisions`, `GET /revisions/jobs/{id}[/result]`), not any Python
code, config, or internal state. Nothing here imports from `..service`.
"""

from __future__ import annotations

import httpx

from . import config


class APIError(RuntimeError):
    """The upstream API returned a non-2xx response. `status_code`/`detail` are kept
    separate so callers can pass the same status code and message straight through."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _request(method: str, path: str, **kwargs) -> httpx.Response:
    with httpx.Client(base_url=config.API_BASE_URL, timeout=30.0) as http_client:
        resp = http_client.request(method, path, **kwargs)
    if resp.is_success:
        return resp
    try:
        detail = resp.json().get("detail", resp.text)
    except ValueError:
        detail = resp.text
    raise APIError(resp.status_code, str(detail))


def list_revisions() -> list[dict]:
    return _request("GET", "/revisions").json()["revisions"]


def create_revision(repo: str, ref: str) -> dict:
    return _request("POST", "/revisions", json={"repo": repo, "ref": ref}).json()


def get_job_status(job_id: str) -> dict:
    return _request("GET", f"/revisions/jobs/{job_id}").json()


def get_job_result(job_id: str) -> dict:
    return _request("GET", f"/revisions/jobs/{job_id}/result").json()
