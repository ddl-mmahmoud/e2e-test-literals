"""Unit tests for e2e_test_literals/templated.py (Pass B: templated steps).

Covers the pure AST-extraction and graph-merge/cycle-guard pieces with fabricated
inputs (no real step-file corpus needed), plus one real end-to-end check that
Pass B actually resolves a known templatized step
(`project_steps.py`'s `add_launcher_to_source_project`) against a real
`internal-e2e-tests-service` checkout -- skipped unless one is available (see
`E2E_TEST_LITERALS_REPO_ROOT` below), since this project doesn't carry a copy of
that repo's real step-file corpus itself.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest
import pytest_check as check

from e2e_test_literals.bootstrap import bootstrap_step_registry
from e2e_test_literals.templated import (
    SENTINEL,
    _extract_run_steps_text,
    _merge_pattern_literals,
    _OwnLiteralsAndDependencies,
    _sentinel_text,
    resolve_templated_literals,
)


def _joined_str_from_source(source: str) -> ast.expr:
    """Parse SOURCE (a single f-string expression, e.g. 'f\"...\"') and return its AST node."""
    module = ast.parse(source, mode="eval")
    return module.body


def _function_from_source(source: str) -> ast.FunctionDef:
    module = ast.parse(source)
    (func_def,) = module.body
    assert isinstance(func_def, ast.FunctionDef)
    return func_def


# -- AST extraction: Constant vs. FormattedValue segments -------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param('"Save"', "Save", id="plain-string-constant"),
        pytest.param(
            'f"""\n And I wait to click the button "New Launcher"\n And I click the button "Save"\n"""',
            '\n And I wait to click the button "New Launcher"\n And I click the button "Save"\n',
            id="fstring-with-no-interpolations-is-pure-literal-text",
        ),
    ],
)
def test_sentinel_text_pure_literal_shapes(source: str, expected: str) -> None:
    node = _joined_str_from_source(source)
    check.equal(_sentinel_text(node), expected)


def test_sentinel_text_mirrors_add_launcher_to_source_project_shape() -> None:
    # Mirrors project_steps.py's add_launcher_to_source_project: a mix of pure
    # literal button/input text and the outer step's own f-string parameters
    # (launcher_name/project_name/user_name), which must never be captured as
    # literal values -- only their *presence* (as a sentinel) matters.
    source = (
        'f"""\n'
        '    When I navigate to the url "{{BASEURL}}/u/{user_name}/{project_name}/endpoints/launchers"\n'
        '     And I wait to click the button "New Launcher"\n'
        '     And I wait to write "{launcher_name}" into the input "Enter Launcher Name"\n'
        '     And I click the button "Save"\n'
        '"""'
    )
    node = _joined_str_from_source(source)

    text = _sentinel_text(node)

    check.is_not_none(text)
    if text is not None:
        check.is_in("New Launcher", text)
        check.is_in("Enter Launcher Name", text)
        check.is_in("Save", text)
        check.is_in("{BASEURL}", text, msg="an escaped {{BASEURL}} is pure literal text, not an interpolation")
        check.is_not_in("launcher_name", text, msg="the parameter name itself must never leak into the text")
        check.equal(
            text.count(SENTINEL), 3, msg="user_name, project_name, and launcher_name are each one FormattedValue"
        )


@pytest.mark.parametrize(
    ("source", "description"),
    [
        pytest.param('"a" + "b"', "string concatenation", id="binop-concatenation"),
        pytest.param('"{}".format("x")', ".format() call", id="dot-format-call"),
        pytest.param("x if y else z", "a conditional expression", id="conditional-expression"),
    ],
)
def test_sentinel_text_returns_none_for_non_literal_shapes(source: str, description: str) -> None:
    node = _joined_str_from_source(source)
    check.is_none(_sentinel_text(node), msg=f"{description} must not be evaluated")


# -- run_steps() argument extraction (dedent-unwrap, local-variable back-reference) --


def test_extract_run_steps_text_direct_fstring_argument() -> None:
    func_def = _function_from_source(
        'def add_launcher(ctx, launcher_name):\n    run_steps(ctx, f"""And I click the button "Save"\n""")\n'
    )
    call = next(
        node
        for node in ast.walk(func_def)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "run_steps"
    )

    text, reason = _extract_run_steps_text(call, func_def)

    check.is_none(reason)
    check.is_in("Save", text or "")


def test_extract_run_steps_text_follows_single_static_local_assignment() -> None:
    # Mirrors admin_steps.py's complete_electronic_signature_workflow shape:
    # `steps = f"..."` then `run_steps(ctx, steps)`.
    func_def = _function_from_source(
        'def do_thing(ctx, name):\n    steps = f"""And I click the button "{name}"\n"""\n    run_steps(ctx, steps)\n'
    )
    call = next(
        node
        for node in ast.walk(func_def)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "run_steps"
    )

    text, reason = _extract_run_steps_text(call, func_def)

    check.is_none(reason)
    check.is_true(SENTINEL in (text or ""))


def test_extract_run_steps_text_unresolvable_for_reassigned_local_variable() -> None:
    # Mirrors login_steps.py's shape where the text is built by concatenating a
    # helper-function call with a literal -- not a single static assignment.
    func_def = _function_from_source(
        "def do_thing(ctx, name):\n"
        "    steps = get_steps(name)\n"
        '    steps = steps + "\\nThen I wait to see the text \\"done\\""\n'
        "    run_steps(ctx, steps)\n"
    )
    call = next(
        node
        for node in ast.walk(func_def)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "run_steps"
    )

    text, reason = _extract_run_steps_text(call, func_def)

    check.is_none(text)
    check.is_not_none(reason)


# -- Recursive merge + cycle guard (fabricated pattern graph, no real step files) --


def test_merge_pattern_literals_follows_a_real_dependency_chain() -> None:
    literal_c = {
        "value": "C-literal",
        "kind": "argument",
        "dynamic": False,
        "source_kind": "templated",
        "source_file": "fake.py",
        "source_line": 1,
    }
    literal_d = {
        "value": "D-literal",
        "kind": "argument",
        "dynamic": False,
        "source_kind": "templated",
        "source_file": "fake.py",
        "source_line": 2,
    }
    graph = {
        "C": _OwnLiteralsAndDependencies(literals=[literal_c], dependency_patterns=["D"], any_failure=False),
        "D": _OwnLiteralsAndDependencies(literals=[literal_d], dependency_patterns=[], any_failure=False),
    }

    merged = _merge_pattern_literals("C", graph, resolved={})

    values = {literal["value"] for literal in merged}
    check.equal(values, {"C-literal", "D-literal"})


def test_merge_pattern_literals_breaks_a_direct_two_pattern_cycle() -> None:
    literal_a = {
        "value": "A-literal",
        "kind": "argument",
        "dynamic": False,
        "source_kind": "templated",
        "source_file": "fake.py",
        "source_line": 1,
    }
    literal_b = {
        "value": "B-literal",
        "kind": "argument",
        "dynamic": False,
        "source_kind": "templated",
        "source_file": "fake.py",
        "source_line": 2,
    }
    graph = {
        "A": _OwnLiteralsAndDependencies(literals=[literal_a], dependency_patterns=["B"], any_failure=False),
        "B": _OwnLiteralsAndDependencies(literals=[literal_b], dependency_patterns=["A"], any_failure=False),
    }

    # The call must terminate (a naive implementation without a cycle guard would
    # recurse forever between A and B) and still recover both patterns' literals.
    merged = _merge_pattern_literals("A", graph, resolved={})

    values = {literal["value"] for literal in merged}
    check.equal(values, {"A-literal", "B-literal"})


def test_merge_pattern_literals_breaks_a_self_cycle() -> None:
    literal_e = {
        "value": "E-literal",
        "kind": "argument",
        "dynamic": False,
        "source_kind": "templated",
        "source_file": "fake.py",
        "source_line": 1,
    }
    graph = {"E": _OwnLiteralsAndDependencies(literals=[literal_e], dependency_patterns=["E"], any_failure=False)}

    merged = _merge_pattern_literals("E", graph, resolved={})

    check.equal([literal["value"] for literal in merged], ["E-literal"])


def test_merge_pattern_literals_returns_empty_for_a_pattern_that_is_not_templatized() -> None:
    # A leaf UI-action step matched inside another templatized step's expansion --
    # present as a dependency name, but never a key of its own in the graph.
    graph: dict = {}

    check.equal(_merge_pattern_literals("not a templatized step", graph, resolved={}), [])


# -- Real end-to-end: add_launcher_to_source_project ------------------------------

_REPO_ROOT_ENV_VAR = "E2E_TEST_LITERALS_REPO_ROOT"


@pytest.mark.skipif(
    not os.environ.get(_REPO_ROOT_ENV_VAR),
    reason=f"set {_REPO_ROOT_ENV_VAR} to a local internal-e2e-tests-service checkout to run this",
)
def test_resolve_templated_literals_real_repo_add_launcher_to_source_project() -> None:
    repo_root = Path(os.environ[_REPO_ROOT_ENV_VAR])
    bootstrap_step_registry(repo_root)

    resolved, unresolvable = resolve_templated_literals(repo_root)

    pattern = 'I add a launcher with name "{launcher_name}" to the project "{project_name}" owned by "{user_name}"'
    check.is_not_in(pattern, unresolvable)
    check.is_in(pattern, resolved)

    literals = resolved.get(pattern, [])
    values = {literal["value"] for literal in literals}
    check.is_in("New Launcher", values)
    check.is_in("Enter Launcher Name", values)
    check.is_in("Save", values)
    check.is_true(all("step_text" in literal for literal in literals))
    check.is_true(
        any(literal["value"] == "New Launcher" and "New Launcher" in literal["step_text"] for literal in literals)
    )

    for literal in literals:
        check.is_not_in("launcher_name", literal["value"])
        check.is_not_in("project_name", literal["value"])
        check.is_not_in("user_name", literal["value"])
