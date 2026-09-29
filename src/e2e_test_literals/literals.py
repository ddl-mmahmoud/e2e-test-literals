"""Per-step literal-dependency extraction: registered-step arguments + DataTable headers."""

from __future__ import annotations

from dataclasses import dataclass

from behave.model import Step, Table
from behave.model_type import Argument
from behave.step_registry import registry

from .cucurc import resolve_var

# Parse-field names that are *always* a cucu variable name or a regex pattern in
# every step registration that uses them (verified against every `@step`/`@given`/
# `@when`/`@then` in this repo's `tests/ui/features/steps/**` and cucu's own
# `cucu/steps/*.py`) -- never product UI text, e.g. `I save the app id ... to
# variable "{variable}"` or `I search for the regex "{regex}" ...`. Deliberately
# excludes ambiguous names reused elsewhere for real values in some step (e.g.
# `{url}`, `{token}`, `{userid}`, `{flow_variable}`, `{name}` all also appear as
# genuine captured values in other steps), since the denylist is per parse-field
# name, not per step pattern.
_STRUCTURAL_ARGUMENT_NAMES = frozenset(
    {
        "variable",
        "variable_name",
        "varname",
        "var_export_id",
        "backup_var_name",
        "final_value_varname",
        "git_cred_varname",
        "command_varname",
        "result_varname",
        "stdout_var",
        "stderr_var",
        "exit_code_var",
        "dockerfile_var",
        "regex",
    }
)


@dataclass(frozen=True)
class UnmatchedStep:
    """A step with no registered matcher -- contributes no literals, but is not silently dropped."""

    feature_file: str
    scenario_name: str
    step_text: str
    line: int


@dataclass(frozen=True)
class Source:
    """Where a literal dependency came from: `kind` is `"direct"` (Pass A, straight from a
    `.feature` file) or `"templated"` (Pass B, hardcoded inside a templatized step's
    `run_steps` body -- see templated.py); `file` and `line` pinpoint the location within
    that kind."""

    kind: str
    file: str
    line: int


def literals_from_arguments(
    arguments: list[Argument], source: Source, step_text: str, static_vars: dict[str, str]
) -> list[dict]:
    """Every *named* captured argument whose value is a str (excludes cucu's `nth`-typed ints, etc.).

    An unnamed argument (`argument.name is None`) comes from a step matched by
    a plain regex group rather than a `parse`-style `{name}` field -- the only
    such step in this suite is cucu's `section_step` (heading markers like
    `* # Some section`), whose captures are a `#`-depth marker and the heading
    text, neither of which is real content a step *uses*; both are dropped.

    A named argument whose parse-field name is in `_STRUCTURAL_ARGUMENT_NAMES`
    is also dropped -- it holds a cucu variable name or a regex pattern, not
    product UI text (see that constant's docstring).
    """
    literals = []
    for argument in arguments:
        if (
            argument.name is not None
            and argument.name not in _STRUCTURAL_ARGUMENT_NAMES
            and isinstance(argument.value, str)
        ):
            value, dynamic = resolve_var(argument.value, static_vars)
            literals.append(
                {
                    "value": value,
                    "kind": "argument",
                    "dynamic": dynamic,
                    "source_kind": source.kind,
                    "source_file": source.file,
                    "source_line": source.line,
                    "step_text": step_text,
                }
            )
    return literals


def literals_from_table(table: Table | None, source: Source, step_text: str, static_vars: dict[str, str]) -> list[dict]:
    """Header-row cells only -- data rows may hold regex wildcards or {VAR} test-data placeholders."""
    if table is None:
        return []
    literals = []
    for heading in table.headings:
        value, dynamic = resolve_var(heading, static_vars)
        literals.append(
            {
                "value": value,
                "kind": "table_column",
                "dynamic": dynamic,
                "source_kind": source.kind,
                "source_file": source.file,
                "source_line": source.line,
                "step_text": step_text,
            }
        )
    return literals


def literals_for_step(
    step: Step,
    feature_file: str,
    scenario_name: str,
    static_vars: dict[str, str],
) -> tuple[list[dict], UnmatchedStep | None]:
    """Literal dependencies for one step, plus an UnmatchedStep if it had no registered match."""
    match = registry.find_match(step)
    if match is None or match.arguments is None:
        return [], UnmatchedStep(feature_file, scenario_name, step.name, step.line)

    source = Source(kind="direct", file=feature_file, line=step.line)
    literals = literals_from_arguments(match.arguments, source, step.name, static_vars)
    literals += literals_from_table(step.table, source, step.name, static_vars)
    return literals, None


def find_match_with_pattern(step: Step):
    """Like `registry.find_match`, but also returns the registered pattern text.

    `StepRegistry.find_match` (used by `literals_for_step` above, mirroring what
    `cucu run` itself does) returns only a `Match` -- it discards the `Matcher`
    (the object holding the original `@step(...)`/`@given(...)`/... registration
    string) once it has extracted arguments from it. Pass B (templated steps,
    see `templated.py`) needs that raw pattern string for two things: tagging a
    TestRail case with which registered patterns its steps used, and recognizing
    when a synthesized/embedded step recurses into one of this repo's own
    templatized steps. Re-implements the small candidate loop from
    `StepRegistry.find_match` (behave/step_registry.py) instead of calling it,
    since it does not expose the matched `Matcher` either.

    Returns `(pattern, Match)`, or `None` if nothing matched.
    """
    candidates = registry.steps[step.step_type]
    more_steps = registry.steps["step"]
    if step.step_type != "step" and more_steps:
        candidates = list(candidates) + list(more_steps)

    for step_definition in candidates:
        result = step_definition.match(step.name)
        if result:
            return step_definition.pattern, result
    return None
