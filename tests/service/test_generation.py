"""Tests for service/generation.py against a real, throwaway local git repo (same
technique as changed-literals' test_extractor_integration.py and test_app.py) -- local
paths are valid git clone sources, so this needs no network."""

from __future__ import annotations

import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest
from routing_helpers import write_fake_routing_repo

from e2e_test_literals.service import config, generation


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _make_test_repo(tmp_path: Path) -> Path:
    """A minimal `internal-e2e-tests-service`-shaped repo: just enough for
    `bootstrap_step_registry` (an empty, importable `steps` package) and one
    `@testrail`-tagged scenario for `build_case_index` to actually find something."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    steps_dir = repo / "tests" / "ui" / "features" / "steps"
    steps_dir.mkdir(parents=True)
    (steps_dir / "__init__.py").write_text("")

    # repo_checkout.checkout_tree's default TESTS_SUBPATHS is ("tests/ui",
    # "tests/common", "tests/helpers") -- `git archive` fails outright if *any* of its
    # pathspecs matches nothing, so both need at least one tracked file even though
    # nothing in this test reads their contents.
    (repo / "tests" / "common").mkdir(parents=True)
    (repo / "tests" / "common" / ".gitkeep").write_text("")
    (repo / "tests" / "helpers").mkdir(parents=True)
    (repo / "tests" / "helpers" / ".gitkeep").write_text("")

    # cucurc.py's CUCURC_PATH -- Pass B (templated.resolve_templated_literals) reads
    # this unconditionally; empty is a valid, real "no static vars defined" file.
    (repo / "tests" / "ui" / "features" / "cucurc.yml").write_text("")

    feature_path = repo / "tests" / "ui" / "features" / "example.feature"
    feature_path.write_text(
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
def generation_env(tmp_path: Path, monkeypatch, bootstrapped_registry) -> None:
    monkeypatch.setattr(config, "DB_DIR", tmp_path / "db")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "REPO_CACHE_DIR", tmp_path / "repo-cache")


def test_resolve_and_generate_builds_a_queryable_db(tmp_path: Path, generation_env):
    repo = _make_test_repo(tmp_path)

    sha = generation.resolve_and_generate(str(repo), "main")

    assert generation.db_path(sha).exists()
    conn = sqlite3.connect(generation.db_path(sha))
    try:
        rows = conn.execute("SELECT value FROM literals").fetchall()
    finally:
        conn.close()
    assert ("Saved successfully",) in rows


def test_resolve_and_generate_keys_by_resolved_sha_not_raw_ref(tmp_path: Path, generation_env):
    repo = _make_test_repo(tmp_path)

    sha_via_branch = generation.resolve_and_generate(str(repo), "main")
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    assert sha_via_branch == head_sha


def test_resolve_and_generate_is_a_cache_hit_on_the_second_call(tmp_path: Path, generation_env, monkeypatch):
    repo = _make_test_repo(tmp_path)
    calls = []
    real_index_from_git = generation.index_from_git

    def _counting_index_from_git(*args, **kwargs):
        calls.append(1)
        return real_index_from_git(*args, **kwargs)

    monkeypatch.setattr(generation, "index_from_git", _counting_index_from_git)

    generation.resolve_and_generate(str(repo), "main")
    generation.resolve_and_generate(str(repo), "main")

    assert len(calls) == 1


def test_resolve_and_generate_rebuilds_after_a_new_commit_on_the_same_branch(tmp_path: Path, generation_env):
    repo = _make_test_repo(tmp_path)

    first_sha = generation.resolve_and_generate(str(repo), "main")

    (repo / "tests" / "ui" / "features" / "example.feature").write_text(
        "Feature: Example\n"
        "\n"
        "  @testrail(1234)\n"
        '  Scenario: Save a widget\n'
        '    Then I click the button "Saved differently"\n'
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "change literal")
    second_sha = generation.resolve_and_generate(str(repo), "main")

    assert first_sha != second_sha
    assert generation.db_path(first_sha).exists()  # old revision's db is untouched/retained
    assert generation.db_path(second_sha).exists()


def test_concurrent_requests_for_the_same_revision_build_only_once(tmp_path: Path, generation_env, monkeypatch):
    repo = _make_test_repo(tmp_path)
    calls = []
    real_index_from_git = generation.index_from_git

    def _slow_counting_index_from_git(*args, **kwargs):
        calls.append(1)
        time.sleep(0.2)
        return real_index_from_git(*args, **kwargs)

    monkeypatch.setattr(generation, "index_from_git", _slow_counting_index_from_git)

    results = []

    def _worker():
        results.append(generation.resolve_and_generate(str(repo), "main"))

    threads = [threading.Thread(target=_worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(calls) == 1
    assert len(set(results)) == 1


def test_resolve_and_generate_stores_routing_data_when_the_revision_has_the_routing_files(
    tmp_path: Path, generation_env
):
    repo = _make_test_repo(tmp_path)
    write_fake_routing_repo(repo)
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "add routing files")

    sha = generation.resolve_and_generate(str(repo), "main")

    conn = sqlite3.connect(generation.db_path(sha))
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        case_ids = {row[0] for row in conn.execute("SELECT case_id FROM entries")}
    finally:
        conn.close()
    assert meta == {"routing_indexed": "1"}
    assert case_ids == {1234, 67895, 555, 7001}


def test_resolve_and_generate_still_builds_the_literal_index_when_routing_is_unavailable(
    tmp_path: Path, generation_env
):
    repo = _make_test_repo(tmp_path)  # no build/circleci files, like a revision that predates them

    sha = generation.resolve_and_generate(str(repo), "main")

    conn = sqlite3.connect(generation.db_path(sha))
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        literal_values = [row[0] for row in conn.execute("SELECT value FROM literals")]
    finally:
        conn.close()
    assert "Saved successfully" in literal_values
    assert meta["routing_indexed"] == "0"
    assert "run_ui_tests.py is not present" in meta["routing_error"]


def test_resolve_and_generate_rebuilds_a_db_that_predates_routing_data(tmp_path: Path, generation_env, monkeypatch):
    repo = _make_test_repo(tmp_path)
    sha = generation.resolve_and_generate(str(repo), "main")
    conn = sqlite3.connect(generation.db_path(sha))
    conn.execute("DROP TABLE meta")  # what a db built before routing data existed looks like
    conn.commit()
    conn.close()
    calls = []
    real_index_from_git = generation.index_from_git
    monkeypatch.setattr(
        generation, "index_from_git", lambda *a, **k: calls.append(1) or real_index_from_git(*a, **k)
    )

    assert generation.resolve_and_generate(str(repo), "main") == sha
    assert generation.resolve_and_generate(str(repo), "main") == sha

    assert len(calls) == 1  # rebuilt once, then a cache hit again
    conn = sqlite3.connect(generation.db_path(sha))
    try:
        assert conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0] >= 1
    finally:
        conn.close()
