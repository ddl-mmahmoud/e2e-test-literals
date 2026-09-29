"""Unit tests for e2e_test_literals/literals.py.

Covers the pure extraction helpers (argument-value filtering, table
header-vs-data-row handling) plus one end-to-end check that a real, bootstrapped
`behave` step registry actually matches a couple of stable cucu builtin step
texts -- proving the registry-bootstrap approach works against the installed
cucu/behave versions, without needing a real internal-e2e-tests-service
checkout (see conftest.py's `bootstrapped_registry` fixture).
"""

from __future__ import annotations

from pathlib import Path

import pytest_check as check
from behave.model import Step, Table
from behave.model_type import Argument

from e2e_test_literals.literals import Source, literals_for_step, literals_from_arguments, literals_from_table


def test_literals_from_arguments_keeps_only_named_string_values() -> None:
    step_text = 'I click the button "Save"'
    arguments = [
        Argument(0, 0, "Save", "Save", name="name"),
        Argument(0, 0, "1st", 0, name="index"),  # cucu's `nth` type converts to int
        Argument(0, 0, "{BASEURL}", "{BASEURL}", name="url"),
        Argument(0, 0, "#", "#", name=None),  # regex-matched, unnamed -- e.g. cucu's section_step
    ]

    literals = literals_from_arguments(
        arguments, Source(kind="direct", file="some.feature", line=10), step_text, static_vars={}
    )

    values = {literal["value"] for literal in literals}
    check.equal(len(literals), 2, msg="the int-valued `nth` and unnamed arguments must be excluded")
    check.is_in("Save", values)
    check.is_in("{BASEURL}", values)
    check.is_true(all(literal["kind"] == "argument" for literal in literals))
    check.is_true(all(literal["step_text"] == step_text for literal in literals))
    check.is_true(any(literal["dynamic"] for literal in literals if literal["value"] == "{BASEURL}"))


def test_literals_from_table_uses_headers_only_never_data_rows() -> None:
    table = Table(
        headings=["Name", "{WORKSPACE_DEFAULT_HARDWARE_TIER}"],
        rows=[[".*", "some-{VAR}-value"], ["Another Row", "Tiny k8s"]],
    )
    step_text = "I wait to see a table that contains rows matching the following"

    literals = literals_from_table(
        table,
        Source(kind="direct", file="some.feature", line=20),
        step_text,
        static_vars={"WORKSPACE_DEFAULT_HARDWARE_TIER": "Tiny k8s"},
    )

    check.equal(len(literals), 2, msg="only the two header cells, never the four row cells")
    values = {literal["value"] for literal in literals}
    check.equal(values, {"Name", "Tiny k8s"})
    check.is_true(all(literal["kind"] == "table_column" for literal in literals))
    check.is_true(all(literal["step_text"] == step_text for literal in literals))


def test_literals_from_table_returns_empty_for_no_table() -> None:
    check.equal(
        literals_from_table(
            None, Source(kind="direct", file="some.feature", line=30), "some step text", static_vars={}
        ),
        [],
    )


def test_literals_for_step_real_registry_matches_builtin_cucu_steps(bootstrapped_registry: Path) -> None:
    click_step = Step("<test>", 1, "When", "when", 'I click the button "Save"')
    literals, unmatched = literals_for_step(click_step, "tests.feature", "some scenario", static_vars={})
    check.is_none(unmatched)
    check.is_true(
        any(
            literal["value"] == "Save" and literal["kind"] == "argument" and literal["step_text"] == click_step.name
            for literal in literals
        )
    )

    see_step = Step("<test>", 2, "Then", "then", 'I should see the text "Welcome!"')
    literals, unmatched = literals_for_step(see_step, "tests.feature", "some scenario", static_vars={})
    check.is_none(unmatched)
    check.is_true(any(literal["value"] == "Welcome!" for literal in literals))


def test_literals_for_step_drops_unnamed_captures_from_comment_header_steps(bootstrapped_registry: Path) -> None:
    # cucu's built-in section_step ("* # Some heading") matches via a plain regex
    # group, not a `parse`-style {name} field -- its "#"-depth marker and heading
    # text are not real UI-facing content a step uses, so they contribute no
    # literals at all (see literals_from_arguments).
    heading_step = Step("<test>", 4, "*", "step", "# Create a Launcher")
    literals, unmatched = literals_for_step(heading_step, "tests.feature", "some scenario", static_vars={})

    check.is_none(unmatched)
    check.equal(literals, [])


def test_literals_for_step_records_unmatched_steps(bootstrapped_registry: Path) -> None:
    bogus_step = Step("<test>", 3, "When", "when", "I do something that no step definition implements")
    literals, unmatched = literals_for_step(bogus_step, "tests.feature", "some scenario", static_vars={})

    check.equal(literals, [])
    check.is_not_none(unmatched)
    if unmatched is not None:
        check.equal(unmatched.feature_file, "tests.feature")
        check.equal(unmatched.line, 3)
