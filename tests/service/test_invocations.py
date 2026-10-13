"""Tests for service/invocations.py: routing data is written through the real `write_sqlite`
and read back by the real `recommend_invocations`; the entries themselves are synthetic
(routing.py's own mapping is covered by tests/test_routing.py)."""

from __future__ import annotations

from pathlib import Path

from e2e_test_literals.db import write_sqlite
from e2e_test_literals.service import invocations


def _entry(case_id: int, candidates: list[tuple[str, list[str]]], *, domain: str = "ui") -> dict:
    return {
        "case_id": case_id,
        "domain": domain,
        "file": "tests/ui/features/some.feature" if domain == "ui" else "tests/api/tests/test_some.py",
        "name": f"case {case_id}",
        "root": "domino" if domain == "ui" else None,
        "labels": [],
        "candidates": [{"test_type": test_type, "flags": flags} for test_type, flags in candidates],
    }


def _recommend(tmp_path: Path, entries: list[dict], case_ids: list[int], **kwargs) -> dict:
    db_file = tmp_path / "db.sqlite"
    write_sqlite({}, db_file, routing=entries)
    return invocations.recommend_invocations(db_file, case_ids, **kwargs)


def test_prefers_cucu_and_emits_no_flags_when_none_are_needed(tmp_path):
    entries = [_entry(1, [("cucu", []), ("cucu", ["on_prem"]), ("nexus", [])])]

    stanza = _recommend(tmp_path, entries, [1])

    assert stanza["commands"] == ["/test-e2e cucu --tags=testrail(1)"]
    assert stanza["structured"] == [{"test_type": "cucu", "tags": ["testrail(1)"], "flags": [], "args": {}}]
    assert stanza["uncovered"] == []


def test_groups_cases_that_share_a_test_type_and_flags_into_one_line(tmp_path):
    entries = [_entry(2, [("cucu", [])]), _entry(1, [("cucu", [])])]

    stanza = _recommend(tmp_path, entries, [2, 1])

    assert stanza["commands"] == ["/test-e2e cucu --tags=testrail(1),testrail(2)"]


def test_on_prem_only_case_gets_the_on_prem_flag_and_its_implied_argument(tmp_path):
    entries = [_entry(3, [("nexus", ["on_prem"]), ("nexus-only", ["on_prem"])])]

    stanza = _recommend(tmp_path, entries, [3])

    assert stanza["commands"] == ["/test-e2e nexus --on_prem --flavor=qe-aws-eks --tags=testrail(3)"]
    assert stanza["structured"] == [
        {"test_type": "nexus", "tags": ["testrail(3)"], "flags": ["on_prem"], "args": {"flavor": "qe-aws-eks"}}
    ]


def test_never_recommends_vcluster_or_is_core_candidates(tmp_path):
    entries = [
        _entry(4, [("cucu", ["vcluster"]), ("nexus", [])]),  # cucu is only reachable via --vcluster
        _entry(5, [("gpu", ["is_core"])]),  # only reachable via the core workflow
    ]

    stanza = _recommend(tmp_path, entries, [4, 5], domains=("ui", "api"))

    assert stanza["commands"] == ["/test-e2e nexus --tags=testrail(4)"]
    assert len(stanza["uncovered"]) == 1
    assert stanza["uncovered"][0].startswith("5: tests/ui/features/some.feature::case 5 only selectable via")


def test_uses_a_lower_tier_only_when_the_preferred_tier_is_not_usable(tmp_path):
    entries = [
        _entry(6, [("cucu", ["vcluster"]), ("gpu", []), ("nexus", [])]),  # tier 1 unusable -> tier 2 (gpu)
    ]

    stanza = _recommend(tmp_path, entries, [6])

    assert stanza["commands"] == ["/test-e2e gpu --tags=testrail(6)"]


def test_set_cover_picks_the_line_covering_the_most_cases_within_a_tier(tmp_path):
    # smoke and gpu are the same tier, so only coverage decides: smoke covers all three cases.
    entries = [
        _entry(1, [("smoke", []), ("gpu", [])]),
        _entry(2, [("smoke", [])]),
        _entry(3, [("gpu", []), ("smoke", [])]),
    ]

    stanza = _recommend(tmp_path, entries, [1, 2, 3])

    assert stanza["commands"] == ["/test-e2e smoke --tags=testrail(1),testrail(2),testrail(3)"]


def test_set_cover_emits_a_second_line_for_what_the_first_cannot_cover(tmp_path):
    entries = [
        _entry(1, [("smoke", []), ("gpu", [])]),
        _entry(2, [("smoke", [])]),
        _entry(3, [("gpu", [])]),
    ]

    stanza = _recommend(tmp_path, entries, [1, 2, 3])

    assert stanza["commands"] == [
        "/test-e2e gpu --tags=testrail(1),testrail(3)",
        "/test-e2e smoke --tags=testrail(2)",
    ]


def test_splits_lines_at_max_length_only_between_tags(tmp_path):
    case_ids = list(range(1001, 1041))
    entries = [_entry(case_id, [("cucu", [])]) for case_id in case_ids]

    stanza = _recommend(tmp_path, entries, case_ids, max_length=80)

    assert len(stanza["commands"]) > 1
    assert all(len(command) <= 80 for command in stanza["commands"])
    assert all(command.startswith("/test-e2e cucu --tags=testrail(") for command in stanza["commands"])
    tags = [tag for item in stanza["structured"] for tag in item["tags"]]
    assert tags == [f"testrail({case_id})" for case_id in case_ids]  # nothing lost, duplicated, or reordered
    for item, command in zip(stanza["structured"], stanza["commands"]):
        assert command.endswith("--tags=" + ",".join(item["tags"]))


def test_max_length_zero_disables_splitting(tmp_path):
    case_ids = list(range(1001, 1101))
    entries = [_entry(case_id, [("cucu", [])]) for case_id in case_ids]

    stanza = _recommend(tmp_path, entries, case_ids, max_length=0)

    assert len(stanza["commands"]) == 1


def test_default_max_length_is_300(tmp_path):
    case_ids = list(range(1001, 1101))
    entries = [_entry(case_id, [("cucu", [])]) for case_id in case_ids]

    stanza = _recommend(tmp_path, entries, case_ids)

    assert invocations.DEFAULT_MAX_LENGTH == 300
    assert len(stanza["commands"]) > 1
    assert all(len(command) <= 300 for command in stanza["commands"])


def test_split_lines_account_for_the_flags_in_the_prefix(tmp_path):
    case_ids = list(range(1001, 1021))
    entries = [_entry(case_id, [("nexus", ["on_prem"])]) for case_id in case_ids]

    stanza = _recommend(tmp_path, entries, case_ids, max_length=100)

    assert all(len(command) <= 100 for command in stanza["commands"])
    assert all("--on_prem --flavor=qe-aws-eks" in command for command in stanza["commands"])


def test_api_entries_are_ignored_by_default_but_can_be_requested(tmp_path):
    entries = [_entry(7, [("gpu", []), ("gpu", ["on_prem"])], domain="api")]

    default = _recommend(tmp_path, entries, [7])
    with_api = _recommend(tmp_path, entries, [7], domains=("ui", "api"))

    assert default["commands"] == []
    assert default["uncovered"] == ["7: no @testrail(7) entry found in the indexed test repo"]
    assert with_api["commands"] == ["/test-e2e gpu --tags=testrail(7)"]


def test_unknown_case_id_is_reported_as_uncovered(tmp_path):
    stanza = _recommend(tmp_path, [_entry(1, [("cucu", [])])], [1, 99])

    assert stanza["commands"] == ["/test-e2e cucu --tags=testrail(1)"]
    assert stanza["uncovered"] == ["99: no @testrail(99) entry found in the indexed test repo"]


def test_no_case_ids_gives_an_empty_stanza(tmp_path):
    assert _recommend(tmp_path, [], []) == {"commands": [], "structured": [], "uncovered": [], "warnings": []}


def test_warns_when_routing_was_not_indexed_for_the_revision(tmp_path):
    db_file = tmp_path / "db.sqlite"
    write_sqlite({}, db_file, routing=None, routing_error="run_ui_tests.py is not present at this revision")

    stanza = invocations.recommend_invocations(db_file, [1])

    assert stanza["commands"] == [] and stanza["structured"] == []
    assert stanza["warnings"] == [
        "no invocation recommendations: run_ui_tests.py is not present at this revision"
    ]


def test_warns_on_a_db_from_before_routing_data_existed(tmp_path):
    import sqlite3

    db_file = tmp_path / "old.sqlite"
    sqlite3.connect(db_file).close()  # no tables at all

    stanza = invocations.recommend_invocations(db_file, [1])

    assert stanza["commands"] == []
    assert stanza["warnings"] == ["no invocation recommendations: routing data was not indexed for this revision"]
