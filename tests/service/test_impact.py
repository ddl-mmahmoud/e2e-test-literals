"""Unit tests for service/impact.py -- changed-literals and the test revision build are
both faked out (Q7 of CHANGED-LITERALS-IMPACT-PLAN.md: assume the documented HTTP
contract and mock it, no real changed-literals instance needed), covering the
filter/fuzzy-match orchestration logic itself."""

from __future__ import annotations

import pytest

from e2e_test_literals.db import write_sqlite
from e2e_test_literals.service import changed_literals_client as cl_client
from e2e_test_literals.service import config, impact
from e2e_test_literals.service.profiling import PHASES, PhaseTracker


def _write_test_db(
    sha: str,
    *,
    case_id: int = 1,
    value: str = "Saved successfully",
    string_kind: str = "text",
    tags=("smoke",),
    routing=None,
) -> None:
    cases = {
        case_id: {
            "scenarios": [
                {
                    "feature_file": "tests/ui/features/example.feature",
                    "scenario_name": "Save a widget",
                    "tags": list(tags),
                }
            ],
            "literals": [
                {
                    "value": value,
                    "situation": "argument",
                    "string_kind": string_kind,
                    "dynamic": False,
                    "source_kind": "direct",
                    "source_file": "tests/ui/features/example.feature",
                    "source_line": 5,
                    "step_text": f'Then I click the button "{value}"',
                }
            ],
            "patterns": [],
        }
    }
    write_sqlite(cases, config.DB_DIR / f"{sha}.sqlite", routing=routing)


def _cucu_entry(case_id: int) -> dict:
    return {
        "case_id": case_id,
        "domain": "ui",
        "file": "tests/ui/features/example.feature",
        "name": "Save a widget",
        "root": "domino",
        "labels": [["tag", "smoke"]],
        "candidates": [{"test_type": "cucu", "flags": []}],
    }


@pytest.fixture(autouse=True)
def _impact_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_DIR", tmp_path / "db")
    config.DB_DIR.mkdir(parents=True)


def _finding(**overrides) -> dict:
    base = {
        "file": "src/Widget.tsx",
        "line": 10,
        "column": 2,
        "text": "Saved successfully",
        "context": "jsx-text",
        "confidence": 0.95,
        "change": "removed",
    }
    base.update(overrides)
    return base


def _stub_changed_literals(monkeypatch, findings: list[dict]) -> None:
    monkeypatch.setattr(cl_client, "create_job", lambda repo, base, updated, **k: {"job_id": "job-1"})
    monkeypatch.setattr(cl_client, "get_job_status", lambda job_id, **k: {"status": "done"})
    monkeypatch.setattr(
        cl_client,
        "get_result_page",
        lambda job_id, *, offset=0, limit=2000, **k: {
            "offset": offset,
            "limit": limit,
            "total": len(findings),
            "findings": findings if offset == 0 else [],
        },
    )


def test_matches_removed_literal_above_confidence(monkeypatch):
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref, **k: "deadbeef")
    _write_test_db("deadbeef")
    _stub_changed_literals(monkeypatch, [_finding()])

    result = impact.compute_impact("test-repo", "main", "product-repo", "base", "updated")

    assert result["test_sha"] == "deadbeef"
    assert result["min_removal_confidence"] == impact.DEFAULT_MIN_REMOVAL_CONFIDENCE
    assert result["min_literal_match_confidence"] == impact.DEFAULT_MIN_LITERAL_MATCH_CONFIDENCE
    assert result["total"] == 1
    finding = result["findings"][0]
    assert finding["change"] == "removed"
    assert len(finding["matched_test_literals"]) == 1
    match = finding["matched_test_literals"][0]
    assert match["value"] == "Saved successfully"
    assert match["scenario_name"] == "Save a widget"
    assert match["source_file"] == "tests/ui/features/example.feature"
    assert match["tags"] == ["smoke"]
    assert match["match_confidence"] == 0.8  # clean containment: substring matches are scaled by 0.8


def test_drops_findings_below_min_confidence(monkeypatch):
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref, **k: "deadbeef")
    _write_test_db("deadbeef")
    _stub_changed_literals(monkeypatch, [_finding(confidence=0.5)])

    result = impact.compute_impact("test-repo", "main", "product-repo", "base", "updated")

    assert result["findings"] == []


def test_added_findings_are_kept_unmatched_and_do_not_drive_invocations(monkeypatch):
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref, **k: "deadbeef")
    _write_test_db("deadbeef")
    added = _finding(change="added", text="Saved successfully", confidence=0.3)
    _stub_changed_literals(monkeypatch, [added])

    result = impact.compute_impact("test-repo", "main", "product-repo", "base", "updated")

    # Not held to min_removal_confidence, and never annotated like a removal.
    assert result["findings"] == [added]
    assert result["total"] == 1
    assert result["recommended_invocations"]["commands"] == []


def test_removed_finding_gets_its_added_counterpart_from_the_same_hunk(monkeypatch):
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref, **k: "deadbeef")
    _write_test_db("deadbeef")
    removed = _finding(text="Saved successfully", hunk=0)
    added = _finding(change="added", text="Saved!", line=11, hunk=0)
    unrelated = _finding(change="added", text="Elsewhere", line=90, hunk=3)
    _stub_changed_literals(monkeypatch, [removed, added, unrelated])

    result = impact.compute_impact("test-repo", "main", "product-repo", "base", "updated")

    by_text = {f["text"]: f for f in result["findings"]}
    assert result["total"] == 3
    assert [r["text"] for r in by_text["Saved successfully"]["replacements"]] == ["Saved!"]
    assert by_text["Saved successfully"]["replacements"][0]["chosen"] is True
    assert "replacements" not in by_text["Saved!"]


def test_a_string_removed_and_re_added_unchanged_in_one_hunk_is_not_reported(monkeypatch):
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref, **k: "deadbeef")
    _write_test_db("deadbeef")
    removed = _finding(text="Saved successfully", hunk=0)
    added = _finding(change="added", text="Saved successfully", line=11, hunk=0)
    _stub_changed_literals(monkeypatch, [removed, added])

    result = impact.compute_impact("test-repo", "main", "product-repo", "base", "updated")

    assert result["findings"] == []
    assert result["recommended_invocations"]["commands"] == []


def test_removed_finding_with_no_substring_match_has_empty_match_list(monkeypatch):
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref, **k: "deadbeef")
    _write_test_db("deadbeef")
    _stub_changed_literals(monkeypatch, [_finding(text="Something unrelated")])

    result = impact.compute_impact("test-repo", "main", "product-repo", "base", "updated")

    assert result["total"] == 1
    assert result["findings"][0]["matched_test_literals"] == []


def test_respects_custom_min_removal_confidence(monkeypatch):
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref, **k: "deadbeef")
    _write_test_db("deadbeef")
    _stub_changed_literals(monkeypatch, [_finding(confidence=0.6)])

    result = impact.compute_impact("test-repo", "main", "product-repo", "base", "updated", min_removal_confidence=0.5)

    assert result["total"] == 1


def test_literal_longer_than_finding_text_never_matches(monkeypatch):
    # rapidfuzz's partial_ratio is direction-agnostic -- given two strings of
    # different lengths it always searches the *shorter* one inside the longer one,
    # regardless of argument order. Without a length guard, a literal longer than the
    # finding's (often short/single-word) `text` would flip the check into "is this
    # short finding text contained in this long literal" -- the opposite of the
    # intended "does the finding's text contain this literal" semantics, and a
    # near-guaranteed spurious match for any literal that happens to share a common
    # word with the finding.
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref, **k: "deadbeef")
    _write_test_db("deadbeef", value="Access Forbidden")
    _stub_changed_literals(monkeypatch, [_finding(text="Access")])

    result = impact.compute_impact("test-repo", "main", "product-repo", "base", "updated")

    assert result["findings"][0]["matched_test_literals"] == []


def test_match_tolerates_minor_noise_above_confidence_floor(monkeypatch):
    # A single-character difference (here, a case change) still scores well above the
    # default floor -- the fuzzy match is meant to tolerate exactly this kind of minor
    # drift, unlike the old exact substring check.
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref, **k: "deadbeef")
    _write_test_db("deadbeef", value="Saved successfully")
    _stub_changed_literals(monkeypatch, [_finding(text="saved successfully (lowercase, different text)")])

    result = impact.compute_impact("test-repo", "main", "product-repo", "base", "updated")

    assert len(result["findings"][0]["matched_test_literals"]) == 1


def test_loose_containment_inside_a_longer_word_falls_below_the_default_floor(monkeypatch):
    # "Save" is contained (case-insensitively) in "Unsaved", but only loosely: the case
    # mismatch costs it points (partial_ratio 75%, then scaled by 0.8 for being a substring
    # match), so it needs a loosened floor to be reported.
    text = "Unsaved changes will be lost"
    assert _matches(monkeypatch, value="Save", string_kind="text", text=text) == []
    (match,) = _matches(monkeypatch, value="Save", string_kind="text", text=text, min_literal_match_confidence=0.6)
    assert match["match_confidence"] == pytest.approx(0.6)


def test_high_confidence_without_true_containment_never_matches(monkeypatch):
    # A one-character typo inside an otherwise-matching window scores well above the
    # default confidence floor via partial_ratio alone (92.9%), but the literal never
    # actually occurs in the text -- containment is a hard gate, not just another signal
    # confidence can outweigh.
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref, **k: "deadbeef")
    _write_test_db("deadbeef", value="Delete project")
    _stub_changed_literals(monkeypatch, [_finding(text="prefix Delete prbject suffix text padding here")])

    result = impact.compute_impact("test-repo", "main", "product-repo", "base", "updated")

    assert result["findings"][0]["matched_test_literals"] == []


def test_short_literal_never_matches(monkeypatch):
    # Below _MIN_LITERAL_LENGTH_FOR_MATCHING, partial_ratio can only ever return 0 or
    # 100 -- no graded confidence to filter on -- so these are excluded outright rather
    # than auto-matching any text that happens to contain them.
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref, **k: "deadbeef")
    _write_test_db("deadbeef", value="OK")
    _stub_changed_literals(monkeypatch, [_finding(text="OK, continuing now")])

    result = impact.compute_impact("test-repo", "main", "product-repo", "base", "updated")

    assert result["findings"][0]["matched_test_literals"] == []


def test_respects_custom_min_literal_match_confidence(monkeypatch):
    # The literal actually occurs in the text (case-insensitively), so it clears the
    # containment gate either way -- but the case mismatch still costs it confidence
    # points (86%, scaled by 0.8 to 0.69), so it only clears the default 0.7 floor once
    # that floor is loosened.
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref, **k: "deadbeef")
    _write_test_db("deadbeef", value="Delete Project")
    _stub_changed_literals(monkeypatch, [_finding(text="Are you sure you want to delete project now?")])

    default_result = impact.compute_impact("test-repo", "main", "product-repo", "base", "updated")
    assert default_result["findings"][0]["matched_test_literals"] == []

    loose_result = impact.compute_impact(
        "test-repo", "main", "product-repo", "base", "updated", min_literal_match_confidence=0.5
    )
    assert len(loose_result["findings"][0]["matched_test_literals"]) == 1


def _matches(monkeypatch, *, value: str, string_kind: str, text: str, **compute_kwargs) -> list[dict]:
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref, **k: "deadbeef")
    _write_test_db("deadbeef", value=value, string_kind=string_kind)
    _stub_changed_literals(monkeypatch, [_finding(text=text)])
    result = impact.compute_impact("test-repo", "main", "product-repo", "base", "updated", **compute_kwargs)
    return result["findings"][0]["matched_test_literals"]


def test_button_label_does_not_match_as_a_substring_of_a_larger_string(monkeypatch):
    # The headline case: a `Save` button shouldn't be pulled in by every removed string
    # that merely contains "save", whereas the same literal as freeform text still is.
    removed = "Save your work before continuing"
    assert _matches(monkeypatch, value="Save", string_kind="button", text=removed) == []
    assert len(_matches(monkeypatch, value="Save", string_kind="text", text=removed)) == 1


def test_button_label_matches_the_whole_removed_string(monkeypatch):
    (match,) = _matches(monkeypatch, value="Save", string_kind="button", text="Save")

    assert match["match_confidence"] == 1.0
    assert match["string_kind"] == "button"
    assert match["match_algorithm"] == "exact"


def test_exact_match_differing_only_by_case_is_reported_at_reduced_confidence(monkeypatch):
    (match,) = _matches(monkeypatch, value="Delete Project", string_kind="button", text="delete project")

    assert (match["match_algorithm"], match["match_confidence"]) == ("exact", 0.7)
    # ... so a stricter floor than the 0.7 default drops it, and it is still not a substring match.
    assert (
        _matches(
            monkeypatch,
            value="Delete Project",
            string_kind="button",
            text="delete project",
            min_literal_match_confidence=0.71,
        )
        == []
    )
    assert _matches(monkeypatch, value="Delete", string_kind="button", text="delete project") == []


def test_exact_match_normalizes_whitespace(monkeypatch):
    assert len(_matches(monkeypatch, value="Delete  Project", string_kind="button", text=" Delete\nProject ")) == 1


def test_three_character_floor_applies_to_exact_kinds_too(monkeypatch):
    assert _matches(monkeypatch, value="OK", string_kind="button", text="OK", min_literal_match_confidence=0.0) == []


def test_freeform_match_records_its_algorithm(monkeypatch):
    (match,) = _matches(monkeypatch, value="Saved", string_kind="text", text="Saved successfully")

    assert match["string_kind"] == "text"
    assert match["match_algorithm"] == "substring_and_partial_ratio"


def test_unclassified_kind_falls_back_to_the_lax_algorithm(monkeypatch):
    assert len(_matches(monkeypatch, value="Saved", string_kind="unknown", text="Saved successfully")) == 1
    assert len(_matches(monkeypatch, value="Saved", string_kind="not-a-kind-yet", text="Saved successfully")) == 1


def test_data_literal_matches_exactly_at_low_confidence_so_only_when_the_floor_is_lowered(monkeypatch):
    assert _matches(monkeypatch, value="Project Alpha", string_kind="data", text="Project Alpha") == []

    (match,) = _matches(
        monkeypatch,
        value="Project Alpha",
        string_kind="data",
        text="Project Alpha",
        min_literal_match_confidence=0.3,
    )
    assert match["match_confidence"] == 0.3
    # ... and still only on equality, not containment.
    assert (
        _matches(
            monkeypatch,
            value="Project Alpha",
            string_kind="data",
            text="Create Project Alpha now",
            min_literal_match_confidence=0.3,
        )
        == []
    )


def test_opaque_literal_never_matches(monkeypatch):
    assert (
        _matches(
            monkeypatch, value="hunter2hunter2", string_kind="opaque", text="hunter2hunter2", min_literal_match_confidence=0.0
        )
        == []
    )


def test_result_no_longer_echoes_a_match_method(monkeypatch):
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref, **k: "deadbeef")
    _write_test_db("deadbeef")
    _stub_changed_literals(monkeypatch, [_finding()])

    result = impact.compute_impact("test-repo", "main", "product-repo", "base", "updated")

    assert "match_method" not in result


def test_raises_on_remote_job_error(monkeypatch):
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref, **k: "deadbeef")
    _write_test_db("deadbeef")
    monkeypatch.setattr(cl_client, "create_job", lambda repo, base, updated, **k: {"job_id": "job-1"})
    monkeypatch.setattr(cl_client, "get_job_status", lambda job_id, **k: {"status": "error", "error": "boom"})

    with pytest.raises(cl_client.ChangedLiteralsError, match="boom"):
        impact.compute_impact("test-repo", "main", "product-repo", "base", "updated")


def test_fetch_all_findings_follows_pagination(monkeypatch):
    page_one = [_finding(text=f"finding-{i}") for i in range(2)]
    page_two = [_finding(text=f"finding-{i}") for i in range(2, 3)]

    def _get_result_page(job_id, *, offset=0, limit=2000, **k):
        if offset == 0:
            return {"offset": 0, "limit": 2, "total": 3, "findings": page_one}
        return {"offset": 2, "limit": 2, "total": 3, "findings": page_two}

    monkeypatch.setattr(cl_client, "get_result_page", _get_result_page)
    monkeypatch.setattr(impact, "_RESULT_PAGE_SIZE", 2)

    all_findings = impact._fetch_all_findings("job-1")

    assert [f["text"] for f in all_findings] == ["finding-0", "finding-1", "finding-2"]


def test_recommends_invocations_for_matched_cases(monkeypatch):
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref, **k: "deadbeef")
    _write_test_db("deadbeef", case_id=1234, routing=[_cucu_entry(1234)])
    _stub_changed_literals(monkeypatch, [_finding()])

    result = impact.compute_impact("test-repo", "main", "product-repo", "base", "updated")

    assert result["min_invocation_confidence"] == 0.0
    assert result["invocation_max_length"] == 300
    assert result["extra_cases_allowed"] is None and result["missing_cases_allowed"] is None
    assert result["recommended_invocations"] == {
        "commands": ["/test-e2e cucu --tags=testrail(1234)"],
        "structured": [{"test_type": "cucu", "tags": ["testrail(1234)"], "flags": [], "args": {}}],
        "uncovered": [],
        "warnings": [],
    }


def test_no_invocations_when_nothing_matched(monkeypatch):
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref, **k: "deadbeef")
    _write_test_db("deadbeef", case_id=1234, routing=[_cucu_entry(1234)])
    _stub_changed_literals(monkeypatch, [_finding(text="Something unrelated")])

    result = impact.compute_impact("test-repo", "main", "product-repo", "base", "updated")

    assert result["recommended_invocations"]["commands"] == []
    assert result["recommended_invocations"]["structured"] == []


def test_min_invocation_confidence_uses_the_weaker_of_removal_and_match_confidence(monkeypatch):
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref, **k: "deadbeef")
    _write_test_db("deadbeef", case_id=1234, string_kind="button", routing=[_cucu_entry(1234)])
    # An exact match (match_confidence 1.0) in a removal changed-literals is only 0.9 sure of.
    _stub_changed_literals(monkeypatch, [_finding(confidence=0.9)])

    default = impact.compute_impact("test-repo", "main", "product-repo", "base", "updated")
    at_bar = impact.compute_impact(
        "test-repo", "main", "product-repo", "base", "updated", min_invocation_confidence=0.9
    )
    above_bar = impact.compute_impact(
        "test-repo", "main", "product-repo", "base", "updated", min_invocation_confidence=0.95
    )

    assert default["recommended_invocations"]["commands"] == ["/test-e2e cucu --tags=testrail(1234)"]
    assert at_bar["recommended_invocations"]["commands"] == ["/test-e2e cucu --tags=testrail(1234)"]
    assert above_bar["recommended_invocations"]["commands"] == []
    # The findings themselves are unaffected -- the bar only gates invocations.
    assert len(above_bar["findings"]) == 1
    assert len(above_bar["findings"][0]["matched_test_literals"]) == 1


def test_invocation_max_length_is_applied(monkeypatch):
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref, **k: "deadbeef")
    _write_test_db("deadbeef", case_id=1234, routing=[_cucu_entry(1234)])
    _stub_changed_literals(monkeypatch, [_finding()])

    # A single tag that alone exceeds the limit still gets a line of its own, never dropped.
    result = impact.compute_impact(
        "test-repo", "main", "product-repo", "base", "updated", invocation_max_length=10
    )

    assert result["invocation_max_length"] == 10
    assert result["recommended_invocations"]["commands"] == ["/test-e2e cucu --tags=testrail(1234)"]


def test_warns_instead_of_recommending_when_the_revision_has_no_routing_data(monkeypatch):
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref, **k: "deadbeef")
    _write_test_db("deadbeef")  # routing=None, as for a revision whose routing files were absent
    _stub_changed_literals(monkeypatch, [_finding()])

    result = impact.compute_impact("test-repo", "main", "product-repo", "base", "updated")

    assert result["total"] == 1  # the literal-matching half still works
    assert result["recommended_invocations"]["commands"] == []
    assert len(result["recommended_invocations"]["warnings"]) == 1


def test_tag_allowances_are_applied_to_the_recommendation(monkeypatch):
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref, **k: "deadbeef")
    _write_test_db("deadbeef", case_id=1234, routing=[_cucu_entry(1234)])  # tagged `smoke`, nothing else is
    _stub_changed_literals(monkeypatch, [_finding()])

    result = impact.compute_impact(
        "test-repo", "main", "product-repo", "base", "updated", extra_cases_allowed=0, missing_cases_allowed=0
    )

    assert result["extra_cases_allowed"] == 0 and result["missing_cases_allowed"] == 0
    assert result["recommended_invocations"]["commands"] == ["/test-e2e cucu --tags=smoke"]
    assert result["recommended_invocations"]["structured"][0]["extra_cases"] == []
    assert result["findings"][0]["matched_test_literals"][0]["case_id"] == 1234  # findings unaffected


def test_pr_number_is_passed_through_and_resolved_refs_are_reported(monkeypatch):
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref, **k: "deadbeef")
    _write_test_db("deadbeef")
    _stub_changed_literals(monkeypatch, [])
    created = {}

    def create_job(repo, base, updated, **kwargs):
        created.update(repo=repo, base=base, updated=updated, **kwargs)
        return {"job_id": "job-1"}

    monkeypatch.setattr(cl_client, "create_job", create_job)
    monkeypatch.setattr(
        cl_client, "get_job_status", lambda job_id, **k: {"status": "done", "base": "basesha", "updated": "headsha"}
    )

    result = impact.compute_impact("test-repo", "main", "product-repo", None, None, pr_number=9)

    assert created == {"repo": "product-repo", "base": None, "updated": None, "pr_number": 9}
    assert (result["base_ref"], result["updated_ref"], result["pr_number"]) == ("basesha", "headsha", 9)


def _running_then_done_status(monkeypatch) -> None:
    """changed-literals' status as first polled mid-way (fetching its repo), then done."""
    polls = iter(
        [
            {
                "status": "running",
                "phase": "fetching_literals_repo",
                "profiling": {
                    "phases": {
                        "resolving_pull_request": None,
                        "fetching_literals_repo": {"state": "running", "seconds": 0.5, "caches": {"findings": "miss"}},
                        "analyzing_literals_diff": None,
                    }
                },
            },
            {
                "status": "done",
                "phase": "done",
                "profiling": {
                    "phases": {
                        "resolving_pull_request": None,
                        "fetching_literals_repo": {
                            "state": "done",
                            "seconds": 2.0,
                            "caches": {"findings": "miss", "repo_clone": "hit"},
                        },
                        "analyzing_literals_diff": {"state": "done", "seconds": 3.0, "caches": {"findings": "miss"}},
                    }
                },
            },
        ]
    )
    monkeypatch.setattr(cl_client, "get_job_status", lambda job_id, **k: next(polls))
    monkeypatch.setattr(impact, "_POLL_INTERVAL_SECONDS", 0)


def test_tracker_gets_every_phase_of_the_job_with_changed_literals_phases_adopted(monkeypatch):
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref, **k: "deadbeef")
    _write_test_db("deadbeef")
    _stub_changed_literals(monkeypatch, [_finding()])
    _running_then_done_status(monkeypatch)
    tracker = PhaseTracker(PHASES)

    impact.compute_impact("test-repo", "main", "product-repo", "base", "updated", tracker=tracker)

    phases = tracker.snapshot()["phases"]
    assert phases["loading_test_literals"]["state"] == "done"
    assert phases["fetching_literals_repo"] == {
        "state": "done",
        "seconds": 2.0,
        "caches": {"findings": "miss", "repo_clone": "hit"},
    }
    assert phases["analyzing_literals_diff"]["seconds"] == 3.0
    assert phases["matching_literals"]["state"] == "done"
    assert phases["computing_invocations"]["state"] == "done"
    # Not asked for, so never run (as is the pull request lookup for a job naming its refs).
    assert phases["optimizing_invocation_tags"] is None
    assert phases["resolving_pull_request"] is None


def test_tag_substitution_is_its_own_phase_when_asked_for(monkeypatch):
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref, **k: "deadbeef")
    _write_test_db("deadbeef", case_id=1234, routing=[_cucu_entry(1234)])
    _stub_changed_literals(monkeypatch, [_finding()])
    tracker = PhaseTracker(PHASES)

    impact.compute_impact(
        "test-repo", "main", "product-repo", "base", "updated", extra_cases_allowed=5, tracker=tracker
    )

    phases = tracker.snapshot()["phases"]
    assert phases["computing_invocations"]["state"] == "done"
    assert phases["optimizing_invocation_tags"]["state"] == "done"


def test_a_changed_literals_failure_leaves_its_phase_failed_in_the_tracker(monkeypatch):
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref, **k: "deadbeef")
    _write_test_db("deadbeef")
    monkeypatch.setattr(cl_client, "create_job", lambda repo, base, updated, **k: {"job_id": "job-1"})
    monkeypatch.setattr(
        cl_client,
        "get_job_status",
        lambda job_id, **k: {
            "status": "error",
            "phase": "error",
            "error": "boom",
            "profiling": {"phases": {"fetching_literals_repo": {"state": "failed", "seconds": 0.1}}},
        },
    )
    tracker = PhaseTracker(PHASES)

    with pytest.raises(cl_client.ChangedLiteralsError):
        impact.compute_impact("test-repo", "main", "product-repo", "base", "updated", tracker=tracker)

    assert tracker.snapshot()["phases"]["fetching_literals_repo"]["state"] == "failed"
    assert tracker.snapshot()["phases"]["matching_literals"] is None


def test_tag_substitution_is_not_run_for_a_revision_without_routing_data(monkeypatch):
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref, **k: "deadbeef")
    _write_test_db("deadbeef")  # routing=None
    _stub_changed_literals(monkeypatch, [_finding()])
    tracker = PhaseTracker(PHASES)

    impact.compute_impact(
        "test-repo", "main", "product-repo", "base", "updated", extra_cases_allowed=5, tracker=tracker
    )

    phases = tracker.snapshot()["phases"]
    assert phases["computing_invocations"]["state"] == "done"
    assert phases["optimizing_invocation_tags"] is None
