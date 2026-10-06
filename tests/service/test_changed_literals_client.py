"""Unit tests for changed_literals_client.py's request/response plumbing, against
`httpx.MockTransport` (ships with httpx -- no new test dependency, no real
changed-literals process needed; see CHANGED-LITERALS-IMPACT-PLAN.md Q7)."""

from __future__ import annotations

import json

import httpx
import pytest

from e2e_test_literals.service import changed_literals_client as cl_client


@pytest.fixture(autouse=True)
def _reset_transport(monkeypatch):
    monkeypatch.setattr(cl_client, "_transport", None)
    yield


def test_create_job_posts_the_documented_body(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = request.read()
        return httpx.Response(202, json={"job_id": "abc", "status": "pending"})

    monkeypatch.setattr(cl_client, "_transport", httpx.MockTransport(handler))

    result = cl_client.create_job("repo-url", "base-ref", "updated-ref")

    assert captured["method"] == "POST"
    assert captured["path"] == "/changed-literals/jobs"
    assert json.loads(captured["body"]) == {"repo": "repo-url", "base": "base-ref", "updated": "updated-ref"}
    assert result == {"job_id": "abc", "status": "pending"}


def test_get_job_status_returns_the_body(monkeypatch):
    monkeypatch.setattr(
        cl_client, "_transport", httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "running"}))
    )
    assert cl_client.get_job_status("abc") == {"status": "running"}


def test_get_result_page_passes_offset_and_limit(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"offset": 400, "limit": 200, "total": 1, "findings": []})

    monkeypatch.setattr(cl_client, "_transport", httpx.MockTransport(handler))

    cl_client.get_result_page("abc", offset=400, limit=200)

    assert captured["path"] == "/changed-literals/jobs/abc/result"
    assert captured["params"] == {"offset": "400", "limit": "200"}


def test_non_2xx_response_raises_changed_literals_error_with_detail(monkeypatch):
    monkeypatch.setattr(
        cl_client,
        "_transport",
        httpx.MockTransport(lambda request: httpx.Response(409, json={"detail": "still pending"})),
    )
    with pytest.raises(cl_client.ChangedLiteralsError, match="still pending"):
        cl_client.get_job_status("abc")


def test_non_json_error_body_falls_back_to_raw_text(monkeypatch):
    monkeypatch.setattr(
        cl_client,
        "_transport",
        httpx.MockTransport(lambda request: httpx.Response(500, text="boom")),
    )
    with pytest.raises(cl_client.ChangedLiteralsError, match="boom"):
        cl_client.get_job_status("abc")
