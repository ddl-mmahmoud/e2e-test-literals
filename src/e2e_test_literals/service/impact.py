"""Orchestrates the "did a removed product literal break a test" check (see
CHANGED-LITERALS-IMPACT-PLAN.md): ensure the test repo's revision is built, ask
changed-literals to diff two refs of a (possibly different) target repo, then check
which of its REMOVED findings -- at or above a confidence floor -- contain a literal
this test revision actually depends on. Added findings ride along so each removal can be
paired with what replaced it (`replacements.py`).

This is the "actual work" counterpart to `impact_jobs.py`'s job-state-machine wrapper,
the same split `generation.py`/`jobs.py` use for revision builds.
"""

from __future__ import annotations

import sqlite3
import time

from .. import matching
from . import changed_literals_client as cl_client
from .generation import db_path, resolve_and_generate
from .invocations import DEFAULT_MAX_LENGTH as DEFAULT_INVOCATION_MAX_LENGTH
from .invocations import recommend_invocations
from .replacements import attach_replacements

DEFAULT_MIN_REMOVAL_CONFIDENCE = 0.85
# Default 0.0: every case a removed finding matched gets an invocation.
DEFAULT_MIN_INVOCATION_CONFIDENCE = 0.0

DEFAULT_MIN_LITERAL_MATCH_CONFIDENCE = matching.DEFAULT_MIN_LITERAL_MATCH_CONFIDENCE

_POLL_INTERVAL_SECONDS = 1.0
_RESULT_PAGE_SIZE = 2000  # changed-literals' own MAX_PAGE_SIZE


def _load_test_literals(sha: str) -> list[dict]:
    """Every literal this test revision depends on, loaded into memory once per job --
    the per-revision sqlite is small (db.py), so matching can be a plain in-process
    fuzzy comparison (see `_match_literals`) rather than one SQL round-trip per
    candidate finding."""
    conn = sqlite3.connect(db_path(sha))
    try:
        conn.row_factory = sqlite3.Row
        literal_rows = conn.execute(
            """
            SELECT literals.value AS value,
                   literals.string_kind AS string_kind,
                   literals.case_id AS case_id,
                   literals.source_line AS source_line,
                   literals.step_text AS step_text,
                   source_files.path AS source_file,
                   cases.scenario_name AS scenario_name
            FROM literals
            JOIN cases ON cases.id = literals.case_id
            JOIN source_files ON source_files.id = literals.source_file
            """
        ).fetchall()
        tag_rows = conn.execute("SELECT case_id, tag FROM tags").fetchall()
    finally:
        conn.close()

    tags_by_case: dict[int, list[str]] = {}
    for row in tag_rows:
        tags_by_case.setdefault(row["case_id"], []).append(row["tag"])

    return [
        {
            "value": row["value"],
            "string_kind": row["string_kind"],
            "case_id": row["case_id"],
            "scenario_name": row["scenario_name"],
            "source_file": row["source_file"],
            "source_line": row["source_line"],
            "step_text": row["step_text"],
            "tags": tags_by_case.get(row["case_id"], []),
        }
        for row in literal_rows
    ]


def _match_literals(
    text: str,
    test_literals: list[dict],
    min_confidence: float = DEFAULT_MIN_LITERAL_MATCH_CONFIDENCE,
) -> list[dict]:
    """Every test literal that matches `text` (a removed product string) at or above
    `min_confidence`. How a literal is compared depends on its `string_kind` -- button/link
    labels must equal `text`, freeform text only has to occur in it, etc. -- see
    `matching.match_confidence` and step_kinds.py. Each match records the `string_kind` and
    the `match_algorithm` that was applied.

    Note `text` is already `.strip()`'d and truncated to 200 chars by changed-literals
    itself (its `Finding.text`) -- a test literal longer than that truncation point
    could produce a false negative here. Known, accepted limitation for v1 (see
    CHANGED-LITERALS-IMPACT-PLAN.md Q4); revisit only if this causes real misses in
    practice."""
    matches = []
    for literal in test_literals:
        matched = matching.match_confidence(literal["value"], text, literal["string_kind"])
        if matched is None:
            continue
        algorithm, confidence = matched
        if confidence >= min_confidence:
            matches.append({**literal, "match_algorithm": algorithm, "match_confidence": confidence})
    return matches


def _poll_changed_literals_job(job_id: str) -> dict:
    """Blocks (on this job's own worker thread, see impact_jobs.py) until the remote
    changed-literals job reaches done/error. No separate timeout of our own for now --
    matches this codebase's existing style of not imposing one on long-running
    upstream work (e.g. proxy.py's streaming requests have none either)."""
    while True:
        status = cl_client.get_job_status(job_id)
        if status["status"] in ("done", "error"):
            return status
        time.sleep(_POLL_INTERVAL_SECONDS)


def _fetch_all_findings(job_id: str) -> list[dict]:
    """Loop-fetches every page of the remote job's result into memory before
    filtering/matching. changed-literals' own finding counts are per-diff (hundreds,
    not millions) so this is a non-issue today -- if a real-world diff ever makes this
    expensive, switch to filtering/matching page-by-page as each page arrives instead
    of collecting the full list upfront first."""
    findings: list[dict] = []
    offset = 0
    while True:
        page = cl_client.get_result_page(job_id, offset=offset, limit=_RESULT_PAGE_SIZE)
        page_findings = page["findings"]
        findings.extend(page_findings)
        offset += len(page_findings)
        if not page_findings or offset >= page["total"]:
            return findings


def _case_ids_for_invocations(findings: list[dict], min_invocation_confidence: float) -> list[int]:
    """Case IDs of every matched test literal whose confidence clears the bar. A match's
    confidence is the weaker of its two links -- how sure changed-literals is that the
    text was removed (`finding["confidence"]`) and how well the literal matched that text
    (`match_confidence`) -- so neither a shaky removal nor a shaky match alone can pull a
    case into a recommendation that the caller asked to be stricter about."""
    return sorted(
        {
            literal["case_id"]
            for finding in findings
            if finding["change"] == "removed"
            for literal in finding["matched_test_literals"]
            if min(finding["confidence"], literal["match_confidence"]) >= min_invocation_confidence
        }
    )


def compute_impact(
    test_repo: str,
    test_ref: str,
    literals_repo: str,
    base_ref: str | None,
    updated_ref: str | None,
    min_removal_confidence: float = DEFAULT_MIN_REMOVAL_CONFIDENCE,
    min_literal_match_confidence: float = DEFAULT_MIN_LITERAL_MATCH_CONFIDENCE,
    min_invocation_confidence: float = DEFAULT_MIN_INVOCATION_CONFIDENCE,
    invocation_max_length: int = DEFAULT_INVOCATION_MAX_LENGTH,
    extra_cases_allowed: int | None = None,
    missing_cases_allowed: int | None = None,
    pr_number: int | None = None,
) -> dict:
    """The actual unit of work for a `POST /changed-literals-impact` job (see
    impact_jobs.py). Runs on the job's own worker thread; nothing here is async.

    `findings` holds every detected change: the `change == "removed"` ones at or above
    `min_removal_confidence` -- this is a *second*, client-side filter on top of
    changed-literals' own internal floor of 0.3 (it never returns anything below that), not
    a replacement for it -- plus all `"added"` ones, which can't break a test but are the
    candidates for what a removed string became. Each removed finding is annotated with
    `matched_test_literals` (possibly empty) rather than only including findings that
    actually matched, so a caller can see exactly which removed-and-confident findings were
    checked, and with `replacements` (see `replacements.attach_replacements`). `total` counts
    every finding. A removed finding's `matched_test_literals`
    entries are only those at or above `min_literal_match_confidence` under the match
    algorithm their literal's `string_kind` calls for (see `_match_literals`).

    `recommended_invocations` (see `invocations.recommend_invocations`) lists the
    `/test-e2e` lines that cover every matched case whose confidence is at least
    `min_invocation_confidence` (see `_case_ids_for_invocations`), each line split to at
    most `invocation_max_length` characters (0 = no limit). If either of
    `extra_cases_allowed` / `missing_cases_allowed` is given, shared scenario tags replace
    `testrail(N)` tags within those bounds (the other is then unbounded); if both are None,
    tags are never substituted. These only shape `recommended_invocations`: the test db and
    the changed-literals findings are fetched exactly as without them.

    The literals diff is named either by `base_ref`/`updated_ref` or by `pr_number`, a
    GitHub pull request of `literals_repo` (then both refs are None): changed-literals
    resolves it to the PR's base and branch-tip SHAs, which the result reports as
    `base_ref`/`updated_ref`.
    """
    test_sha = resolve_and_generate(test_repo, test_ref)
    test_literals = _load_test_literals(test_sha)

    created = cl_client.create_job(literals_repo, base_ref, updated_ref, pr_number=pr_number)
    status = _poll_changed_literals_job(created["job_id"])
    if status["status"] == "error":
        error = status.get("error") or "changed-literals job failed"
        print(f"changed-literals: remote job {created['job_id']} itself reported status=error: {error[:500]!r}")
        raise cl_client.ChangedLiteralsError(error)
    base_ref, updated_ref = status.get("base", base_ref), status.get("updated", updated_ref)

    findings = []
    for finding in _fetch_all_findings(created["job_id"]):
        if finding["change"] != "removed":
            findings.append(finding)  # a candidate replacement; see attach_replacements
        elif finding["confidence"] >= min_removal_confidence:
            matched = _match_literals(finding["text"], test_literals, min_literal_match_confidence)
            findings.append({**finding, "matched_test_literals": matched})
    findings = attach_replacements(findings)

    return {
        "test_repo": test_repo,
        "test_ref": test_ref,
        "test_sha": test_sha,
        "literals_repo": literals_repo,
        "base_ref": base_ref,
        "updated_ref": updated_ref,
        "pr_number": pr_number,
        "min_removal_confidence": min_removal_confidence,
        "min_literal_match_confidence": min_literal_match_confidence,
        "min_invocation_confidence": min_invocation_confidence,
        "invocation_max_length": invocation_max_length,
        "extra_cases_allowed": extra_cases_allowed,
        "missing_cases_allowed": missing_cases_allowed,
        "total": len(findings),
        "findings": findings,
        "recommended_invocations": recommend_invocations(
            db_path(test_sha),
            _case_ids_for_invocations(findings, min_invocation_confidence),
            max_length=invocation_max_length,
            extra_cases_allowed=extra_cases_allowed,
            missing_cases_allowed=missing_cases_allowed,
        ),
    }
