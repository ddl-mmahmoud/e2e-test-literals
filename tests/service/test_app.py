"""End-to-end tests for the FastAPI wrapper: real local git repo -> real async job ->
real generated sqlite db -> real `datasette serve` subprocess -> real proxied HTTP
query, exercised through `TestClient` exactly the way an actual caller would use it."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from e2e_test_literals.service import app as app_module
from e2e_test_literals.service import config, generation, jobs
from e2e_test_literals.service.pool import pool


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _make_test_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    steps_dir = repo / "tests" / "ui" / "features" / "steps"
    steps_dir.mkdir(parents=True)
    (steps_dir / "__init__.py").write_text("")
    (repo / "tests" / "common").mkdir(parents=True)
    (repo / "tests" / "common" / ".gitkeep").write_text("")
    (repo / "tests" / "helpers").mkdir(parents=True)
    (repo / "tests" / "helpers" / ".gitkeep").write_text("")
    (repo / "tests" / "ui" / "features" / "cucurc.yml").write_text("")

    (repo / "tests" / "ui" / "features" / "example.feature").write_text(
        "Feature: Example\n"
        "\n"
        "  @testrail(1234)\n"
        '  Scenario: Save a widget\n'
        '    Then I click the button "Saved successfully"\n'
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "base")
    return repo


@pytest.fixture
def service_env(tmp_path: Path, monkeypatch, bootstrapped_registry):
    monkeypatch.setattr(config, "DB_DIR", tmp_path / "db")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "REPO_CACHE_DIR", tmp_path / "repo-cache")
    # NOT under tmp_path -- see test_pool.py's pool_env fixture for why (Unix socket
    # path length limit, confirmed by hitting it while writing these tests).
    socket_dir = Path(tempfile.mkdtemp(prefix="e2e-svc-test-"))
    monkeypatch.setattr(config, "SOCKET_DIR", socket_dir)
    with jobs._jobs_lock:
        jobs._jobs.clear()
    with TestClient(app_module.app) as client:
        yield client
    pool.shutdown()
    shutil.rmtree(socket_dir, ignore_errors=True)
    with jobs._jobs_lock:
        jobs._jobs.clear()


def _await_job(client: TestClient, job_id: str, *, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/revisions/jobs/{job_id}").json()
        if body["status"] in ("done", "error"):
            return body
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


def test_index_returns_usage(service_env: TestClient):
    resp = service_env.get("/")
    assert resp.status_code == 200
    assert "revisions" in resp.json()["usage"]


@pytest.mark.parametrize(
    "path",
    [
        "favicon.ico",
        "robots.txt",
        "index.html",
        "index.htm",
        "apple-touch-icon.png",
        "apple-touch-icon-precomposed.png",
    ],
)
def test_well_known_paths_are_not_treated_as_a_revision_sha(service_env: TestClient, path: str):
    # Regression: browsers/crawlers request these unprompted, and they must not fall
    # through to the catch-all proxy routes and get treated as a revision sha.
    resp = service_env.get(f"/{path}")
    assert resp.status_code == 404


def test_revision_job_status_404_for_unknown_job(service_env: TestClient):
    resp = service_env.get("/revisions/jobs/does-not-exist")
    assert resp.status_code == 404


def test_revision_job_result_409_while_pending(service_env: TestClient, monkeypatch):
    import threading

    release = threading.Event()
    monkeypatch.setattr(jobs, "resolve_and_generate", lambda *a, **k: release.wait(5) and "deadbeef")

    resp = service_env.post("/revisions", json={"repo": "irrelevant", "ref": "main"})
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    try:
        deadline = time.time() + 2
        while service_env.get(f"/revisions/jobs/{job_id}").json()["status"] == "pending" and time.time() < deadline:
            time.sleep(0.005)
        result_resp = service_env.get(f"/revisions/jobs/{job_id}/result")
        assert result_resp.status_code == 409
    finally:
        release.set()
        _await_job(service_env, job_id)


def test_full_flow_build_then_query_through_the_proxy(service_env: TestClient, tmp_path: Path):
    repo = _make_test_repo(tmp_path)

    create_resp = service_env.post("/revisions", json={"repo": str(repo), "ref": "main"})
    assert create_resp.status_code == 202
    body = create_resp.json()
    assert body["status"] == "pending"
    job_id = body["job_id"]
    assert create_resp.headers["location"] == f"/revisions/jobs/{job_id}"

    status = _await_job(service_env, job_id)
    assert status["status"] == "done", status
    assert status["result_url"] == f"/revisions/jobs/{job_id}/result"

    result = service_env.get(f"/revisions/jobs/{job_id}/result").json()
    sha = result["sha"]
    assert result["datasette_url"] == f"/{sha}"

    # Bare single-segment route -> Datasette's db index.
    index_resp = service_env.get(f"/{sha}.json")
    assert index_resp.status_code == 200
    table_names = {t["name"] for t in index_resp.json()["tables"]}
    assert "literals" in table_names

    # Multi-segment route -> a real table, with the actual generated literal in it.
    table_resp = service_env.get(f"/{sha}/literals.json")
    assert table_resp.status_code == 200
    table_body = table_resp.json()
    value_index = table_body["columns"].index("value")
    values = {row[value_index] for row in table_body["rows"]}
    assert "Saved successfully" in values

    # Bare single-segment route + ?sql= -> the raw SQL endpoint.
    sql_resp = service_env.get(f"/{sha}.json", params={"sql": "select value from literals"})
    assert sql_resp.status_code == 200
    assert ["Saved successfully"] in sql_resp.json()["rows"]


def test_second_job_for_the_same_revision_is_a_cache_hit(service_env: TestClient, tmp_path: Path, monkeypatch):
    repo = _make_test_repo(tmp_path)
    calls = []
    real = generation.index_from_git

    def _counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(generation, "index_from_git", _counting)

    first_job = service_env.post("/revisions", json={"repo": str(repo), "ref": "main"}).json()["job_id"]
    first_result = _await_job(service_env, first_job)
    assert first_result["status"] == "done"

    second_job = service_env.post("/revisions", json={"repo": str(repo), "ref": "main"}).json()["job_id"]
    second_result = _await_job(service_env, second_job)
    assert second_result["status"] == "done"

    first_sha = service_env.get(f"/revisions/jobs/{first_job}/result").json()["sha"]
    second_sha = service_env.get(f"/revisions/jobs/{second_job}/result").json()["sha"]
    assert first_sha == second_sha
    assert len(calls) == 1
