"""Orchestrates git_ops + diff_parser + per-file adapter dispatch + walker."""

from __future__ import annotations

import tempfile
from pathlib import Path

from . import git_ops
from .diff_parser import DiffFile, parse_diff
from .finding import Finding
from .languages import pathspecs_for, resolve_adapter
from .languages.base import LanguageAdapter
from .profiling import ANALYZING_LITERALS_DIFF, FETCHING_LITERALS_REPO, PhaseTracker, track
from .walker import collect_findings


def _process_file(
    repo_dir: Path,
    base_ref: str,
    updated_ref: str,
    diff_file: DiffFile,
    adapter: LanguageAdapter,
) -> list[Finding]:
    findings: list[Finding] = []

    if not diff_file.is_new_file and diff_file.removed_lines:
        base_blob = git_ops.show_blob(repo_dir, base_ref, diff_file.path)
        if base_blob is not None:
            findings.extend(
                collect_findings(
                    base_blob,
                    adapter,
                    diff_file.removed_lines,
                    "removed",
                    diff_file.path,
                    hunk_of_line=diff_file.removed_hunks,
                )
            )

    if not diff_file.is_deleted_file and diff_file.added_lines:
        updated_blob = git_ops.show_blob(repo_dir, updated_ref, diff_file.path)
        if updated_blob is not None:
            findings.extend(
                collect_findings(
                    updated_blob,
                    adapter,
                    diff_file.added_lines,
                    "added",
                    diff_file.path,
                    hunk_of_line=diff_file.added_hunks,
                )
            )

    return findings


def _fetch_refs(repo_dir: Path, base_ref: str, updated_ref: str, *, force_fetch: bool, pull_request: int | None) -> None:
    if pull_request is not None:
        git_ops.fetch_pull_head(repo_dir, pull_request)
    git_ops.fetch_refs(repo_dir, [base_ref, updated_ref], force=force_fetch)


def _analyze_diff(
    repo_dir: Path, base_ref: str, updated_ref: str, languages: set[str] | None
) -> list[Finding]:
    diff_text = git_ops.diff_unified0(repo_dir, base_ref, updated_ref, pathspecs_for(languages))
    diff_files = parse_diff(diff_text)

    findings: list[Finding] = []
    for diff_file in diff_files:
        adapter = resolve_adapter(diff_file.path, languages)
        if adapter is None:
            continue
        findings.extend(_process_file(repo_dir, base_ref, updated_ref, diff_file, adapter))

    findings.sort(key=lambda f: f.confidence, reverse=True)
    return findings


def extract(
    repo_url: str,
    base_ref: str,
    updated_ref: str,
    languages: set[str] | None = None,
    repo_cache: Path | None = None,
    pull_request: int | None = None,
    tracker: PhaseTracker | None = None,
) -> list[Finding]:
    """Run the pipeline against repo_url.

    `pull_request` names the GitHub PR that `updated_ref` (its head SHA) belongs to, so
    that commit is fetched via `refs/pull/<n>/head` -- it may live only in a fork.

    With `repo_cache`, a bare clone is created there on first use and reused
    on every later call instead of being re-cloned -- and never cleaned up,
    so several invocations (even concurrent ones, since a bare repo has no
    worktree to contend over) can share it. Without it, a full clone is made
    in a temp dir and discarded once this call returns.

    With a `tracker`, the two stages are reported as its `fetching_literals_repo` (clone or
    reuse the cache, fetch the refs) and `analyzing_literals_diff` (diff, parse, AST walk)
    phases; the first notes whether the repo cache was a hit.
    """
    if repo_cache is not None:
        with track(tracker, FETCHING_LITERALS_REPO) as phase:
            phase.caches["repo_clone"] = "hit" if repo_cache.exists() and any(repo_cache.iterdir()) else "miss"
            git_ops.ensure_repo_cache(repo_url, repo_cache)
            _fetch_refs(repo_cache, base_ref, updated_ref, force_fetch=True, pull_request=pull_request)
        with track(tracker, ANALYZING_LITERALS_DIFF):
            return _analyze_diff(repo_cache, base_ref, updated_ref, languages)

    with tempfile.TemporaryDirectory(prefix="changed-literals-") as tmp:
        repo_dir = Path(tmp) / "repo"
        with track(tracker, FETCHING_LITERALS_REPO) as phase:
            phase.caches["repo_clone"] = "disabled"
            git_ops.clone(repo_url, repo_dir)
            _fetch_refs(repo_dir, base_ref, updated_ref, force_fetch=False, pull_request=pull_request)
        with track(tracker, ANALYZING_LITERALS_DIFF):
            return _analyze_diff(repo_dir, base_ref, updated_ref, languages)
