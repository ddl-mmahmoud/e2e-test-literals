"""The data model behind the impact report page (`report_page.py`, served by
`GET /changed-literals-impact/jobs/<id>/result?report=1`): turns a finished impact result into
exactly what the page shows -- changed product strings, each with the feature files and steps
that may be affected -- with every link and every line of the coding-agent prompt already built.
Keeping all of that here, as plain data in and plain data out, leaves the page's JavaScript
with only rendering, toggling and joining strings, and lets the interesting logic be unit
tested (see IMPACT-REPORT-PLAN.md).

Shape of `build_report(result)`:

    {"title", "storage_key", "meta": {...}, "preamble": "<first lines of the prompt>",
     "recompute": {...what the page POSTs to recompute invocations...},
     "invocations": <the result's own recommended_invocations>,
     "changes": [   # removed product strings that some test step matched, in file/line order
        {"id", "file", "line", "column", "text", "diff_url", "replacement": "new text" | None,
         "groups": [   # one per feature file, in path order
            {"id", "file", "feature_url", "entries": [
               {"id", "case_id", "case_url", "scenario_name", "line", "step_url",
                "step": {"before", "literal", "after"}, "tags": [{"text", "url"}],
                "confidence", "exact" (a case-sensitive match of an `exact`-algorithm literal),
                "prompt_line" (None: the text did not change, so the prompt omits it)}, ...]}, ...]}, ...],
     "unmatched": [{"file", "line", "column", "text", "diff_url", "replacement"}, ...]}
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import quote

from .. import matching
from . import config
from .replacements import chosen_replacement

_GITHUB_REPO_RE = re.compile(
    r"^(?:https?://(?:[^@/]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)(?P<slug>[^/]+/[^/]+?)(?:\.git)?/?$"
)
_TESTRAIL_TAG_RE = re.compile(r"^testrail\((\d+)\)$")
# How many characters of a SHA to show in prose.
_SHORT_SHA = 12


def github_repo_url(repo: str) -> str | None:
    """`https://github.com/<owner>/<repo>` for a GitHub remote in any of its usual spellings
    (https, ssh://, scp-like, with or without `.git`), else None."""
    match = _GITHUB_REPO_RE.match(repo.strip())
    return f"https://github.com/{match['slug']}" if match else None


def diff_url(
    repo_url: str | None, file: str, line: int, base_ref: str | None, updated_ref: str | None, pr_number: int | None
) -> str | None:
    """The file's section of the diff, scrolled to the (removed, so left-hand) `line`. GitHub
    anchors a file in a diff as `diff-` + the SHA-256 of its path, and a line in it as `L<n>`
    (left side) or `R<n>` (right side)."""
    if repo_url is None:
        return None
    anchor = f"#diff-{hashlib.sha256(file.encode()).hexdigest()}L{line}"
    if pr_number is not None:
        return f"{repo_url}/pull/{pr_number}/files{anchor}"
    if base_ref and updated_ref:
        return f"{repo_url}/compare/{base_ref}...{updated_ref}{anchor}"
    return None


def blob_url(repo_url: str | None, sha: str, path: str, line: int | None = None) -> str | None:
    if repo_url is None:
        return None
    url = f"{repo_url}/blob/{sha}/{quote(path)}"
    return url if line is None else f"{url}#L{line}"


def ref_url(repo_url: str | None, base_ref: str | None, updated_ref: str | None, pr_number: int | None) -> str | None:
    """The pull request, or else the comparison, that the literals diff came from."""
    if repo_url is None:
        return None
    if pr_number is not None:
        return f"{repo_url}/pull/{pr_number}"
    if base_ref and updated_ref:
        return f"{repo_url}/compare/{base_ref}...{updated_ref}"
    return None


def tree_url(repo_url: str | None, sha: str) -> str | None:
    """GitHub's browse page for the repo as of `sha`."""
    return None if repo_url is None else f"{repo_url}/tree/{sha}"


def is_exact_match(literal: dict, text: str) -> bool:
    """Whether the literal equals the changed `text` (modulo whitespace) with the same casing.
    A literal matched case-insensitively, or by containment, is not. This is not the same as
    a confidence of 1.0: a `data` literal is an exact match at a flat, lower confidence."""
    return literal["match_algorithm"] == matching.EXACT and matching.normalize_whitespace(
        literal["value"]
    ) == matching.normalize_whitespace(text)


def testrail_case_url(case_id: int) -> str:
    return f"{config.TESTRAIL_URL}/index.php?/cases/view/{case_id}"


def split_step(step_text: str, literal: str) -> dict:
    """The step with the first (case-insensitive) occurrence of the matched literal split out so
    the page can highlight it. A literal that isn't visible in the step text -- one a templated
    step supplies -- leaves the step whole."""
    start = step_text.lower().find(literal.lower()) if literal else -1
    if start < 0:
        return {"before": step_text, "literal": "", "after": ""}
    end = start + len(literal)
    return {"before": step_text[:start], "literal": step_text[start:end], "after": step_text[end:]}


def prompt_line(old: str, new: str | None, literal: dict) -> str | None:
    """One bullet of the coding-agent prompt for one affected step, or None when the text did
    not actually change (`old == new`), so there is nothing for the agent to adjust. A literal
    compared with `exact` is the whole label, so the new value simply replaces it; a freeform
    (substring) literal is only part of the product text, so the whole old -> new change is
    stated and the agent works out what the step should now say."""
    if new == old:
        return None
    where = (
        f"the step `{literal['step_text']}` in `{literal['source_file']}` "
        f"(line {literal['source_line']}, scenario \"{literal['scenario_name']}\", C{literal['case_id']})"
    )
    if new is None:
        return f'- "{old}" was removed from the product and no replacement was detected; it is used in {where}'
    if literal["match_algorithm"] == "exact":
        return f'- "{old}" is now "{new}" in {where}'
    return f'- The product text "{old}" is now "{new}"; {where} checks for "{literal["value"]}"'


def _tags(tags: list[str]) -> list[dict]:
    out = []
    for tag in tags:
        match = _TESTRAIL_TAG_RE.match(tag)
        out.append({"text": tag, "url": testrail_case_url(int(match[1])) if match else None})
    return out


def _preamble(result: dict) -> str:
    if result.get("pr_number") is not None:
        diff = f"pull request #{result['pr_number']} of {result['literals_repo']}"
    else:
        diff = f"{result['literals_repo']} {str(result['base_ref'])[:_SHORT_SHA]}...{str(result['updated_ref'])[:_SHORT_SHA]}"
    return f"The following strings have changed in the product ({diff}), and these test steps need to be updated accordingly:"


def build_report(result: dict) -> dict:
    literals_url = github_repo_url(result["literals_repo"])
    test_url = github_repo_url(result["test_repo"])
    base_ref, updated_ref, pr_number = result.get("base_ref"), result.get("updated_ref"), result.get("pr_number")

    def finding_diff_url(finding: dict) -> str | None:
        return diff_url(literals_url, finding["file"], finding["line"], base_ref, updated_ref, pr_number)

    removed = sorted(
        (f for f in result["findings"] if f["change"] == "removed"),
        key=lambda f: (f["file"], f["line"], f["column"]),
    )
    changes, unmatched = [], []
    for finding in removed:
        replacement = chosen_replacement(finding)
        new_text = replacement["text"] if replacement else None
        change_id = f"{finding['file']}:{finding['line']}:{finding['column']}"
        matches = finding["matched_test_literals"]
        if not matches:
            unmatched.append(
                {
                    "file": finding["file"],
                    "line": finding["line"],
                    "column": finding["column"],
                    "text": finding["text"],
                    "diff_url": finding_diff_url(finding),
                    "replacement": new_text,
                }
            )
            continue

        groups: dict[str, list[dict]] = {}
        seen_ids: dict[str, int] = {}
        for literal in sorted(matches, key=lambda m: (m["source_file"], m["source_line"], m["case_id"])):
            entry_id = f"{change_id}|{literal['case_id']}|{literal['source_file']}:{literal['source_line']}"
            seen_ids[entry_id] = seen_ids.get(entry_id, 0) + 1
            if seen_ids[entry_id] > 1:  # the same step matched by two literals of one finding
                entry_id += f"#{seen_ids[entry_id]}"
            groups.setdefault(literal["source_file"], []).append(
                {
                    "id": entry_id,
                    "case_id": literal["case_id"],
                    "case_url": testrail_case_url(literal["case_id"]),
                    "scenario_name": literal["scenario_name"],
                    "line": literal["source_line"],
                    "step_url": blob_url(test_url, result["test_sha"], literal["source_file"], literal["source_line"]),
                    "step": split_step(literal["step_text"], literal["value"]),
                    "tags": _tags(literal["tags"]),
                    "confidence": min(finding["confidence"], literal["match_confidence"]),
                    "exact": is_exact_match(literal, finding["text"]),
                    "prompt_line": prompt_line(finding["text"], new_text, literal),
                }
            )
        changes.append(
            {
                "id": change_id,
                "file": finding["file"],
                "line": finding["line"],
                "column": finding["column"],
                "text": finding["text"],
                "diff_url": finding_diff_url(finding),
                "replacement": new_text,
                "groups": [
                    {
                        "id": f"{change_id}|{path}",
                        "file": path,
                        "feature_url": blob_url(test_url, result["test_sha"], path),
                        "entries": entries,
                    }
                    for path, entries in groups.items()
                ],
            }
        )

    return {
        "title": "Impact report",
        "storage_key": f"e2e-test-literals-report:{result['test_sha']}:{base_ref}:{updated_ref}",
        "meta": {
            "literals_repo": result["literals_repo"],
            "test_repo": result["test_repo"],
            "test_sha": result["test_sha"],
            "base_ref": base_ref,
            "updated_ref": updated_ref,
            "pr_number": pr_number,
            "literals_url": literals_url,
            "test_url": test_url,
            "literals_ref_url": ref_url(literals_url, base_ref, updated_ref, pr_number),
            "test_sha_url": tree_url(test_url, result["test_sha"]),
        },
        "preamble": _preamble(result),
        "recompute": {
            "test_sha": result["test_sha"],
            "invocation_max_length": result["invocation_max_length"],
            "extra_cases_allowed": result["extra_cases_allowed"],
            "missing_cases_allowed": result["missing_cases_allowed"],
            "min_invocation_confidence": result["min_invocation_confidence"],
        },
        "invocations": result["recommended_invocations"],
        "changes": changes,
        "unmatched": unmatched,
    }
