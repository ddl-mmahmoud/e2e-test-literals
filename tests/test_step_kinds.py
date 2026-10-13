"""Unit tests for e2e_test_literals/step_kinds.py: the argument classifier and the policy table.

The patterns are real registered step patterns (cucu's own and this suite's custom steps)."""

from __future__ import annotations

import pytest

from e2e_test_literals import step_kinds
from e2e_test_literals.step_kinds import EXACT, FUZZY, NEVER


@pytest.mark.parametrize(
    ("pattern", "arg_name", "expected"),
    [
        pytest.param('I click the button "{name}"', "name", "button", id="button"),
        pytest.param('I wait up to "{seconds}" seconds to click the "{nth:nth}" button "{name}"', "name", "button", id="nth-button"),
        pytest.param('I should see the link "{name}"', "name", "link", id="link"),
        pytest.param('I click the tab "{name}"', "name", "tab", id="tab"),
        pytest.param('I click the menu item "{name}"', "name", "menu_item", id="menu-item-beats-menu"),
        pytest.param('I should see the menu "{name}"', "name", "menu", id="menu"),
        pytest.param('I check the checkbox "{name}"', "name", "checkbox", id="checkbox"),
        pytest.param('I select the radio button "{name}"', "name", "radio_button", id="radio-button"),
        pytest.param('I select the option "{option}" from the dropdown "{dropdown}"', "option", "option", id="option"),
        pytest.param('I select the option "{option}" from the dropdown "{dropdown}"', "dropdown", "dropdown", id="dropdown"),
        pytest.param('I should see the dropdown option "{name}" is disabled', "name", "option", id="dropdown-option"),
        pytest.param('I should see the input "{name}"', "name", "input", id="input"),
        pytest.param('I click the table header "{name}"', "name", "table_header", id="table-header"),
        pytest.param('I sort the table by column "{column_header}" in "{sort_order}" order', "column_header", "table_header", id="column"),
        pytest.param('I should see the image with the alt text "{name}"', "name", "image_alt", id="image-alt-beats-text"),
        pytest.param('I should see the text "{text}"', "text", "text", id="text"),
        pytest.param('I click the element "{name}"', "name", "element", id="element"),
        pytest.param(
            'I drag the "{nth_1:nth}" element "{name_1}" to the "{nth_2:nth}" element "{name_2}"',
            "name_2",
            "element",
            id="second-argument-of-a-two-argument-step",
        ),
        pytest.param('I click the "{action}" link for hardware tier with name "{tier_name}"', "action", "link", id="noun-after-the-field"),
        pytest.param('I write "{value}" into the input "{name}"', "value", "data", id="value-typed-into-a-field"),
        pytest.param('I write "{value}" into the input "{name}"', "name", "input", id="the-field-typed-into"),
        pytest.param('I open the project "{project_name}"', "project_name", "data", id="data-noun"),
        pytest.param('I create a datasource "{datasource_name}" with username "{username}"', "username", "opaque", id="opaque-username"),
        pytest.param('I request the async prediction "{prediction_id}"', "prediction_id", "opaque", id="opaque-id-beats-data-noun"),
        pytest.param('I sleep for "{value}" seconds', "value", "opaque", id="seconds-follow-the-field"),
        pytest.param('I wait up to "{seconds}" seconds to see the text "{text}"', "seconds", "opaque", id="opaque-seconds"),
        pytest.param('I add a goal with name "{goal_name}" via domino API', "goal_name", "data", id="unclassified-in-an-api-step-is-data"),
        pytest.param('I add a goal with name "{goal_name}" via Jira API', "goal_name", "data", id="any-api"),
        pytest.param('I log "{message}" at level "{level}"', "message", "unknown", id="unclassified-is-unknown"),
        pytest.param('I log "{message}" at level "{level}"', "level", "opaque", id="opaque-level"),
        pytest.param('I click the button "{name}"', "no_such_field", "unknown", id="missing-field-is-unknown"),
    ],
)
def test_classify_argument(pattern: str, arg_name: str, expected: str) -> None:
    assert step_kinds.classify_argument(pattern, arg_name) == expected


def test_overrides_win_over_the_noun_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(step_kinds.OVERRIDES, ('I click the button "{name}"', "name"), step_kinds.DATA)

    assert step_kinds.classify_argument('I click the button "{name}"', "name") == "data"


@pytest.mark.parametrize(
    ("string_kind", "algorithm", "confidence"),
    [
        pytest.param("button", EXACT, 1.0, id="button"),
        pytest.param("table_header", EXACT, 1.0, id="table-header"),
        pytest.param("data", EXACT, 0.3, id="data-is-low-confidence"),
        pytest.param("opaque", NEVER, 1.0, id="opaque"),
        pytest.param("text", FUZZY, 1.0, id="text"),
        pytest.param("element", FUZZY, 1.0, id="element-is-freeform"),
        pytest.param("unknown", FUZZY, 1.0, id="unknown"),
        pytest.param("a-kind-nobody-has-defined", FUZZY, 1.0, id="undefined-kind-falls-back"),
    ],
)
def test_policy_for(string_kind: str, algorithm: str, confidence: float) -> None:
    policy = step_kinds.policy_for(string_kind)

    assert (policy.algorithm, policy.confidence) == (algorithm, confidence)
