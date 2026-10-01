"""Tests for the UI's own FastAPI app, exercised through its HTTP surface the way a
browser would. `client.py`'s calls to the upstream e2e-test-literals-service are
monkeypatched here -- this suite is about the UI app's own routing/rewriting/error
mapping, not the upstream service (which has its own tests under tests/service/)."""

from __future__ import annotations

import socketserver
import tempfile
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from e2e_test_literals.service_ui import app as app_module
from e2e_test_literals.service_ui import client, config


@pytest.fixture
def ui_client(monkeypatch):
    # Never actually dialed in most of these tests (client.py's own functions are
    # monkeypatched instead) -- only test_unmatched_path_is_proxied_to_the_upstream_api
    # below points this at a real socket.
    monkeypatch.setattr(config, "API_SOCKET_PATH", Path("/nonexistent/api.sock"))
    with TestClient(app_module.app) as test_client:
        yield test_client


def test_index_serves_html_page(ui_client: TestClient):
    resp = ui_client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "e2e-test-literals revisions" in resp.text
    # Fetch paths must be relative (no leading slash) so the same page works whether
    # mounted at "/" or behind a DOMINO_RUN_HOST_PATH prefix -- see page.py.
    assert '"/api/revisions"' not in resp.text
    assert '"api/revisions"' in resp.text


def test_list_revisions_passthrough(ui_client: TestClient, monkeypatch):
    upstream_revisions = [
        {"sha": "deadbeef", "datasette_url": "/data/deadbeef", "built_at": "2026-01-01T00:00:00+00:00", "running": True}
    ]
    monkeypatch.setattr(client, "list_revisions", lambda: upstream_revisions)
    resp = ui_client.get("/api/revisions")
    assert resp.status_code == 200
    # datasette_url stays a plain same-origin path -- the browser reaches it through
    # this app's own catch-all proxy (proxy_passthrough), not directly against
    # API_SOCKET_PATH (which isn't even a TCP endpoint, see app.sh). Unchanged here
    # because config.PREFIX is "/" by default in tests -- see
    # test_list_revisions_rebases_datasette_url_under_this_app_prefix below for the
    # non-default-PREFIX case.
    assert resp.json()["revisions"] == upstream_revisions


def test_list_revisions_rebases_datasette_url_under_this_app_prefix(ui_client: TestClient, monkeypatch):
    # The upstream service always reports datasette_url rooted at *its own* PREFIX
    # (always "/" in production, see app.sh) -- this app must rebase it under its own
    # PREFIX instead, same as status_url/result_url elsewhere in this file, since the
    # browser reaches Datasette pages through this app's own origin.
    monkeypatch.setattr(config, "PREFIX", "/e2e-test-literals/")
    upstream_revisions = [
        {"sha": "deadbeef", "datasette_url": "/data/deadbeef", "built_at": "2026-01-01T00:00:00+00:00", "running": True}
    ]
    monkeypatch.setattr(client, "list_revisions", lambda: upstream_revisions)
    resp = ui_client.get("/api/revisions")
    assert resp.status_code == 200
    assert resp.json()["revisions"][0]["datasette_url"] == "/e2e-test-literals/data/deadbeef"


def test_create_revision_points_status_and_result_url_at_this_app(ui_client: TestClient, monkeypatch):
    monkeypatch.setattr(
        client,
        "create_revision",
        lambda repo, ref: {"job_id": "abc123", "status": "pending", "status_url": "/revisions/jobs/abc123"},
    )
    resp = ui_client.post("/api/revisions", json={"repo": "some-repo", "ref": "main"})
    assert resp.status_code == 202
    assert resp.json() == {
        "job_id": "abc123",
        "status": "pending",
        "status_url": "/api/revisions/jobs/abc123",
        "result_url": "/api/revisions/jobs/abc123/result",
    }


def test_job_status_passthrough(ui_client: TestClient, monkeypatch):
    monkeypatch.setattr(
        client,
        "get_job_status",
        lambda job_id: {"job_id": job_id, "status": "done", "status_url": "/revisions/jobs/abc123"},
    )
    resp = ui_client.get("/api/revisions/jobs/abc123")
    assert resp.status_code == 200
    assert resp.json() == {
        "job_id": "abc123",
        "status": "done",
        "result_url": "/api/revisions/jobs/abc123/result",
    }


def test_job_result_passthrough(ui_client: TestClient, monkeypatch):
    monkeypatch.setattr(
        client,
        "get_job_result",
        lambda job_id: {"sha": "deadbeef", "datasette_url": "/data/deadbeef"},
    )
    resp = ui_client.get("/api/revisions/jobs/abc123/result")
    assert resp.status_code == 200
    # Unchanged here because config.PREFIX is "/" by default in tests -- see
    # test_job_result_rebases_datasette_url_under_this_app_prefix below.
    assert resp.json() == {"sha": "deadbeef", "datasette_url": "/data/deadbeef"}


def test_job_result_rebases_datasette_url_under_this_app_prefix(ui_client: TestClient, monkeypatch):
    monkeypatch.setattr(config, "PREFIX", "/e2e-test-literals/")
    monkeypatch.setattr(
        client,
        "get_job_result",
        lambda job_id: {"sha": "deadbeef", "datasette_url": "/data/deadbeef"},
    )
    resp = ui_client.get("/api/revisions/jobs/abc123/result")
    assert resp.status_code == 200
    assert resp.json() == {"sha": "deadbeef", "datasette_url": "/e2e-test-literals/data/deadbeef"}


def test_upstream_error_passed_through(ui_client: TestClient, monkeypatch):
    def _raise(job_id):
        raise client.APIError(409, f"job {job_id} is running; poll again")

    monkeypatch.setattr(client, "get_job_result", _raise)
    resp = ui_client.get("/api/revisions/jobs/abc123/result")
    assert resp.status_code == 409
    assert "poll again" in resp.json()["detail"]


def test_unknown_job_404_passed_through(ui_client: TestClient, monkeypatch):
    def _raise(job_id):
        raise client.APIError(404, f"no such job {job_id!r}")

    monkeypatch.setattr(client, "get_job_status", _raise)
    resp = ui_client.get("/api/revisions/jobs/does-not-exist")
    assert resp.status_code == 404


class _FakeUpstreamHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 -- fixed by BaseHTTPRequestHandler
        self.requested_path = self.path  # exposed for the test to assert on
        _FakeUpstreamHandler.last_requested_path = self.path
        body = b'{"tables": ["literals"]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence BaseHTTPRequestHandler's default logging
        pass


class _ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


@pytest.fixture
def fake_upstream():
    # A short path straight under the system temp dir, not pytest's own (often
    # long/nested) tmp_path -- Unix socket paths have a real ~108-byte length limit on
    # Linux (see DATASETTE-WRAPPER-PLAN.md's note on config.SOCKET_DIR, bitten by this
    # exact issue once already).
    socket_path = Path(tempfile.mktemp(suffix=".sock", prefix="e2e-test-literals-ui-"))
    server = _ThreadingUnixHTTPServer(str(socket_path), _FakeUpstreamHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield socket_path
    finally:
        server.shutdown()
        thread.join()
        socket_path.unlink(missing_ok=True)


def test_unmatched_path_is_proxied_to_the_upstream_api(ui_client: TestClient, monkeypatch, fake_upstream):
    monkeypatch.setattr(config, "API_SOCKET_PATH", fake_upstream)

    resp = ui_client.get("/data/deadbeef/literals.json?_sort=id")

    assert resp.status_code == 200
    assert resp.json() == {"tables": ["literals"]}
    # The UI's own routing prefix (none here, config.PREFIX == "/") must be stripped
    # before forwarding -- the request the fake upstream actually received should be
    # exactly the path+query the browser asked this UI for.
    assert _FakeUpstreamHandler.last_requested_path == "/data/deadbeef/literals.json?_sort=id"
