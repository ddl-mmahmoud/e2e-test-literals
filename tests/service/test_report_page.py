"""The report page's rendering is not exercised here (no browser in this environment); what is:
the HTML wrapper, the safe embedding of the model, a syntax check of the whole script, and the
DOM-free selection/prompt/invocation logic the page runs, executed under node."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from e2e_test_literals.service.report import build_report
from e2e_test_literals.service.report_page import LOGIC_END, LOGIC_START, render_report

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _literal(case_id: int, line: int, **overrides) -> dict:
    base = {
        "value": "Access",
        "string_kind": "link",
        "case_id": case_id,
        "scenario_name": f"Scenario {case_id}",
        "source_file": "tests/a.feature",
        "source_line": line,
        "step_text": 'I click the link "Access"',
        "tags": [f"testrail({case_id})"],
        "match_algorithm": "exact",
        "match_confidence": 1.0,
    }
    base.update(overrides)
    return base


def _result(findings: list[dict], **overrides) -> dict:
    base = {
        "test_repo": "https://github.com/o/tests",
        "test_sha": "a" * 40,
        "literals_repo": "https://github.com/o/product",
        "base_ref": "b" * 40,
        "updated_ref": "c" * 40,
        "pr_number": None,
        "min_invocation_confidence": 0.0,
        "invocation_max_length": 300,
        "extra_cases_allowed": None,
        "missing_cases_allowed": 1,
        "findings": findings,
        "recommended_invocations": {"commands": [], "structured": [], "uncovered": [], "warnings": []},
    }
    base.update(overrides)
    return base


def _finding(line: int, matches: list[dict], text: str = "Access", **overrides) -> dict:
    base = {
        "file": "views/a.html",
        "line": line,
        "column": 1,
        "text": text,
        "context": "html-text",
        "confidence": 0.9,
        "change": "removed",
        "hunk": 0,
        "matched_test_literals": matches,
        "replacements": [],
    }
    base.update(overrides)
    return base


def _run(model: dict, program: str):
    """Evaluates `program` (JS using the page's logic functions and a `model`) under node and
    returns what it passes to `out(...)`, decoded from JSON."""
    html = render_report(_result([]))
    logic = html[html.index(LOGIC_START) : html.index(LOGIC_END)]
    script = f"{logic}\nconst model = {json.dumps(model)};\nconst out = (v) => console.log(JSON.stringify(v));\n{program}"
    done = subprocess.run(["node", "-"], input=script, capture_output=True, text=True, check=True)
    return json.loads(done.stdout)


@pytest.fixture
def model() -> dict:
    return build_report(
        _result(
            [
                _finding(10, [_literal(1, 5), _literal(2, 9)]),
                _finding(20, [_literal(2, 9, step_text='I click the link "Access" again'), _literal(3, 4, match_confidence=0.7)], text="Other"),
            ]
        )
    )


def test_page_wraps_the_embedded_model_and_has_no_templating_placeholder_left():
    html = render_report(_result([_finding(10, [_literal(1, 5)])]))

    assert "__DATA__" not in html
    assert html.startswith("<!doctype html>")
    match = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', html, re.S)
    assert json.loads(match.group(1))["changes"][0]["text"] == "Access"


def test_embedded_model_cannot_close_the_script_element_or_open_a_comment():
    nasty = '</script><!-- <img src=x onerror=alert(1)> &  '
    html = render_report(_result([_finding(10, [_literal(1, 5)], text=nasty)]))
    match = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', html, re.S)

    assert "<" not in match.group(1) and " " not in match.group(1)
    assert json.loads(match.group(1))["changes"][0]["text"] == nasty


@needs_node
def test_the_whole_page_script_parses(tmp_path):
    html = render_report(_result([_finding(10, [_literal(1, 5)])]))
    script = re.findall(r"<script>\n(.*?)</script>", html, re.S)[0]
    path = tmp_path / "page.js"
    path.write_text(script)

    subprocess.run(["node", "--check", str(path)], check=True, capture_output=True)


@needs_node
def test_everything_starts_selected_and_the_prompt_lists_each_distinct_step_once(model):
    prompt = _run(
        model,
        "const off = new Set(); out({state: selectionState(entriesOf(model), off), prompt: promptText(model, off)});",
    )

    assert prompt["state"] == "all"
    lines = prompt["prompt"].split("\n")
    assert lines[0] == model["preamble"]
    assert len(lines) == 1 + 4  # 4 entries, 4 distinct bullets


@needs_node
def test_deselecting_a_group_and_the_selection_state_of_its_ancestors(model):
    result = _run(
        model,
        """
        const off = new Set();
        const first = model.changes[0];
        setSelected(first.groups[0].entries, off, false);
        out({
          group: selectionState(first.groups[0].entries, off),
          change: selectionState(first.groups.flatMap(g => g.entries), off),
          everything: selectionState(entriesOf(model), off),
          other: selectionState(model.changes[1].groups[0].entries, off),
          cases: invocationCaseIds(model, off),
          prompt: promptText(model, off),
        });
        """,
    )

    assert result["group"] == "none"
    assert result["change"] == "none"  # one feature file holds all of this change's entries
    assert result["everything"] == "some"
    assert result["other"] == "all"
    assert result["cases"] == [2, 3]
    assert "Scenario 1" not in result["prompt"]


@needs_node
def test_selecting_one_entry_leaves_its_group_partial(model):
    result = _run(
        model,
        """
        const off = new Set();
        setSelected(entriesOf(model), off, false);
        const entry = model.changes[0].groups[0].entries[0];
        setSelected([entry], off, true);
        out({
          group: selectionState(model.changes[0].groups[0].entries, off),
          all: selectionState(entriesOf(model), off),
          prompt: promptText(model, off).split("\\n").length,
        });
        """,
    )

    assert result == {"group": "some", "all": "some", "prompt": 2}


@needs_node
def test_nothing_selected_means_an_empty_prompt_and_no_cases(model):
    result = _run(
        model,
        """
        const off = new Set();
        setSelected(entriesOf(model), off, false);
        out({prompt: promptText(model, off), cases: invocationCaseIds(model, off), state: selectionState(entriesOf(model), off)});
        """,
    )

    assert result == {"prompt": "", "cases": [], "state": "none"}


@needs_node
def test_only_cases_clearing_the_invocation_confidence_floor_are_recomputed():
    floored = build_report(
        _result(
            [_finding(10, [_literal(1, 5), _literal(3, 4, match_confidence=0.7)])],
            min_invocation_confidence=0.8,
        )
    )

    cases = _run(floored, "out(invocationCaseIds(model, new Set()))")

    assert cases == [1]


@needs_node
def test_recompute_body_carries_the_results_own_settings(model):
    body = _run(model, "out(recomputeBody(model, [2, 3]))")

    assert body == {
        "test_sha": "a" * 40,
        "case_ids": [2, 3],
        "invocation_max_length": 300,
        "extra_cases_allowed": None,
        "missing_cases_allowed": 1,
    }


@needs_node
def test_stored_deselections_are_restored_but_unknown_or_malformed_ones_are_ignored(model):
    known = model["changes"][0]["groups"][0]["entries"][0]["id"]
    result = _run(
        model,
        f"""
        out({{
          good: [...parseDeselected(JSON.stringify([{json.dumps(known)}, "from some other report"]), model)],
          broken: [...parseDeselected("{{not json", model)],
          unset: [...parseDeselected(null, model)],
          wrongType: [...parseDeselected('"x"', model)],
        }});
        """,
    )

    assert result == {"good": [known], "broken": [], "unset": [], "wrongType": []}
