"""Tests for repo_checkout.checkout_tree's optional (routing) subpaths, against a real
throwaway local git repo."""

from __future__ import annotations

import subprocess
from pathlib import Path

from routing_helpers import write_fake_routing_repo

from e2e_test_literals.repo_checkout import checkout_tree


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _commit_all(repo: Path) -> None:
    _git(repo, "init", "--quiet", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "base")


def _required_tree(repo: Path) -> None:
    for subpath in ("tests/ui", "tests/common", "tests/helpers"):
        (repo / subpath).mkdir(parents=True, exist_ok=True)
        (repo / subpath / ".gitkeep").write_text("")


def test_checkout_includes_routing_files_when_the_revision_has_them(tmp_path):
    repo = write_fake_routing_repo(tmp_path / "repo")
    _required_tree(repo)
    _commit_all(repo)

    with checkout_tree(str(repo), "main") as tree:
        assert (tree / "build/circleci/run_ui_tests.py").is_file()
        assert (tree / "build/circleci/step_run_api_tests.py").is_file()
        assert (tree / "tests/api/tests/test_gpu.py").is_file()
        assert (tree / "tests/ui/features/domino/login/login.feature").is_file()


def test_checkout_of_a_revision_without_routing_files_still_succeeds(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _required_tree(repo)
    _commit_all(repo)

    with checkout_tree(str(repo), "main") as tree:
        assert (tree / "tests/ui").is_dir()
        assert not (tree / "build").exists()
        assert not (tree / "tests/api").exists()
