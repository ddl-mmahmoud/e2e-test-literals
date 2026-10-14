from __future__ import annotations

from e2e_test_literals.service.replacements import (
    MIN_SIMILARITY,
    attach_replacements,
    cancel_unchanged,
    chosen_replacement,
)


def _f(change: str, text: str, *, file: str = "a.tsx", hunk: int | None = 0, line: int = 1, **extra) -> dict:
    return {
        "file": file,
        "line": line,
        "column": 1,
        "text": text,
        "context": "jsx-text",
        "confidence": 0.9,
        "change": change,
        "hunk": hunk,
        **extra,
    }


def _replacements(findings: list[dict]) -> dict[str, list[dict]]:
    return {f["text"]: f["replacements"] for f in attach_replacements(findings) if f["change"] == "removed"}


def test_a_reworded_label_is_paired_with_its_replacement():
    result = _replacements([_f("removed", "Create Project"), _f("added", "Create a Project")])

    assert [c["text"] for c in result["Create Project"]] == ["Create a Project"]
    assert result["Create Project"][0]["chosen"] is True
    assert result["Create Project"][0]["score"] > 80


def test_each_added_finding_replaces_at_most_one_removal():
    findings = [
        _f("removed", "Save project", line=1),
        _f("removed", "Delete project", line=2),
        _f("added", "Delete the project", line=1),
        _f("added", "Save the project", line=2),
    ]

    result = _replacements(findings)

    assert chosen_replacement({"replacements": result["Save project"]})["text"] == "Save the project"
    assert chosen_replacement({"replacements": result["Delete project"]})["text"] == "Delete the project"


def test_a_balanced_hunk_pairs_even_completely_rewritten_text():
    result = _replacements([_f("removed", "Access"), _f("added", "Zzzzzz qqq")])

    assert result["Access"][0]["score"] < MIN_SIMILARITY
    assert result["Access"][0]["chosen"] is True


def test_a_dissimilar_candidate_in_a_lopsided_hunk_is_listed_but_not_chosen():
    result = _replacements([_f("removed", "Access"), _f("removed", "Other thing"), _f("added", "Zzzzzz qqq")])

    assert [c["chosen"] for c in result["Access"]] == [False]
    assert chosen_replacement({"replacements": result["Access"]}) is None


def test_only_the_same_file_and_hunk_count_as_candidates():
    findings = [
        _f("removed", "Access"),
        _f("added", "Access", file="b.tsx"),
        _f("added", "Access", hunk=1),
        _f("added", "Access", hunk=None),
    ]

    assert _replacements(findings) == {"Access": []}


def test_a_removal_without_a_hunk_has_no_replacements():
    assert _replacements([_f("removed", "Access", hunk=None), _f("added", "Access", hunk=None)]) == {"Access": []}


def test_added_findings_pass_through_unchanged_and_inputs_are_not_mutated():
    removed, added = _f("removed", "Access"), _f("added", "Access now")
    out = attach_replacements([removed, added])

    assert out[1] == added
    assert "replacements" not in removed
    assert out[0]["replacements"][0]["line"] == added["line"]


def test_identical_text_removed_and_added_in_one_hunk_is_not_a_change():
    findings = [_f("removed", "Access"), _f("added", "Access", line=2)]

    assert cancel_unchanged(findings) == []


def test_cancelling_lets_the_remaining_findings_pair_among_themselves():
    findings = [_f("removed", "Access"), _f("removed", "Zzzzzz"), _f("added", "Access"), _f("added", "Qqq")]

    result = _replacements(cancel_unchanged(findings))

    assert list(result) == ["Zzzzzz"]
    assert chosen_replacement({"replacements": result["Zzzzzz"]})["text"] == "Qqq"


def test_cancelling_pairs_one_removal_with_one_addition():
    findings = [_f("removed", "Access", line=1), _f("removed", "Access", line=2), _f("added", "Access", line=3)]

    assert [(f["change"], f["line"]) for f in cancel_unchanged(findings)] == [("removed", 2)]


def test_cancelling_needs_the_same_file_hunk_and_exact_text():
    findings = [
        _f("removed", "Access"),
        _f("added", "Access", file="b.tsx"),
        _f("added", "Access", hunk=1),
        _f("added", "access"),
        _f("removed", "Moved", hunk=None),
        _f("added", "Moved", hunk=None),
    ]

    assert cancel_unchanged(findings) == findings
