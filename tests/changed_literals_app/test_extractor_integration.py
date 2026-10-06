import subprocess
from pathlib import Path

from changed_literals.extractor import extract


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _write(repo: Path, rel_path: str, content: str) -> None:
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_end_to_end_against_local_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    _write(
        repo,
        "src/Widget.tsx",
        'function Widget() { return <div>Old greeting</div>; }\n',
    )
    _write(
        repo,
        "views/example.scala.html",
        "<div><p>Old sentence here for the panel.</p></div>\n",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "base")
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    _write(
        repo,
        "src/Widget.tsx",
        'function Widget() { return <div>New greeting for everyone</div>; }\n',
    )
    _write(
        repo,
        "views/example.scala.html",
        "<div><p>New sentence here for the panel.</p></div>\n",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "updated")
    updated_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    findings = extract(str(repo), base_sha, updated_sha)

    added_texts = {f.text for f in findings if f.change == "added"}
    removed_texts = {f.text for f in findings if f.change == "removed"}

    assert "New greeting for everyone" in added_texts
    assert "New sentence here for the panel." in added_texts
    assert "Old greeting" in removed_texts
    assert "Old sentence here for the panel." in removed_texts


def test_updated_ref_on_non_default_branch(tmp_path: Path):
    """A clone only checks out its default branch; other branches must still resolve by name."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    _write(repo, "src/Widget.tsx", 'function Widget() { return <div>Old greeting</div>; }\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "base")

    _git(repo, "checkout", "--quiet", "-b", "feature.branch")
    _write(repo, "src/Widget.tsx", 'function Widget() { return <div>New greeting</div>; }\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "updated")
    _git(repo, "checkout", "--quiet", "main")

    findings = extract(str(repo), "main", "feature.branch")

    added_texts = {f.text for f in findings if f.change == "added"}
    removed_texts = {f.text for f in findings if f.change == "removed"}

    assert "New greeting" in added_texts
    assert "Old greeting" in removed_texts


def test_ignores_base_commits_made_after_branch_diverged(tmp_path: Path):
    """Diffing must isolate what updated_ref changed since it diverged, not
    everything that differs between the two tips. If base_ref keeps moving
    after updated_ref branches off, those later base_ref-only commits must
    not show up as if updated_ref had reverted them."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    _write(repo, "src/Widget.tsx", 'function Widget() { return <div>Old greeting</div>; }\n')
    _write(repo, "src/Other.tsx", 'function Other() { return <div>Unrelated text</div>; }\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "base")

    _git(repo, "checkout", "--quiet", "-b", "feature.branch")
    _write(repo, "src/Widget.tsx", 'function Widget() { return <div>New greeting</div>; }\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "updated")

    _git(repo, "checkout", "--quiet", "main")
    _write(repo, "src/Other.tsx", 'function Other() { return <div>Changed after divergence</div>; }\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "unrelated later change on main")

    findings = extract(str(repo), "main", "feature.branch")

    changed_files = {f.file for f in findings}
    added_texts = {f.text for f in findings if f.change == "added"}
    removed_texts = {f.text for f in findings if f.change == "removed"}

    assert changed_files == {"src/Widget.tsx"}
    assert "New greeting" in added_texts
    assert "Old greeting" in removed_texts
    assert "Unrelated text" not in removed_texts
    assert "Changed after divergence" not in added_texts


def test_repo_cache_is_reused_across_calls(tmp_path: Path):
    """The second call must not re-clone -- only the cache's git dir may change."""
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

    _write(repo, "src/Widget.tsx", 'function Widget() { return <div>New greeting</div>; }\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "updated")
    updated_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    cache_dir = tmp_path / "cache"
    findings = extract(str(repo), base_sha, updated_sha, repo_cache=cache_dir)
    assert {f.text for f in findings if f.change == "added"} == {"New greeting"}
    assert cache_dir.exists()
    clone_mtime = (cache_dir / "HEAD").stat().st_mtime

    # Second call against the same cache: it must be reused, not re-cloned.
    findings_again = extract(str(repo), base_sha, updated_sha, repo_cache=cache_dir)
    assert {f.text for f in findings_again if f.change == "added"} == {"New greeting"}
    assert (cache_dir / "HEAD").stat().st_mtime == clone_mtime


def test_repo_cache_picks_up_moved_branch(tmp_path: Path):
    """A branch name may have moved upstream since it was last cached; force-fetching
    on every call (since a bare cache has no worktree to refuse the fetch) keeps it
    from serving a stale diff on the second call."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    _write(repo, "src/Widget.tsx", 'function Widget() { return <div>Old greeting</div>; }\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "base")
    _git(repo, "checkout", "--quiet", "-b", "feature")
    _git(repo, "checkout", "--quiet", "main")

    cache_dir = tmp_path / "cache"
    first = extract(str(repo), "main", "feature", repo_cache=cache_dir)
    assert first == []  # feature == main so far, no diff yet

    _git(repo, "checkout", "--quiet", "feature")
    _write(repo, "src/Widget.tsx", 'function Widget() { return <div>New greeting</div>; }\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "updated")
    _git(repo, "checkout", "--quiet", "main")

    second = extract(str(repo), "main", "feature", repo_cache=cache_dir)
    added_texts = {f.text for f in second if f.change == "added"}
    assert "New greeting" in added_texts
