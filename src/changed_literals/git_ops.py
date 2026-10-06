"""Thin subprocess wrappers around the git binary.

No GitPython: cloning, fetching, diffing, and reading a blob at a ref are
all it takes, so a subprocess call per operation keeps the dependency list
to just the AST tooling.
"""

from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    """A git subprocess exited non-zero."""


def _auth_args() -> list[str]:
    """`-c http.extraheader=...` from GITHUB_LITERALS_PAT, or [] if unset.

    GitHub's HTTPS remotes accept a PAT as the password half of Basic auth;
    the username is a placeholder since GitHub ignores it for tokens. Passed
    as a `-c` override rather than baked into the URL so it never lands in
    `.git/config`. Harmless to include even for a ssh:// or local-path
    remote -- git only consults http.* config for the HTTP transport.
    """
    token = os.environ.get("GITHUB_LITERALS_PAT")
    if not token:
        return []
    credentials = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.extraheader=Authorization: Basic {credentials}"]


def _redact(args: list[str]) -> list[str]:
    return ["<redacted>" if "Authorization" in arg else arg for arg in args]


# A repo-cache directory persists across invocations (and, for the app
# wrapper, across container restarts on a mounted volume), so its owner UID
# can end up not matching whatever UID this process runs as. Git's dubious-
# ownership check (CVE-2022-24765) then refuses to touch it. Every repo this
# tool operates on is one it manages itself under a server-controlled path,
# never an arbitrary user-owned directory, so blanket-trusting ownership for
# these invocations is safe -- and passing it as a `-c` override rather than
# writing to ~/.gitconfig keeps it thread-safe across concurrent jobs.
_SAFE_DIRECTORY_ARGS = ["-c", "safe.directory=*"]


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(["git", *_SAFE_DIRECTORY_ARGS, *args], cwd=cwd, capture_output=True)
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        raise GitError(f"git {' '.join(_redact(args))} failed: {stderr}")
    return result


def clone(repo_url: str, dest: Path) -> None:
    """Clone repo_url into dest (which must not exist yet, dest.parent must)."""
    _run([*_auth_args(), "clone", "--quiet", repo_url, str(dest)], cwd=dest.parent)


def ensure_repo_cache(repo_url: str, cache_dir: Path) -> None:
    """Make cache_dir a bare clone of repo_url, reusing it if already populated.

    Bare because nothing in this tool reads files off disk -- `diff_unified0`
    and `show_blob` both work against git's object store alone, with no
    working tree required. That in turn is what makes the cache safe to share
    across concurrent invocations: there's no checked-out state for one
    process to disturb while another is mid-diff.
    """
    if cache_dir.exists() and any(cache_dir.iterdir()):
        return
    cache_dir.mkdir(parents=True, exist_ok=True)
    _run(
        [*_auth_args(), "clone", "--quiet", "--bare", repo_url, str(cache_dir)],
        cwd=cache_dir.parent,
    )


def _resolves(repo: Path, ref: str) -> bool:
    return subprocess.run(
        ["git", *_SAFE_DIRECTORY_ARGS, "rev-parse", "--verify", "--quiet", ref],
        cwd=repo,
        capture_output=True,
    ).returncode == 0


def _is_local_branch(repo: Path, ref: str) -> bool:
    return subprocess.run(
        ["git", *_SAFE_DIRECTORY_ARGS, "show-ref", "--verify", "--quiet", f"refs/heads/{ref}"],
        cwd=repo,
        capture_output=True,
    ).returncode == 0


def fetch_refs(repo: Path, refs: list[str], *, force: bool = False) -> None:
    """Make sure `refs` are resolvable locally by their literal name.

    `git fetch origin <ref>` with no destination refspec only populates
    FETCH_HEAD; it leaves nothing resolvable under `<ref>` for later `git
    diff`/`git show` calls. A branch that isn't the one checked out by
    `clone()` only exists as `refs/remotes/origin/<ref>`, which git's bare-name
    lookup won't DWIM to, so it fetches its own local `refs/heads/<ref>`
    instead.

    By default, refs that already resolve (e.g. the checked-out default
    branch, or a SHA already in the clone's history) are left alone, since
    re-fetching the currently checked-out branch is refused by git.

    `force=True` (for a bare `--repo-cache` repo reused across invocations)
    also re-fetches a ref that already resolves *and* names a local branch,
    since that branch may have moved upstream since it was last cached --
    unlike a normal clone's checked-out branch, a bare repo has no worktree
    for git to refuse the update on. A ref that already resolves but isn't a
    local branch (a SHA, or a revision expression like `HEAD~1`) is left
    alone even under `force`: it's either immutable or not a valid fetch
    refspec to begin with.
    """
    for ref in refs:
        resolves = _resolves(repo, ref)
        if resolves and not (force and _is_local_branch(repo, ref)):
            continue
        _run(
            [*_auth_args(), "fetch", "--quiet", "origin", f"+{ref}:refs/heads/{ref}"],
            cwd=repo,
        )


def diff_unified0(repo: Path, base_ref: str, updated_ref: str, pathspecs: list[str]) -> str:
    """Diff against the merge-base of base_ref/updated_ref, not their tips.

    `base_ref updated_ref` (two-dot) diffs the two tree states directly, so if
    base_ref has moved on since updated_ref branched off, its later commits
    show up as part of the diff too. `base_ref...updated_ref` (three-dot)
    diffs against the merge-base instead, isolating just what updated_ref
    itself changed since it diverged -- the actual PR-style diff callers want.
    """
    args = [
        "diff",
        "--no-color",
        "--unified=0",
        "--no-renames",
        f"{base_ref}...{updated_ref}",
        "--",
        *pathspecs,
    ]
    return _run(args, cwd=repo).stdout.decode(errors="replace")


def show_blob(repo: Path, ref: str, path: str) -> bytes | None:
    """Content of `path` at `ref`, or None if it doesn't exist there."""
    result = subprocess.run(
        ["git", *_SAFE_DIRECTORY_ARGS, "show", f"{ref}:{path}"], cwd=repo, capture_output=True
    )
    if result.returncode != 0:
        return None
    return result.stdout
