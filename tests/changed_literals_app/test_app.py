import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from changed_literals import app
from changed_literals.git_ops import GitError


@pytest.fixture(autouse=True)
def _clear_state():
    app._cache.clear()
    app._jobs.clear()
    yield
    app._cache.clear()
    app._jobs.clear()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _write(repo: Path, rel_path: str, content: str) -> None:
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _make_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    _write(repo, "src/Widget.tsx", 'function Widget() { return <div>Old greeting</div>; }\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "base")
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    _write(
        repo, "src/Widget.tsx", 'function Widget() { return <div>New greeting for everyone</div>; }\n'
    )
    _write(repo, "src/Other.tsx", 'function Other() { return <div>Second new string</div>; }\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "updated")
    updated_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    return repo, base_sha, updated_sha


client = TestClient(app.app)


def _await_job(job_id: str, *, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/changed-literals/jobs/{job_id}").json()
        if body["status"] in ("done", "error"):
            return body
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


def _submit_and_await(payload: dict, *, timeout: float = 5.0) -> tuple[str, dict]:
    resp = client.post("/changed-literals/jobs", json=payload)
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    return job_id, _await_job(job_id, timeout=timeout)


# ── helpers ──────────────────────────────────────────────────────────────


def test_parse_languages_none_means_all():
    assert app._parse_languages(None) is None
    assert app._parse_languages("") is None


def test_parse_languages_splits_and_strips():
    assert app._parse_languages("ts, tsx") == {"ts", "tsx"}


def test_parse_languages_rejects_unknown():
    with pytest.raises(Exception) as exc_info:
        app._parse_languages("ts,cobol")
    assert exc_info.value.status_code == 400


def test_repo_cache_path_none_when_unset(monkeypatch):
    monkeypatch.setattr(app, "REPO_CACHE_DIR", None)
    assert app._repo_cache_path("https://example.com/repo.git") is None


def test_repo_cache_path_hashes_per_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "REPO_CACHE_DIR", str(tmp_path))
    path_a = app._repo_cache_path("https://example.com/a.git")
    path_b = app._repo_cache_path("https://example.com/b.git")
    assert path_a.parent == tmp_path
    assert path_a != path_b
    # Deterministic: same URL always maps to the same subdirectory.
    assert app._repo_cache_path("https://example.com/a.git") == path_a


def test_paginate_slices_and_reports_total():
    items = list(range(10))
    page_items, total = app._paginate(items, offset=3, limit=3)
    assert page_items == [3, 4, 5]
    assert total == 10


def test_paginate_past_the_end_is_empty():
    page_items, total = app._paginate(list(range(5)), offset=99, limit=10)
    assert page_items == []
    assert total == 5


def test_cache_key_ignores_language_order():
    key_a = app._cache_key("repo", "b", "u", {"ts", "tsx"})
    key_b = app._cache_key("repo", "b", "u", {"tsx", "ts"})
    assert key_a == key_b


def test_cached_findings_reuses_within_ttl(monkeypatch):
    calls = []
    monkeypatch.setattr(app, "extract", lambda *a, **k: calls.append(1) or [])
    app._cached_findings("repo", "base", "upd", None, force_refresh=False)
    app._cached_findings("repo", "base", "upd", None, force_refresh=False)
    assert len(calls) == 1


def test_cached_findings_refresh_bypasses_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(app, "extract", lambda *a, **k: calls.append(1) or [])
    app._cached_findings("repo", "base", "upd", None, force_refresh=False)
    app._cached_findings("repo", "base", "upd", None, force_refresh=True)
    assert len(calls) == 2


def test_cached_findings_recomputes_after_ttl_expiry(monkeypatch):
    calls = []
    monkeypatch.setattr(app, "extract", lambda *a, **k: calls.append(1) or [])
    monkeypatch.setattr(app, "CACHE_TTL_SECONDS", 0.01)
    app._cached_findings("repo", "base", "upd", None, force_refresh=False)
    time.sleep(0.02)
    app._cached_findings("repo", "base", "upd", None, force_refresh=False)
    assert len(calls) == 2


def test_prune_expired_jobs_removes_only_old_ones():
    app._jobs["old"] = app.Job(id="old", status="done", created_at=time.time() - 100)
    app._jobs["fresh"] = app.Job(id="fresh", status="done", created_at=time.time())
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(app, "JOB_RETENTION_SECONDS", 10)
        app._prune_expired_jobs()
    assert "old" not in app._jobs
    assert "fresh" in app._jobs


# ── POST /changed-literals/jobs ───────────────────────────────────────────────────────────


def test_create_job_returns_202_with_status_and_result_urls():
    resp = client.post("/changed-literals/jobs", json={"repo": "irrelevant", "base": "a", "updated": "b"})
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "pending"
    job_id = body["job_id"]
    assert body["status_url"] == f"/changed-literals/jobs/{job_id}"
    assert resp.headers["location"] == f"/changed-literals/jobs/{job_id}"
    _await_job(job_id)  # let the worker thread finish before the fixture clears _jobs


def test_create_job_rejects_unknown_language():
    resp = client.post(
        "/changed-literals/jobs", json={"repo": "irrelevant", "base": "a", "updated": "b", "languages": "cobol"}
    )
    assert resp.status_code == 400


def test_create_job_missing_required_field_is_422():
    resp = client.post("/changed-literals/jobs", json={"base": "a", "updated": "b"})
    assert resp.status_code == 422


# ── GET /changed-literals/jobs/{id} ───────────────────────────────────────────────────────


def test_job_status_404_for_unknown_job():
    resp = client.get("/changed-literals/jobs/does-not-exist")
    assert resp.status_code == 404


def test_job_status_reaches_done_and_links_result(tmp_path):
    repo, base_sha, updated_sha = _make_repo(tmp_path)
    job_id, status = _submit_and_await(
        {"repo": str(repo), "base": base_sha, "updated": updated_sha}
    )
    assert status["status"] == "done"
    assert status["result_url"] == f"/changed-literals/jobs/{job_id}/result"


def test_job_status_reports_git_error(monkeypatch):
    monkeypatch.setattr(app, "extract", lambda *a, **k: (_ for _ in ()).throw(GitError("boom")))
    job_id, status = _submit_and_await({"repo": "/does/not/exist", "base": "a", "updated": "b"})
    assert status["status"] == "error"
    assert "boom" in status["error"]


def test_job_status_reports_internal_error_without_crashing_worker(monkeypatch):
    monkeypatch.setattr(app, "extract", lambda *a, **k: (_ for _ in ()).throw(ValueError("kaboom")))
    job_id, status = _submit_and_await({"repo": "irrelevant", "base": "a", "updated": "b"})
    assert status["status"] == "error"


# ── GET /changed-literals/jobs/{id}/result ───────────────────────────────────────────────


def test_job_result_404_for_unknown_job():
    resp = client.get("/changed-literals/jobs/does-not-exist/result")
    assert resp.status_code == 404


def test_job_result_409_while_pending(monkeypatch):
    # Block the worker thread so the job never leaves "running" during the assertion.
    release = __import__("threading").Event()
    monkeypatch.setattr(app, "extract", lambda *a, **k: release.wait(5) and [])

    resp = client.post("/changed-literals/jobs", json={"repo": "irrelevant", "base": "a", "updated": "b"})
    job_id = resp.json()["job_id"]
    try:
        deadline = time.time() + 2
        while client.get(f"/changed-literals/jobs/{job_id}").json()["status"] == "pending" and time.time() < deadline:
            time.sleep(0.005)
        result_resp = client.get(f"/changed-literals/jobs/{job_id}/result")
        assert result_resp.status_code == 409
    finally:
        release.set()
        _await_job(job_id)  # let the worker thread finish before the next test clears _jobs


def test_job_result_returns_json_envelope_with_pagination_info(tmp_path):
    repo, base_sha, updated_sha = _make_repo(tmp_path)
    job_id, status = _submit_and_await(
        {"repo": str(repo), "base": base_sha, "updated": updated_sha}
    )
    assert status["status"] == "done"

    unpaged = client.get(f"/changed-literals/jobs/{job_id}/result")
    total = unpaged.json()["total"]
    assert total > 1  # otherwise pagination below can't be exercised

    resp = client.get(f"/changed-literals/jobs/{job_id}/result", params={"limit": 1})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")

    body = resp.json()
    assert body["offset"] == 0
    assert body["limit"] == 1
    assert body["total"] == total
    assert len(body["findings"]) == 1


def test_job_result_offset_selects_a_later_window(tmp_path):
    repo, base_sha, updated_sha = _make_repo(tmp_path)
    job_id, status = _submit_and_await(
        {"repo": str(repo), "base": base_sha, "updated": updated_sha}
    )
    all_findings = client.get(f"/changed-literals/jobs/{job_id}/result").json()["findings"]
    assert len(all_findings) > 1  # otherwise the offset below can't be exercised

    resp = client.get(f"/changed-literals/jobs/{job_id}/result", params={"offset": 1, "limit": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["offset"] == 1
    assert body["findings"] == all_findings[1:2]


def test_job_result_offset_past_end_is_empty_but_200(tmp_path):
    repo, base_sha, updated_sha = _make_repo(tmp_path)
    job_id, status = _submit_and_await(
        {"repo": str(repo), "base": base_sha, "updated": updated_sha}
    )
    total = client.get(f"/changed-literals/jobs/{job_id}/result").json()["total"]

    resp = client.get(f"/changed-literals/jobs/{job_id}/result", params={"offset": 9999})
    assert resp.status_code == 200
    body = resp.json()
    assert body["findings"] == []
    assert body["total"] == total


def test_job_result_error_job_returns_500(monkeypatch):
    monkeypatch.setattr(app, "extract", lambda *a, **k: (_ for _ in ()).throw(GitError("boom")))
    job_id, status = _submit_and_await({"repo": "/does/not/exist", "base": "a", "updated": "b"})
    assert status["status"] == "error"

    resp = client.get(f"/changed-literals/jobs/{job_id}/result")
    assert resp.status_code == 500
    assert "boom" in resp.json()["detail"]


def test_index_returns_usage():
    resp = client.get("/")
    assert resp.status_code == 200
    assert "jobs" in resp.json()["usage"]
