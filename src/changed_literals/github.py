"""Resolve a GitHub pull request number to the two commits a diff should compare.

Uses the one GitHub REST call that returns both (`GET /repos/{owner}/{repo}/pulls/{n}`)
over the stdlib's `urllib`, authenticated with the same `GITHUB_LITERALS_PAT` that
`git_ops` passes to `git clone`/`git fetch` -- so neither the `gh` CLI nor an HTTP/GitHub
client library is needed, and a repo this service can clone is one whose PRs it can read.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import NamedTuple
from urllib.parse import urlparse

from . import git_ops

_API_TIMEOUT_SECONDS = 30

# scp-style remote, `git@github.com:owner/repo.git`.
_SCP_REMOTE = re.compile(r"^(?:[^@/]+@)?(?P<host>[^/:]+):(?P<path>[^/].*)$")


class PullRequestError(git_ops.GitError):
    """A pull request couldn't be resolved. A `GitError` so every caller that already
    reports a failed clone/fetch to its user reports this the same way."""


class PullRequestRefs(NamedTuple):
    base: str  # commit SHA the PR is based on
    head: str  # commit SHA of the PR's branch tip


def _host_and_slug(repo_url: str) -> tuple[str, str]:
    """`(host, "owner/repo")` of an https://, ssh://, git:// or scp-style remote URL."""
    if "://" in repo_url:
        parsed = urlparse(repo_url)
        host, path = parsed.hostname, parsed.path
    else:
        match = _SCP_REMOTE.match(repo_url)
        host, path = (match["host"], match["path"]) if match else (None, "")

    slug = path.strip("/").removesuffix(".git")
    if not host or slug.count("/") != 1:
        raise PullRequestError(
            f"can't resolve a pull request on {repo_url!r}: not a GitHub-style owner/repo remote URL"
        )
    return host, slug


def _api_base(host: str) -> str:
    return "https://api.github.com" if host in ("github.com", "www.github.com") else f"https://{host}/api/v3"


def resolve_pull_request(repo_url: str, number: int) -> PullRequestRefs:
    """The base and head commit SHAs of pull request `number` of `repo_url`.

    SHAs rather than branch names: a fork's branch doesn't exist in `repo_url`, and a
    SHA keeps the result immutable for whatever caches on it.
    """
    host, slug = _host_and_slug(repo_url)
    request = urllib.request.Request(
        f"{_api_base(host)}/repos/{slug}/pulls/{number}",
        headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
    )
    token = git_ops.pat()
    if token:
        request.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(request, timeout=_API_TIMEOUT_SECONDS) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        # GitHub answers 404 (not 403) for a private repo the token can't see.
        hint = " (no such pull request, or GITHUB_LITERALS_PAT can't see the repo)" if exc.code == 404 else ""
        raise PullRequestError(f"GitHub returned {exc.code} for {slug} pull request #{number}{hint}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise PullRequestError(f"couldn't fetch {slug} pull request #{number}: {exc}") from exc

    return PullRequestRefs(base=body["base"]["sha"], head=body["head"]["sha"])
