"""Tests for service/tag_planning.py: the planner is pure, so these feed it tag universes directly."""

from __future__ import annotations

import math

import pytest

from e2e_test_literals.service.tag_planning import TagSelectionError, plan_tag_selection


def test_a_tag_with_no_outsiders_is_free_and_always_taken():
    plan, exhausted = plan_tag_selection([([1, 2], {"smoke": {1, 2}})], 0, 0)

    assert plan == [("smoke",)]
    assert not exhausted


def test_no_groups_is_an_empty_plan():
    assert plan_tag_selection([], 0, 0) == ([], False)


# `wide` covers all three targets for 3 extras (3 total); `narrow` covers two for 1 extra and
# leaves one missing (2 total); all-explicit is 3 missing (3 total).
_TRADE_OFF = ([1, 2, 3], {"wide": {1, 2, 3, 10, 11, 12}, "narrow": {1, 2, 20}})


def test_minimizes_extra_plus_missing_among_selections_that_fit():
    plan, _ = plan_tag_selection([_TRADE_OFF], math.inf, math.inf)

    assert plan == [("narrow",)]


def test_allowances_force_the_other_end_of_the_trade_off():
    plan, _ = plan_tag_selection([_TRADE_OFF], math.inf, 0)

    assert plan == [("wide",)]


def test_allowances_are_summed_across_groups():
    # Each group alone could afford its single extra, but the two together exceed 1.
    groups = [([1], {"a": {1, 10}}), ([2], {"b": {2, 11}})]

    with pytest.raises(TagSelectionError):
        plan_tag_selection(groups, 1, 0)
    plan, _ = plan_tag_selection(groups, 2, 0)
    assert plan == [("a",), ("b",)]


def test_shortfall_message_reports_the_achievable_minima():
    with pytest.raises(TagSelectionError) as excinfo:
        plan_tag_selection([([1, 2], {"smoke": {1, 2, 3}})], 0, 0)

    message = str(excinfo.value)
    assert "extra_cases_allowed=0" in message and "missing_cases_allowed=0" in message
    assert "fewest extra + missing overall: 1 extra, 0 missing" in message
    assert "with at most 0 missing: at least 1 extra" in message
    assert "with at most 0 extra: at least 2 missing" in message


def test_unbounded_allowances_are_described_as_such():
    # Case 2 has no shared tag at all, so it is missing however many extras are allowed.
    with pytest.raises(TagSelectionError) as excinfo:
        plan_tag_selection([([1, 2], {"a": {1}})], math.inf, 0)

    message = str(excinfo.value)
    assert "extra_cases_allowed=unbounded" in message
    assert "at least 1 missing even with unlimited extra cases" in message
