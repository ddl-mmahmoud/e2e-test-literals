"""Orchestrates git_ops + diff_parser + per-file adapter dispatch + walker."""

from __future__ import annotations

import tempfile
from pathlib import Path

from . import git_ops
from .diff_parser import DiffFile, parse_diff
from .finding import Finding
from .languages import pathspecs_for, resolve_adapter
from .languages.base import LanguageAdapter
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
                collect_findings(base_blob, adapter, diff_file.removed_lines, "removed", diff_file.path)
            )

    if not diff_file.is_deleted_file and diff_file.added_lines:
        updated_blob = git_ops.show_blob(repo_dir, updated_ref, diff_file.path)
        if updated_blob is not None:
            findings.extend(
                collect_findings(updated_blob, adapter, diff_file.added_lines, "added", diff_file.path)
            )

    return findings


def _extract_from_repo(
    repo_dir: Path,
    base_ref: str,
    updated_ref: str,
    languages: set[str] | None,
    *,
    force_fetch: bool,
) -> list[Finding]:
    git_ops.fetch_refs(repo_dir, [base_ref, updated_ref], force=force_fetch)

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
) -> list[Finding]:
    """Run the pipeline against repo_url.

    With `repo_cache`, a bare clone is created there on first use and reused
    on every later call instead of being re-cloned -- and never cleaned up,
    so several invocations (even concurrent ones, since a bare repo has no
    worktree to contend over) can share it. Without it, a full clone is made
    in a temp dir and discarded once this call returns.
    """
    if repo_cache is not None:
        git_ops.ensure_repo_cache(repo_url, repo_cache)
        return _extract_from_repo(repo_cache, base_ref, updated_ref, languages, force_fetch=True)

    with tempfile.TemporaryDirectory(prefix="changed-literals-") as tmp:
        repo_dir = Path(tmp) / "repo"
        git_ops.clone(repo_url, repo_dir)
        return _extract_from_repo(repo_dir, base_ref, updated_ref, languages, force_fetch=False)
