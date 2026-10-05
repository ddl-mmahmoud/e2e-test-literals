"""Orchestrates the "did a removed product literal break a test" check (see
CHANGED-LITERALS-IMPACT-PLAN.md): ensure the test repo's revision is built, ask
changed-literals to diff two refs of a (possibly different) target repo, then check
which of its REMOVED findings -- at or above a confidence floor -- contain a literal
this test revision actually depends on.

This is the "actual work" counterpart to `impact_jobs.py`'s job-state-machine wrapper,
the same split `generation.py`/`jobs.py` use for revision builds.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Literal

from rapidfuzz import fuzz

from . import changed_literals_client as cl_client
from .generation import db_path, resolve_and_generate

DEFAULT_MIN_REMOVAL_CONFIDENCE = 0.85
DEFAULT_MIN_LITERAL_MATCH_CONFIDENCE = 0.9

# "substring": plain case-insensitive containment, `match_confidence` always 1.0 --
#   the old exact-containment check, for callers who don't want fuzziness at all.
# "partial_ratio": rapidfuzz score only, no containment required -- a literal that
#   merely *resembles* some window of the text (shared words, a stray typo) can match.
# "substring_and_partial_ratio" (default): containment is a hard gate, `partial_ratio`
#   only grades the confidence of matches that already passed it (see `_match_literals`
#   for why pure `partial_ratio` alone is too permissive).
MatchMethod = Literal["substring", "partial_ratio", "substring_and_partial_ratio"]
DEFAULT_MATCH_METHOD: MatchMethod = "substring_and_partial_ratio"
_MATCH_METHODS: tuple[MatchMethod, ...] = ("substring", "partial_ratio", "substring_and_partial_ratio")

_POLL_INTERVAL_SECONDS = 1.0
_RESULT_PAGE_SIZE = 2000  # changed-literals' own MAX_PAGE_SIZE

# Below this length, rapidfuzz's partial_ratio has no room for partial credit -- it
# degenerates to a binary 0-or-100 result -- so it can't express the graded confidence
# a match is meant to carry, and a bare 1-2 char/word literal is exactly the case that
# motivated moving off plain substring matching (spurious hits in unrelated text). Also
# applied under the "substring" method, for the same reason: a 1-2 char/word literal is
# a near-guaranteed spurious containment hit in unrelated text, fuzzy or not.
# Literals shorter than this never match, regardless of confidence or match_method.
_MIN_LITERAL_LENGTH_FOR_MATCHING = 3


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
    match_method: MatchMethod = DEFAULT_MATCH_METHOD,
) -> list[dict]:
    """Three interchangeable notions of "literal matches text" (see `MatchMethod`):

    - "substring": `literal["value"]` must appear inside `text` (case-insensitively).
      Binary -- every match gets `match_confidence` 1.0, so `min_confidence` is moot.
    - "partial_ratio": rapidfuzz's `partial_ratio` alone, the best-aligning edit
      distance between `value` and some window of `text`, normalized to 0-1. No
      containment required, so a literal that merely *resembles* some window of `text`
      (shared words, nearby phrasing, a stray typo) can match without truly occurring
      in it -- useful for catching paraphrased/rewritten findings, noisier otherwise.
    - "substring_and_partial_ratio" (the default): containment is a hard gate, and
      `partial_ratio` only grades the confidence of matches that already passed it
      (case differences and the like still cost points -- see
      `test_match_tolerates_minor_noise_above_confidence_floor`). Gives a caller a
      `match_confidence` that means something rather than one that's trivially 1.0
      whenever the gate passes, while still ruling out resemblance-only matches.

    Note `text` is already `.strip()`'d and truncated to 200 chars by changed-literals
    itself (its `Finding.text`) -- a test literal longer than that truncation point
    could produce a false negative here. Known, accepted limitation for v1 (see
    CHANGED-LITERALS-IMPACT-PLAN.md Q4); revisit only if this causes real misses in
    practice.

    `partial_ratio` itself is direction-agnostic: given two strings of different
    lengths, it always searches the *shorter* one inside the longer one, regardless of
    argument order -- there's no "needle vs haystack" distinction at that layer. Plenty
    of findings have short `text` (changed-literals often reports a single word/short
    label), while test literals are frequently full sentences/labels, so without a
    length guard a literal *longer* than `text` would silently flip the check into "is
    this short finding text contained somewhere in this long literal" -- the opposite
    of what we want (and a near-guaranteed spurious match for any common short word).
    Skip those pairs outright, same as a containment check would anyway
    (`value in text` is trivially `False` whenever `value` is longer than `text`)."""
    matches = []
    for literal in test_literals:
        value = literal["value"]
        if len(value) < _MIN_LITERAL_LENGTH_FOR_MATCHING or len(value) > len(text):
            continue

        if match_method != "partial_ratio" and value.casefold() not in text.casefold():
            continue

        confidence = 1.0 if match_method == "substring" else fuzz.partial_ratio(value, text) / 100.0
        if confidence >= min_confidence:
            matches.append({**literal, "match_confidence": confidence})
    return matches


def _poll_changed_literals_job(job_id: str, auth_header: str | None) -> dict:
    """Blocks (on this job's own worker thread, see impact_jobs.py) until the remote
    changed-literals job reaches done/error. No separate timeout of our own for now --
    matches this codebase's existing style of not imposing one on long-running
    upstream work (e.g. proxy.py's streaming requests have none either)."""
    while True:
        status = cl_client.get_job_status(job_id, auth_header=auth_header)
        if status["status"] in ("done", "error"):
            return status
        time.sleep(_POLL_INTERVAL_SECONDS)


def _fetch_all_findings(job_id: str, auth_header: str | None = None) -> list[dict]:
    """Loop-fetches every page of the remote job's result into memory before
    filtering/matching. changed-literals' own finding counts are per-diff (hundreds,
    not millions) so this is a non-issue today -- if a real-world diff ever makes this
    expensive, switch to filtering/matching page-by-page as each page arrives instead
    of collecting the full list upfront first."""
    findings: list[dict] = []
    offset = 0
    while True:
        page = cl_client.get_result_page(job_id, offset=offset, limit=_RESULT_PAGE_SIZE, auth_header=auth_header)
        page_findings = page["findings"]
        findings.extend(page_findings)
        offset += len(page_findings)
        if not page_findings or offset >= page["total"]:
            return findings


def compute_impact(
    test_repo: str,
    test_ref: str,
    literals_repo: str,
    base_ref: str,
    updated_ref: str,
    min_removal_confidence: float = DEFAULT_MIN_REMOVAL_CONFIDENCE,
    min_literal_match_confidence: float = DEFAULT_MIN_LITERAL_MATCH_CONFIDENCE,
    match_method: MatchMethod = DEFAULT_MATCH_METHOD,
    auth_header: str | None = None,
) -> dict:
    """The actual unit of work for a `POST /changed-literals-impact` job (see
    impact_jobs.py). Runs on the job's own worker thread; nothing here is async.

    Only `change == "removed"` findings at or above `min_removal_confidence` are
    returned -- this is a *second*, client-side filter on top of changed-literals' own
    internal floor of 0.3 (it never returns anything below that), not a replacement for
    it. Each returned finding is annotated with `matched_test_literals` (possibly
    empty) rather than only including findings that actually matched, so a caller can
    see exactly which removed-and-confident findings were checked. `matched_test_literals`
    entries are only those at or above `min_literal_match_confidence` under `match_method`
    (see `_match_literals` and `MatchMethod`).

    `auth_header` is the original caller's own `Authorization` header value, forwarded
    unchanged to every changed-literals call: changed-literals itself is unauthenticated,
    but Domino's app-proxy gateway in front of it isn't, and 302-redirects any call that
    doesn't carry it to an SSO login page instead of reaching the app (see
    changed_literals_client.py).
    """
    if match_method not in _MATCH_METHODS:
        raise ValueError(f"match_method must be one of {_MATCH_METHODS}, got {match_method!r}")

    test_sha = resolve_and_generate(test_repo, test_ref)
    test_literals = _load_test_literals(test_sha)

    created = cl_client.create_job(literals_repo, base_ref, updated_ref, auth_header=auth_header)
    status = _poll_changed_literals_job(created["job_id"], auth_header)
    if status["status"] == "error":
        error = status.get("error") or "changed-literals job failed"
        print(f"changed-literals: remote job {created['job_id']} itself reported status=error: {error[:500]!r}")
        raise cl_client.ChangedLiteralsError(error)

    findings = []
    for finding in _fetch_all_findings(created["job_id"], auth_header):
        if finding["change"] != "removed" or finding["confidence"] < min_removal_confidence:
            continue
        matched = _match_literals(finding["text"], test_literals, min_literal_match_confidence, match_method)
        findings.append({**finding, "matched_test_literals": matched})

    return {
        "test_repo": test_repo,
        "test_ref": test_ref,
        "test_sha": test_sha,
        "literals_repo": literals_repo,
        "base_ref": base_ref,
        "updated_ref": updated_ref,
        "min_removal_confidence": min_removal_confidence,
        "min_literal_match_confidence": min_literal_match_confidence,
        "match_method": match_method,
        "total": len(findings),
        "findings": findings,
    }
