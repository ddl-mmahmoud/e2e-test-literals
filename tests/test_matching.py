"""Unit tests for e2e_test_literals/matching.py -- the per-kind literal-vs-product-string comparison."""

from __future__ import annotations

import pytest

from e2e_test_literals.matching import (
    CASE_INSENSITIVE_CONFIDENCE,
    DEFAULT_MIN_LITERAL_MATCH_CONFIDENCE,
    match_confidence,
)


def test_exact_kind_requires_equality() -> None:
    assert match_confidence("Save", "Save", "button") == ("exact", 1.0)
    assert match_confidence("Save", "Save your work", "button") is None


def test_exact_kind_differing_only_by_case_matches_at_reduced_confidence() -> None:
    assert match_confidence("Save", "save", "button") == ("exact", 0.7)
    assert match_confidence("ENVIRONMENT", "Environment", "table_header") == ("exact", 0.7)
    assert match_confidence("Save", "saved", "button") is None
    assert DEFAULT_MIN_LITERAL_MATCH_CONFIDENCE <= CASE_INSENSITIVE_CONFIDENCE


def test_exact_kind_normalizes_whitespace() -> None:
    assert match_confidence("New  Project", "New\nProject ", "link") == ("exact", 1.0)


def test_fuzzy_kind_requires_containment_and_grades_the_confidence() -> None:
    assert match_confidence("Saved", "Saved successfully", "text") == ("substring_and_partial_ratio", 0.8)
    algorithm, confidence = match_confidence("Saved", "so saved here", "text")
    assert algorithm == "substring_and_partial_ratio"
    assert 0.0 < confidence < 0.8  # contained case-insensitively, but the case difference costs points


def test_substring_confidence_is_scaled_below_what_an_exact_match_scores() -> None:
    # A clean containment tops out at 0.8 -- above the default floor, below an exact match.
    _, confidence = match_confidence("Saved", "Saved successfully", "text")
    assert CASE_INSENSITIVE_CONFIDENCE <= confidence < 1.0
    assert DEFAULT_MIN_LITERAL_MATCH_CONFIDENCE <= confidence
    # A weak one (a case-mismatched hit inside a longer word) falls under the default floor.
    _, weak = match_confidence("Save", "Unsaved changes will be lost", "text")
    assert weak < DEFAULT_MIN_LITERAL_MATCH_CONFIDENCE


def test_fuzzy_kind_never_matches_a_literal_longer_than_the_text() -> None:
    assert match_confidence("Access Forbidden", "Access", "text") is None


def test_resemblance_without_containment_never_matches() -> None:
    assert match_confidence("Delete project", "prefix Delete prbject suffix text padding here", "text") is None


@pytest.mark.parametrize("string_kind", ["button", "text", "data", "unknown"])
def test_short_literals_never_match(string_kind: str) -> None:
    assert match_confidence("OK", "OK", string_kind) is None
    assert match_confidence("OK", "OK, continuing now", string_kind) is None


def test_never_kind_never_matches() -> None:
    assert match_confidence("hunter2hunter2", "hunter2hunter2", "opaque") is None


def test_data_kind_is_exact_at_a_fixed_low_confidence() -> None:
    assert match_confidence("Project Alpha", "Project Alpha", "data") == ("exact", 0.3)
    assert match_confidence("Project Alpha", "Create Project Alpha", "data") is None
    assert match_confidence("Project Alpha", "project alpha", "data") == ("exact", 0.3)  # capped, not raised


def test_reverse_flips_the_containment_direction_for_fuzzy_kinds_only() -> None:
    assert match_confidence("Save changes", "Save", "text") is None
    assert match_confidence("Save changes", "Save", "text", reverse=True) == ("substring_and_partial_ratio", 0.8)
    assert match_confidence("Save changes", "Save", "button", reverse=True) is None


def test_force_exact_makes_fuzzy_kinds_exact_but_leaves_never_and_data_confidence_alone() -> None:
    assert match_confidence("Saved", "Saved successfully", "text", force_exact=True) is None
    assert match_confidence("Saved", "Saved", "text", force_exact=True) == ("exact", 1.0)
    assert match_confidence("hunter2hunter2", "hunter2hunter2", "opaque", force_exact=True) is None
    assert match_confidence("Project Alpha", "Project Alpha", "data", force_exact=True) == ("exact", 0.3)
