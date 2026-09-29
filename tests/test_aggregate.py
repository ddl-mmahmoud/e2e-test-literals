"""Unit tests for e2e_test_literals/aggregate.py.

Covers the pure aggregation pieces (de-duplication, the Pass-B merge seam)
without spinning up the real step registry or walking a feature-file corpus.
"""

from __future__ import annotations

import pytest_check as check

from e2e_test_literals.aggregate import _dedupe_literals, merge_templated_literals


def test_merge_templated_literals_is_a_noop_when_none() -> None:
    cases = {
        101602: {
            "scenarios": [{"feature_file": "some.feature", "scenario_name": "some scenario", "tags": []}],
            "literals": [
                {
                    "value": "Save",
                    "kind": "argument",
                    "dynamic": False,
                    "source_kind": "direct",
                    "source_file": "some.feature",
                    "source_line": 5,
                }
            ],
        }
    }

    result = merge_templated_literals(cases, None)

    check.equal(result, cases)


def test_merge_templated_literals_is_a_noop_when_empty_mapping() -> None:
    cases = {101602: {"scenarios": [], "literals": []}}

    result = merge_templated_literals(cases, {})

    check.equal(result, cases)


def test_dedupe_literals_collapses_identical_entries_but_keeps_distinct_sources() -> None:
    literals = [
        {
            "value": "Save",
            "kind": "argument",
            "dynamic": False,
            "source_kind": "direct",
            "source_file": "some.feature",
            "source_line": 5,
        },
        {
            "value": "Save",
            "kind": "argument",
            "dynamic": False,
            "source_kind": "direct",
            "source_file": "some.feature",
            "source_line": 5,
        },  # exact repeat
        {
            "value": "Save",
            "kind": "argument",
            "dynamic": False,
            "source_kind": "direct",
            "source_file": "some.feature",
            "source_line": 9,
        },  # same value/kind, different step
    ]

    deduped = _dedupe_literals(literals)

    check.equal(len(deduped), 2)
    sources = sorted(literal["source_line"] for literal in deduped)
    check.equal(sources, [5, 9])


def test_dedupe_literals_drops_cucu_variable_noise() -> None:
    literals = [
        {
            "value": "Save",
            "kind": "argument",
            "dynamic": False,
            "source_kind": "direct",
            "source_file": "some.feature",
            "source_line": 5,
        },
        {
            "value": "{USER_NAME}",
            "kind": "argument",
            "dynamic": True,
            "source_kind": "direct",
            "source_file": "some.feature",
            "source_line": 6,
        },
        {
            "value": "Project-{SCENARIO_RUN_ID}",
            "kind": "argument",
            "dynamic": True,
            "source_kind": "direct",
            "source_file": "some.feature",
            "source_line": 7,
        },
        {
            "value": "   ",
            "kind": "table_column",
            "dynamic": False,
            "source_kind": "direct",
            "source_file": "some.feature",
            "source_line": 8,
        },
    ]

    deduped = _dedupe_literals(literals)

    check.equal(deduped, [literals[0]])
