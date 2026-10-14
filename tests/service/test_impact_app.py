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
from e2e_test_literals.service import config, impact, impact_jobs
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
    assert status["report_url"] == f"/changed-literals-impact/jobs/{job_id}/result?report=1"

    result = impact_client.get(f"/changed-literals-impact/jobs/{job_id}/result").json()
    assert result == fake_result


def _create_done_job(client: TestClient, monkeypatch, **headers) -> dict:
    monkeypatch.setattr(impact_jobs, "compute_impact", lambda *a, **k: {"total": 0, "findings": []})
    job_id = client.post("/changed-literals-impact", json=_REQUEST_BODY, headers=headers).json()["job_id"]
    _await_job(client, job_id)
    return client.get(f"/changed-literals-impact/jobs/{job_id}", headers=headers).json()


def test_job_urls_are_absolute_when_the_ui_forwards_the_original_host(impact_client: TestClient, monkeypatch):
    status = _create_done_job(
        impact_client, monkeypatch, **{"X-Forwarded-Host": "domino.example.com", "X-Forwarded-Proto": "https"}
    )
    base = f"https://domino.example.com/changed-literals-impact/jobs/{status['job_id']}"
    assert status["status_url"] == base
    assert status["result_url"] == f"{base}/result"
    assert status["report_url"] == f"{base}/result?report=1"


def test_job_urls_use_the_external_prefix_not_the_internal_one(impact_client: TestClient, monkeypatch):
    # The real deployment: this process unprefixed internally, but reached under the
    # Domino host path (see config.py's EXTERNAL_PREFIX).
    monkeypatch.setattr(config, "EXTERNAL_PREFIX", "/ext/app/")
    status = _create_done_job(impact_client, monkeypatch, **{"X-Forwarded-Host": "domino.example.com"})
    assert status["result_url"] == f"https://domino.example.com/ext/app/changed-literals-impact/jobs/{status['job_id']}/result"


def test_configured_external_origin_wins_over_forwarded_headers(impact_client: TestClient, monkeypatch):
    monkeypatch.setattr(config, "EXTERNAL_ORIGIN", "https://configured.example.com")
    status = _create_done_job(impact_client, monkeypatch, **{"X-Forwarded-Host": "other.example.com"})
    assert status["result_url"].startswith("https://configured.example.com/changed-literals-impact/jobs/")


def test_forwarded_header_lists_use_their_first_value(impact_client: TestClient, monkeypatch):
    status = _create_done_job(
        impact_client, monkeypatch, **{"X-Forwarded-Host": "a.example.com, proxy.internal", "X-Forwarded-Proto": "http, https"}
    )
    assert status["result_url"].startswith("http://a.example.com/changed-literals-impact/jobs/")


def test_default_min_removal_confidence_is_applied(impact_client: TestClient, monkeypatch):
    captured_args = []
    monkeypatch.setattr(
        impact_jobs, "compute_impact", lambda *a, **k: captured_args.append(a) or {"total": 0, "findings": []}
    )

    create_resp = impact_client.post("/changed-literals-impact", json=_REQUEST_BODY)
    _await_job(impact_client, create_resp.json()["job_id"])

    (args,) = captured_args
    assert args[-6] == impact.DEFAULT_MIN_REMOVAL_CONFIDENCE
    assert args[-5] == impact.DEFAULT_MIN_LITERAL_MATCH_CONFIDENCE
    assert args[-4] == impact.DEFAULT_MIN_INVOCATION_CONFIDENCE == 0.0
    assert args[-3] == impact.DEFAULT_INVOCATION_MAX_LENGTH == 300
    # Omitted means "no tag coalescing", not zero: both stay None all the way down.
    assert args[-2] is None
    assert args[-1] is None


def test_tag_allowances_are_passed_through_when_given(impact_client: TestClient, monkeypatch):
    captured_args = []
    monkeypatch.setattr(
        impact_jobs, "compute_impact", lambda *a, **k: captured_args.append(a) or {"total": 0, "findings": []}
    )

    body = {**_REQUEST_BODY, "extra_cases_allowed": 3}
    create_resp = impact_client.post("/changed-literals-impact", json=body)
    _await_job(impact_client, create_resp.json()["job_id"])

    (args,) = captured_args
    assert args[-2] == 3
    assert args[-1] is None  # only one given: the other is left unbounded downstream, still not defaulted here


@pytest.mark.parametrize("field", ["extra_cases_allowed", "missing_cases_allowed"])
def test_negative_tag_allowance_is_422(impact_client: TestClient, field):
    resp = impact_client.post("/changed-literals-impact", json={**_REQUEST_BODY, field: -1})
    assert resp.status_code == 422


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


@pytest.mark.parametrize(
    "overrides",
    [
        {"base_ref": None, "updated_ref": None},
        {"base_ref": None},
        {"pr_number": 3},  # alongside base_ref/updated_ref
        {"base_ref": None, "updated_ref": None, "pr_number": 0},
    ],
)
def test_impact_request_requires_exactly_one_way_to_name_the_diff(impact_client: TestClient, overrides):
    body = {**_REQUEST_BODY, **overrides}
    body = {k: v for k, v in body.items() if v is not None}
    assert impact_client.post("/changed-literals-impact", json=body).status_code == 422


def test_impact_request_accepts_pr_number_in_place_of_refs(impact_client: TestClient, monkeypatch):
    seen = {}

    def fake_compute(*a, **k):
        seen["args"], seen["kwargs"] = a, k
        return {"total": 0, "findings": []}

    monkeypatch.setattr(impact_jobs, "compute_impact", fake_compute)
    body = {k: v for k, v in _REQUEST_BODY.items() if k not in ("base_ref", "updated_ref")}

    resp = impact_client.post("/changed-literals-impact", json={**body, "pr_number": 3})

    assert resp.status_code == 202
    assert _await_job(impact_client, resp.json()["job_id"])["status"] == "done"
    assert seen["args"][3:5] == (None, None)
    assert seen["kwargs"]["pr_number"] == 3


def _report_result() -> dict:
    literal = {
        "value": "Access",
        "string_kind": "link",
        "case_id": 771386,
        "scenario_name": "Check links",
        "source_file": "tests/a.feature",
        "source_line": 63,
        "step_text": 'I click the link "Access"',
        "tags": ["testrail(771386)"],
        "match_algorithm": "exact",
        "match_confidence": 1.0,
    }
    finding = {
        "file": "views/</script>.html",
        "line": 45,
        "column": 3,
        "text": "Access",
        "context": "html-text",
        "confidence": 0.9,
        "change": "removed",
        "hunk": 0,
        "matched_test_literals": [literal],
        "replacements": [],
    }
    return {
        "test_repo": "https://github.com/o/tests",
        "test_sha": "a" * 40,
        "literals_repo": "https://github.com/o/product",
        "base_ref": "b" * 40,
        "updated_ref": "c" * 40,
        "pr_number": None,
        "min_invocation_confidence": 0.0,
        "invocation_max_length": 300,
        "extra_cases_allowed": None,
        "missing_cases_allowed": None,
        "total": 1,
        "findings": [finding],
        "recommended_invocations": {"commands": [], "structured": [], "uncovered": [], "warnings": []},
    }


def _finished_job(client: TestClient, monkeypatch, result: dict) -> str:
    monkeypatch.setattr(impact_jobs, "compute_impact", lambda *a, **k: result)
    job_id = client.post("/changed-literals-impact", json=_REQUEST_BODY).json()["job_id"]
    assert _await_job(client, job_id)["status"] == "done"
    return job_id


def test_report_param_serves_the_html_report_and_plain_result_stays_json(impact_client: TestClient, monkeypatch):
    result = _report_result()
    job_id = _finished_job(impact_client, monkeypatch, result)

    report = impact_client.get(f"/changed-literals-impact/jobs/{job_id}/result?report=1")
    plain = impact_client.get(f"/changed-literals-impact/jobs/{job_id}/result")

    assert report.status_code == 200
    assert report.headers["content-type"].startswith("text/html")
    assert "report-data" in report.text
    # The file name that tried to close the script element is embedded escaped.
    assert "</script>.html" not in report.text
    assert plain.json() == result

    # report=0 is the JSON again.
    assert impact_client.get(f"/changed-literals-impact/jobs/{job_id}/result?report=0").json() == result


def test_report_is_409_while_the_job_is_pending(impact_client: TestClient, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(impact_jobs, "compute_impact", lambda *a, **k: release.wait(5) and {"total": 0, "findings": []})
    job_id = impact_client.post("/changed-literals-impact", json=_REQUEST_BODY).json()["job_id"]
    try:
        assert impact_client.get(f"/changed-literals-impact/jobs/{job_id}/result?report=1").status_code == 409
    finally:
        release.set()
        _await_job(impact_client, job_id)


@pytest.fixture
def routed_db(tmp_path, monkeypatch):
    from e2e_test_literals.db import write_sqlite
    from e2e_test_literals.service import config

    monkeypatch.setattr(config, "DB_DIR", tmp_path / "db")
    config.DB_DIR.mkdir(parents=True)

    def entry(case_id: int) -> dict:
        return {
            "case_id": case_id,
            "domain": "ui",
            "file": "tests/ui/features/example.feature",
            "name": f"Case {case_id}",
            "root": "domino",
            "labels": [["tag", "smoke"]],
            "candidates": [{"test_type": "cucu", "flags": []}],
        }

    cases = {
        case_id: {
            "scenarios": [{"feature_file": "tests/ui/features/example.feature", "scenario_name": f"Case {case_id}", "tags": ["smoke"]}],
            "literals": [],
            "patterns": [],
        }
        for case_id in (1, 2)
    }
    write_sqlite(cases, config.DB_DIR / "deadbeef.sqlite", routing=[entry(1), entry(2)])


def test_invocations_are_recomputed_for_just_the_given_cases(impact_client: TestClient, routed_db):
    resp = impact_client.post(
        "/changed-literals-impact/invocations", json={"test_sha": "deadbeef", "case_ids": [2], "invocation_max_length": 300}
    )

    assert resp.status_code == 200
    assert resp.json()["commands"] == ["/test-e2e cucu --tags=testrail(2)"]


def test_invocations_for_no_cases_are_empty(impact_client: TestClient, routed_db):
    resp = impact_client.post("/changed-literals-impact/invocations", json={"test_sha": "deadbeef", "case_ids": []})

    assert resp.json() == {"commands": [], "structured": [], "uncovered": [], "warnings": []}


def test_invocations_honor_tag_allowances(impact_client: TestClient, routed_db):
    resp = impact_client.post(
        "/changed-literals-impact/invocations",
        json={"test_sha": "deadbeef", "case_ids": [1, 2], "extra_cases_allowed": 0},
    )

    assert resp.json()["commands"] == ["/test-e2e cucu --tags=smoke"]


def test_invocations_404_for_a_revision_that_is_not_built(impact_client: TestClient, routed_db):
    resp = impact_client.post("/changed-literals-impact/invocations", json={"test_sha": "abc1234", "case_ids": [1]})

    assert resp.status_code == 404
    assert "run the impact check again" in resp.json()["detail"]


@pytest.mark.parametrize("test_sha", ["../../etc/passwd", "DEADBEEF", "", "dead beef"])
def test_invocations_reject_a_test_sha_that_is_not_a_plain_hex_sha(impact_client: TestClient, test_sha):
    resp = impact_client.post("/changed-literals-impact/invocations", json={"test_sha": test_sha, "case_ids": [1]})

    assert resp.status_code == 422


def test_status_reports_phase_and_profiling_while_running_and_when_done(impact_client: TestClient, monkeypatch):
    release = threading.Event()
    entered = threading.Event()

    def fake_compute(*a, tracker, **k):
        with tracker.phase("populating_test_db") as phase:
            phase.caches["test_db"] = "hit"
        with tracker.phase("matching_literals"):
            entered.set()
            release.wait(5)
        return {"total": 0, "findings": []}

    monkeypatch.setattr(impact_jobs, "compute_impact", fake_compute)
    create_body = impact_client.post("/changed-literals-impact", json=_REQUEST_BODY).json()
    job_id = create_body["job_id"]
    assert create_body["phase"] == "queued"
    assert set(create_body["profiling"]["phases"].values()) == {None}

    assert entered.wait(5)
    running = impact_client.get(f"/changed-literals-impact/jobs/{job_id}").json()
    release.set()
    done = _await_job(impact_client, job_id)

    assert running["status"] == "running"
    assert running["phase"] == "matching_literals"
    assert running["profiling"]["phases"]["populating_test_db"]["caches"] == {"test_db": "hit"}
    assert running["profiling"]["phases"]["matching_literals"]["state"] == "running"
    assert running["profiling"]["phases"]["computing_invocations"] is None
    assert done["phase"] == "done"
    assert done["profiling"]["phases"]["matching_literals"]["state"] == "done"
    assert done["profiling"]["phases"]["computing_invocations"] is None  # this fake never got that far
    # Final figures: the total no longer grows once the job is done.
    assert impact_client.get(f"/changed-literals-impact/jobs/{job_id}").json()["profiling"]["total_seconds"] == (
        done["profiling"]["total_seconds"]
    )


def test_status_marks_the_phase_an_error_job_failed_in(impact_client: TestClient, monkeypatch):
    def fake_compute(*a, tracker, **k):
        with tracker.phase("populating_test_db"):
            raise ChangedLiteralsError("boom")

    monkeypatch.setattr(impact_jobs, "compute_impact", fake_compute)
    job_id = impact_client.post("/changed-literals-impact", json=_REQUEST_BODY).json()["job_id"]

    status = _await_job(impact_client, job_id)

    assert status["status"] == "error"
    assert status["phase"] == "error"
    assert status["profiling"]["phases"]["populating_test_db"]["state"] == "failed"
