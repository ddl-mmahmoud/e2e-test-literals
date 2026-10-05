"""End-to-end tests for the /changed-literals-impact routes -- the orchestration logic
itself (impact.compute_impact) is unit-tested in test_impact.py; these only exercise
the job-management/HTTP wiring, same split test_app.py/test_jobs.py use for
/revisions.

`compute_impact` is monkeypatched on `impact_jobs` (not `impact`) -- impact_jobs.py
imports the name directly (`from .impact import compute_impact`), so that's the
reference it actually calls, same gotcha jobs.py's own tests patch around."""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from e2e_test_literals.service import app as app_module
from e2e_test_literals.service import impact, impact_jobs
from e2e_test_literals.service.changed_literals_client import ChangedLiteralsError

_REQUEST_BODY = {
    "test_repo": "test-repo",
    "test_ref": "main",
    "literals_repo": "product-repo",
    "base_ref": "base",
    "updated_ref": "updated",
}


@pytest.fixture
def impact_client():
    with impact_jobs._jobs_lock:
        impact_jobs._jobs.clear()
    with TestClient(app_module.app) as client:
        yield client
    with impact_jobs._jobs_lock:
        impact_jobs._jobs.clear()


def _await_job(client: TestClient, job_id: str, *, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/changed-literals-impact/jobs/{job_id}").json()
        if body["status"] in ("done", "error"):
            return body
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


def test_impact_job_status_404_for_unknown_job(impact_client: TestClient):
    resp = impact_client.get("/changed-literals-impact/jobs/does-not-exist")
    assert resp.status_code == 404


def test_full_flow_returns_computed_result(impact_client: TestClient, monkeypatch):
    fake_result = {"total": 0, "findings": []}
    monkeypatch.setattr(impact_jobs, "compute_impact", lambda *a, **k: fake_result)

    create_resp = impact_client.post("/changed-literals-impact", json=_REQUEST_BODY)
    assert create_resp.status_code == 202
    body = create_resp.json()
    assert body["status"] == "pending"
    job_id = body["job_id"]
    assert create_resp.headers["location"] == f"/changed-literals-impact/jobs/{job_id}"

    status = _await_job(impact_client, job_id)
    assert status["status"] == "done"
    assert status["result_url"] == f"/changed-literals-impact/jobs/{job_id}/result"

    result = impact_client.get(f"/changed-literals-impact/jobs/{job_id}/result").json()
    assert result == fake_result


def test_default_min_removal_confidence_is_applied(impact_client: TestClient, monkeypatch):
    captured_args = []
    monkeypatch.setattr(
        impact_jobs, "compute_impact", lambda *a, **k: captured_args.append(a) or {"total": 0, "findings": []}
    )

    create_resp = impact_client.post("/changed-literals-impact", json=_REQUEST_BODY)
    _await_job(impact_client, create_resp.json()["job_id"])

    (args,) = captured_args
    assert args[-2] == impact.DEFAULT_MIN_REMOVAL_CONFIDENCE
    assert args[-1] is None


def test_callers_authorization_header_is_forwarded_to_compute_impact(impact_client: TestClient, monkeypatch):
    captured_args = []
    monkeypatch.setattr(
        impact_jobs, "compute_impact", lambda *a, **k: captured_args.append(a) or {"total": 0, "findings": []}
    )

    create_resp = impact_client.post(
        "/changed-literals-impact", json=_REQUEST_BODY, headers={"Authorization": "Bearer caller-token"}
    )
    _await_job(impact_client, create_resp.json()["job_id"])

    (args,) = captured_args
    assert args[-1] == "Bearer caller-token"


def test_result_409_while_pending(impact_client: TestClient, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(
        impact_jobs, "compute_impact", lambda *a, **k: release.wait(5) and {"total": 0, "findings": []}
    )

    create_resp = impact_client.post("/changed-literals-impact", json=_REQUEST_BODY)
    job_id = create_resp.json()["job_id"]
    try:
        resp = impact_client.get(f"/changed-literals-impact/jobs/{job_id}/result")
        assert resp.status_code == 409
    finally:
        release.set()
        _await_job(impact_client, job_id)


def test_result_500_on_error(impact_client: TestClient, monkeypatch):
    def _boom(*a, **k):
        raise ChangedLiteralsError("boom")

    monkeypatch.setattr(impact_jobs, "compute_impact", _boom)

    create_resp = impact_client.post("/changed-literals-impact", json=_REQUEST_BODY)
    job_id = create_resp.json()["job_id"]
    status = _await_job(impact_client, job_id)
    assert status["status"] == "error"

    resp = impact_client.get(f"/changed-literals-impact/jobs/{job_id}/result")
    assert resp.status_code == 500
    assert resp.json()["detail"] == "boom"


def test_missing_required_field_is_422(impact_client: TestClient):
    bad_body = dict(_REQUEST_BODY)
    del bad_body["base_ref"]
    resp = impact_client.post("/changed-literals-impact", json=bad_body)
    assert resp.status_code == 422
