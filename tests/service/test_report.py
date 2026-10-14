from __future__ import annotations

import hashlib

import pytest

from e2e_test_literals.service import config
from e2e_test_literals.service.report import (
    blob_url,
    build_report,
    diff_url,
    github_repo_url,
    prompt_item,
    split_step,
)


@pytest.mark.parametrize(
    "repo",
    [
        "https://github.com/cerebrotech/domino",
        "https://github.com/cerebrotech/domino.git",
        "https://github.com/cerebrotech/domino/",
        "https://x-access-token:abc@github.com/cerebrotech/domino.git",
        "git@github.com:cerebrotech/domino.git",
        "ssh://git@github.com/cerebrotech/domino",
    ],
)
def test_github_repo_url_normalizes_the_usual_spellings(repo):
    assert github_repo_url(repo) == "https://github.com/cerebrotech/domino"


def test_github_repo_url_is_none_for_other_hosts_and_local_paths():
    assert github_repo_url("https://gitlab.com/a/b") is None
    assert github_repo_url("/home/me/repo") is None


def test_diff_url_for_a_compare_anchors_the_left_side_line_of_the_hashed_path():
    digest = hashlib.sha256(b"a/b.html").hexdigest()

    assert diff_url("https://github.com/o/r", "a/b.html", 45, "base", "upd", None) == (
        f"https://github.com/o/r/compare/base...upd#diff-{digest}L45"
    )


def test_diff_url_for_a_pull_request_uses_the_files_tab():
    assert diff_url("https://github.com/o/r", "x", 1, "b", "u", 12).startswith("https://github.com/o/r/pull/12/files#diff-")


def test_diff_url_is_none_without_a_github_repo():
    assert diff_url(None, "x", 1, "b", "u", None) is None


def test_blob_url_pins_the_sha_and_the_line():
    assert blob_url("https://github.com/o/r", "abc", "tests/a b.feature", 63) == (
        "https://github.com/o/r/blob/abc/tests/a%20b.feature#L63"
    )
    assert blob_url("https://github.com/o/r", "abc", "t.feature") == "https://github.com/o/r/blob/abc/t.feature"


def test_split_step_highlights_the_literal_case_insensitively_keeping_the_steps_own_casing():
    assert split_step('I click the link "ACCESS" now', "access") == {
        "before": 'I click the link "',
        "literal": "ACCESS",
        "after": '" now',
    }


def test_split_step_leaves_the_step_whole_when_the_literal_is_not_in_it():
    assert split_step("I run the launcher step", "Access") == {
        "before": "I run the launcher step",
        "literal": "",
        "after": "",
    }


def _literal(**overrides) -> dict:
    base = {
        "value": "Access",
        "string_kind": "link",
        "case_id": 771386,
        "scenario_name": "Check links",
        "source_file": "tests/a.feature",
        "source_line": 63,
        "step_text": 'I click the link "Access"',
        "tags": ["eng", "testrail(771386)"],
        "match_algorithm": "exact",
        "match_confidence": 1.0,
    }
    base.update(overrides)
    return base


def test_prompt_item_for_an_exact_match_states_the_new_value_and_only_the_step():
    assert prompt_item("Access", "Manage access", _literal()) == {
        "change": '"Access" is now "Manage access" in the product. Affected steps:',
        "step": 'I click the link "Access"',
        "partial": None,
    }


def test_prompt_item_for_a_substring_match_keeps_what_the_step_checks_for():
    item = prompt_item(
        "Create a new project now",
        "Create a project now",
        _literal(match_algorithm="substring_and_partial_ratio", value="new project"),
    )

    assert item["change"].startswith('"Create a new project now" is now "Create a project now"')
    assert item["partial"] == "new project"


def test_prompt_item_without_a_replacement_says_so():
    assert "was removed from the product and no replacement was detected" in prompt_item("Access", None, _literal())["change"]


def test_prompt_item_leaves_out_what_the_agent_does_not_need():
    rendered = str(prompt_item("Access", None, _literal()))

    assert "Check links" not in rendered  # scenario name: the case id is shown by the page
    assert "63" not in rendered  # line number
    assert "tests/a.feature" not in rendered  # the page groups by feature file


def test_prompt_item_is_omitted_when_the_text_did_not_change():
    assert prompt_item("Access", "Access", _literal()) is None
    assert prompt_item("Access", "Access", _literal(match_algorithm="substring_and_partial_ratio")) is None


def _result(findings: list[dict], **overrides) -> dict:
    base = {
        "test_repo": "https://github.com/o/tests",
        "test_ref": "main",
        "test_sha": "t" * 40,
        "literals_repo": "https://github.com/o/product",
        "base_ref": "b" * 40,
        "updated_ref": "u" * 40,
        "pr_number": None,
        "min_invocation_confidence": 0.0,
        "invocation_max_length": 300,
        "extra_cases_allowed": None,
        "missing_cases_allowed": 2,
        "findings": findings,
        "recommended_invocations": {"commands": ["/test-e2e cucu"], "structured": [], "uncovered": [], "warnings": []},
    }
    base.update(overrides)
    return base


def _finding(text="Access", line=45, matches=None, **overrides) -> dict:
    base = {
        "file": "views/a.html",
        "line": line,
        "column": 3,
        "text": text,
        "context": "html-text",
        "confidence": 0.9,
        "change": "removed",
        "hunk": 0,
        "matched_test_literals": [_literal()] if matches is None else matches,
        "replacements": [],
    }
    base.update(overrides)
    return base


def test_an_entry_exists_per_finding_and_matched_literal_grouped_by_feature_file():
    matches = [
        _literal(source_file="tests/b.feature", source_line=5, case_id=2),
        _literal(source_file="tests/a.feature", source_line=63, case_id=1),
        _literal(source_file="tests/a.feature", source_line=9, case_id=3),
    ]

    built = build_report(_result([_finding(matches=matches)]))

    (change,) = built["changes"]
    assert [g["file"] for g in change["groups"]] == ["tests/a.feature", "tests/b.feature"]
    assert [e["line"] for e in change["groups"][0]["entries"]] == [9, 63]
    assert change["groups"][0]["feature_url"] == f"https://github.com/o/tests/blob/{'t' * 40}/tests/a.feature"
    assert change["groups"][0]["entries"][1]["step_url"].endswith("/tests/a.feature#L63")


def test_entry_ids_are_unique_even_when_one_step_matches_twice():
    twice = [_literal(), _literal(value="access")]

    built = build_report(_result([_finding(matches=twice)]))

    ids = [e["id"] for g in built["changes"][0]["groups"] for e in g["entries"]]
    assert len(ids) == len(set(ids)) == 2


def test_findings_are_ordered_by_file_then_line_and_unmatched_ones_are_set_aside():
    built = build_report(_result([_finding(text="Late", line=90), _finding(text="None", line=50, matches=[]), _finding()]))

    assert [c["line"] for c in built["changes"]] == [45, 90]
    assert [u["text"] for u in built["unmatched"]] == ["None"]


def test_the_chosen_replacement_reaches_the_change_and_the_prompt_items():
    replacements = [{"text": "Manage access", "chosen": True, "score": 40}]

    built = build_report(_result([_finding(replacements=replacements)]))

    (change,) = built["changes"]
    assert change["replacement"] == "Manage access"
    assert '"Access" is now "Manage access"' in change["groups"][0]["entries"][0]["prompt"]["change"]


def test_an_unchanged_replacement_leaves_the_entrys_prompt_empty():
    replacements = [{"text": "Access", "chosen": True, "score": 100}]

    built = build_report(_result([_finding(replacements=replacements)]))

    assert built["changes"][0]["groups"][0]["entries"][0]["prompt"] is None


def test_an_unchosen_candidate_is_not_stated_as_the_replacement():
    built = build_report(_result([_finding(replacements=[{"text": "Zzz", "chosen": False, "score": 3}])]))

    assert built["changes"][0]["replacement"] is None


def test_entries_carry_the_weaker_confidence_testrail_links_and_tag_links():
    built = build_report(_result([_finding(confidence=0.9, matches=[_literal(match_confidence=0.7)])]))

    entry = built["changes"][0]["groups"][0]["entries"][0]
    assert entry["confidence"] == 0.7
    assert entry["case_url"] == f"{config.TESTRAIL_URL}/index.php?/cases/view/771386"
    assert [t["url"] for t in entry["tags"]] == [None, entry["case_url"]]


def test_added_findings_are_not_listed_and_the_recompute_block_echoes_the_result_settings():
    added = {**_finding(), "change": "added"}
    added.pop("matched_test_literals")

    built = build_report(_result([_finding(), added]))

    assert len(built["changes"]) == 1 and built["unmatched"] == []
    assert built["recompute"] == {
        "test_sha": "t" * 40,
        "invocation_max_length": 300,
        "extra_cases_allowed": None,
        "missing_cases_allowed": 2,
        "min_invocation_confidence": 0.0,
    }
    assert built["invocations"]["commands"] == ["/test-e2e cucu"]


def test_a_pull_request_names_itself_in_the_preamble_and_links_to_its_files_tab():
    built = build_report(_result([_finding()], pr_number=7))

    assert "pull request #7 of https://github.com/o/product" in built["preamble"]
    assert "/pull/7/files#diff-" in built["changes"][0]["diff_url"]


def test_non_github_repos_simply_get_no_links():
    built = build_report(_result([_finding()], test_repo="/local/tests", literals_repo="/local/product"))

    assert built["changes"][0]["diff_url"] is None
    assert built["changes"][0]["groups"][0]["feature_url"] is None
    assert built["changes"][0]["groups"][0]["entries"][0]["step_url"] is None



def test_the_header_links_to_the_pull_request_or_comparison_and_to_the_test_revisions_tree():
    pr = build_report(_result([_finding()], pr_number=7))["meta"]
    compare = build_report(_result([_finding()]))["meta"]

    assert pr["literals_ref_url"] == "https://github.com/o/product/pull/7"
    assert compare["literals_ref_url"] == f"https://github.com/o/product/compare/{'b' * 40}...{'u' * 40}"
    assert pr["test_sha_url"] == f"https://github.com/o/tests/tree/{'t' * 40}"


def test_the_header_gets_no_links_for_non_github_repos():
    meta = build_report(_result([_finding()], test_repo="/local/tests", literals_repo="/local/product"))["meta"]

    assert meta["literals_ref_url"] is None and meta["test_sha_url"] is None


@pytest.mark.parametrize(
    ("literal", "exact"),
    [
        (_literal(), True),
        (_literal(value="  Access "), True),  # whitespace is normalized away
        (_literal(value="access", match_confidence=0.7), False),  # differs by case only
        (_literal(match_algorithm="substring_and_partial_ratio", match_confidence=0.8), False),
        (_literal(string_kind="data", match_confidence=0.3), True),  # exact, though not at confidence 1.0
    ],
)
def test_an_entry_is_exact_only_for_a_case_sensitive_match_of_an_exact_literal(literal, exact):
    built = build_report(_result([_finding(matches=[literal])]))

    assert built["changes"][0]["groups"][0]["entries"][0]["exact"] is exact
