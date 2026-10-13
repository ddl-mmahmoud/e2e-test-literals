"""Tests for github.resolve_pull_request, with `urllib.request.urlopen` stubbed -- these
verify URL/header construction and error mapping, NOT behavior against the real GitHub
API."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from changed_literals import github
from changed_literals.git_ops import GitError


def _stub_urlopen(monkeypatch, *, body=None, error=None):
    captured = {}

    def fake(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = {k.lower(): v for k, v in request.header_items()}
        if error is not None:
            raise error
        return io.BytesIO(json.dumps(body).encode())

    monkeypatch.setattr(github.urllib.request, "urlopen", fake)
    return captured


_PR_BODY = {"base": {"sha": "b" * 40}, "head": {"sha": "a" * 40}}


@pytest.mark.parametrize(
    "repo_url",
    [
        "https://github.com/acme/widgets",
        "https://github.com/acme/widgets.git",
        "git@github.com:acme/widgets.git",
        "ssh://git@github.com/acme/widgets.git",
    ],
)
def test_resolves_base_and_head_shas_from_any_github_remote_form(monkeypatch, repo_url):
    captured = _stub_urlopen(monkeypatch, body=_PR_BODY)

    refs = github.resolve_pull_request(repo_url, 42)

    assert refs == github.PullRequestRefs(base="b" * 40, head="a" * 40)
    assert captured["url"] == "https://api.github.com/repos/acme/widgets/pulls/42"


def test_uses_the_clone_pat_as_bearer_token(monkeypatch):
    monkeypatch.setenv("GITHUB_LITERALS_PAT", "s3cret")
    captured = _stub_urlopen(monkeypatch, body=_PR_BODY)

    github.resolve_pull_request("https://github.com/acme/widgets", 1)

    assert captured["headers"]["authorization"] == "Bearer s3cret"


def test_no_authorization_header_without_a_pat(monkeypatch):
    monkeypatch.delenv("GITHUB_LITERALS_PAT", raising=False)
    captured = _stub_urlopen(monkeypatch, body=_PR_BODY)

    github.resolve_pull_request("https://github.com/acme/widgets", 1)

    assert "authorization" not in captured["headers"]


def test_enterprise_host_uses_its_api_v3_path(monkeypatch):
    captured = _stub_urlopen(monkeypatch, body=_PR_BODY)

    github.resolve_pull_request("https://git.example.com/acme/widgets", 7)

    assert captured["url"] == "https://git.example.com/api/v3/repos/acme/widgets/pulls/7"


def test_local_path_is_rejected():
    with pytest.raises(github.PullRequestError, match="owner/repo"):
        github.resolve_pull_request("/tmp/some/local/repo", 1)


def test_404_mentions_the_token_and_is_a_git_error(monkeypatch):
    _stub_urlopen(
        monkeypatch, error=urllib.error.HTTPError("u", 404, "Not Found", {}, io.BytesIO(b"{}"))
    )
    with pytest.raises(GitError, match="404.*GITHUB_LITERALS_PAT"):
        github.resolve_pull_request("https://github.com/acme/widgets", 1)


def test_network_failure_is_reported(monkeypatch):
    _stub_urlopen(monkeypatch, error=urllib.error.URLError("no route"))
    with pytest.raises(github.PullRequestError, match="no route"):
        github.resolve_pull_request("https://github.com/acme/widgets", 1)
