"""Get a real, on-disk `tests/` subtree out of a remote `internal-e2e-tests-service`
checkout, without ever materializing a live working tree for concurrent runs to collide over.

Adapted from `changed-literals`'s `git_ops.py` (bare-clone cache + fetch), but for a
different shape of consumer: `changed-literals` only ever needs blob content at a ref
(`git show <ref>:<path>`), since its diff/AST walk works entirely off git objects. The
indexer here (`features.py`/`bootstrap.py`/`templated.py`) needs actual files on disk --
it globs `*.feature` files, calls `behave.parser.parse_file`, and `import`s this repo's
own step-definition Python modules to populate the step registry. A bare clone alone
can't serve that, so this module adds one more step on top: `git archive <ref> -- <paths>`
un-tarred into a fresh temp directory per call. That keeps the same "no live worktree to
disturb" property the bare cache relies on -- each caller gets its own materialized copy
even though they all share one underlying bare clone.
"""

from __future__ import annotations

import base64
import os
import subprocess
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class GitError(RuntimeError):
    """A git subprocess exited non-zero."""


# The subtree needed for `bootstrap_step_registry` to import this repo's custom step
# modules (which pull in `common.*` and `helpers.*`) plus `discover_feature_files`/
# `templated.py`'s AST scan. Not the generated `domino_client_v4`/`domino_public_client`
# packages -- `bootstrap.py` stubs those out when absent -- and not `tests/api`,
# `tests/setup`, `tests/bin` (nothing under `tests/ui/features/steps/**` imports from
# them; widen this if a future `ImportError` says otherwise).
TESTS_SUBPATHS = ("tests/ui", "tests/common", "tests/helpers")


def _auth_args() -> list[str]:
    """`-c http.extraheader=...` from GITHUB_LITERALS_PAT, or [] if unset.

    Same mechanism as changed-literals' git_ops.py: GitHub's HTTPS remotes accept a PAT
    as the password half of Basic auth, passed as a `-c` override so it never lands in
    `.git/config`. Reusing the same env var name on purpose -- it's already a generic
    "literals tooling" PAT, not scoped to changed-literals specifically.
    """
    token = os.environ.get("GITHUB_LITERALS_PAT")
    if not token:
        return []
    credentials = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.extraheader=Authorization: Basic {credentials}"]


def _redact(args: list[str]) -> list[str]:
    return ["<redacted>" if "Authorization" in arg else arg for arg in args]


# See changed-literals' git_ops.py for the CVE-2022-24765 rationale: a repo-cache
# directory persists across invocations/container restarts, so its owner UID can drift
# from whatever UID this process runs as. Every repo this tool operates on is one it
# manages itself under a server-controlled path, never an arbitrary user-owned directory.
_SAFE_DIRECTORY_ARGS = ["-c", "safe.directory=*"]


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(["git", *_SAFE_DIRECTORY_ARGS, *args], cwd=cwd, capture_output=True)
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        raise GitError(f"git {' '.join(_redact(args))} failed: {stderr}")
    return result


def ensure_repo_cache(repo_url: str, cache_dir: Path) -> None:
    """Make cache_dir a bare clone of repo_url, reusing it if already populated."""
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


def fetch_ref(repo: Path, ref: str, *, force: bool = False) -> None:
    """Make sure `ref` is resolvable locally by its literal name -- see changed-literals'
    `fetch_refs` for the full rationale (bare-name lookup, force-refetch for a cached
    branch that may have moved upstream). Single-ref variant since this tool only ever
    indexes one ref at a time, not a base/updated pair."""
    resolves = _resolves(repo, ref)
    if resolves and not (force and _is_local_branch(repo, ref)):
        return
    _run([*_auth_args(), "fetch", "--quiet", "origin", f"+{ref}:refs/heads/{ref}"], cwd=repo)


def materialize_subtree(repo_dir: Path, ref: str, subpaths: tuple[str, ...], dest: Path) -> None:
    """Extract `subpaths` at `ref` into `dest` as real files, preserving their full
    repo-relative paths (so `dest / "tests/ui/features"` etc. works exactly like a
    normal checkout root). Uses `git archive` rather than `git worktree add` -- nothing
    downstream needs `.git` plumbing in the materialized copy, only the files, and this
    stays stateless per call (no worktree registration/cleanup on the shared bare cache).
    """
    archive = _run(["archive", "--format=tar", ref, "--", *subpaths], cwd=repo_dir).stdout
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".tar") as tmp_tar:
        tmp_tar.write(archive)
        tmp_tar.flush()
        with tarfile.open(tmp_tar.name) as tar:
            tar.extractall(dest, filter="data")


@contextmanager
def checkout_tree(
    repo_url: str,
    ref: str,
    *,
    repo_cache: Path | None = None,
    subpaths: tuple[str, ...] = TESTS_SUBPATHS,
) -> Iterator[Path]:
    """Yield a temp directory containing `subpaths` of `repo_url` at `ref`, materialized
    as real files -- usable directly as the `repo_root` argument to `aggregate.build_case_index`
    and friends.

    With `repo_cache`, a bare clone is created there on first use and reused on every
    later call (fetching `ref` with `force=True` each time, since a cached branch may
    have moved upstream). Without it, a full bare clone is made in a temp dir and
    discarded once this call returns -- correct but slow for repeated runs against the
    same repo.
    """
    with tempfile.TemporaryDirectory(prefix="e2e-test-literals-checkout-") as tmp:
        dest = Path(tmp) / "tree"

        if repo_cache is not None:
            ensure_repo_cache(repo_url, repo_cache)
            fetch_ref(repo_cache, ref, force=True)
            materialize_subtree(repo_cache, ref, subpaths, dest)
        else:
            with tempfile.TemporaryDirectory(prefix="e2e-test-literals-clone-") as clone_tmp:
                bare_dir = Path(clone_tmp) / "repo.git"
                ensure_repo_cache(repo_url, bare_dir)
                fetch_ref(bare_dir, ref, force=False)
                materialize_subtree(bare_dir, ref, subpaths, dest)

        yield dest
