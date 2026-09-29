"""Pass B: literals hardcoded inside templatized custom steps' `run_steps` bodies.

A "templatized step" is a custom `@step(...)` (this repo never uses
`@given`/`@when`/`@then` directly -- see the collab plan) whose implementation
calls `run_steps(ctx, <gherkin-ish text>)`, re-matching that text against the
same step registry at runtime. `.feature` files only ever show the one-line
call to the outer step; any UI literal hardcoded inside the `run_steps` text
(e.g. `'I wait to click the button "New Launcher"'`) is invisible to Pass A
(`aggregate.collect_direct_literals`), which only walks `.feature` files.

This module statically finds those hidden literals via AST inspection only --
no execution, no evaluation of arbitrary expressions (see
cucu-literal-dependency-mapping-plan.md, Q3). A templatized step whose
`run_steps` argument is too dynamic to read statically (string concatenation
across statements, a `.format()` call, an if/else branch, an unresolved local
variable, ...) is recorded as "unresolvable" rather than guessed.

Scope is `tests/ui/features/steps/**/*.py` only -- cucu's own library steps are
out of scope (see the collab plan and CLAUDE.md context for this changeset).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from behave.model import Step
from behave.parser import ParserError, parse_feature

from .aggregate import _dedupe_literals
from .bootstrap import bootstrap_step_registry
from .cucurc import CUCURC_PATH, load_static_vars, resolve_var
from .literals import _STRUCTURAL_ARGUMENT_NAMES, Source, find_match_with_pattern

STEPS_DIR = "tests/ui/features/steps"

# cucu only re-exports behave's @step decorator under this repo's own custom
# step files (see the collab plan, "Step matching is exact, not fuzzy") -- but
# guard for @given/@when/@then too in case a future step module uses them.
_STEP_DECORATOR_NAMES = frozenset({"step", "given", "when", "then"})

# Placeholder substituted for every Python f-string interpolation
# (`ast.FormattedValue`) inside a `run_steps` argument. Chosen to be something
# that could never appear in real Gherkin/UI text, so a reconstructed step's
# captured argument value can be tested for "did this come from an
# interpolation" with a simple substring check.
SENTINEL = "\x00CUCU_FMT\x00"

# Bounds purely to make cycles/pathological call graphs impossible to loop on
# forever; the real call chains found in this repo are only a few hops deep
# (see e.g. extension_steps.py's create_automl_extension_admin_project_and_publish_extension_steps).
_MAX_HELPER_FOLLOW_DEPTH = 8
_MAX_PATTERN_RECURSION_DEPTH = 20


@dataclass(frozen=True)
class RunStepsCallSite:
    """One `run_steps(ctx, <text>)` call reachable from a templatized step's own body."""

    call: ast.Call
    enclosing_func: ast.FunctionDef
    file: str  # relative to repo root
    line: int


def _decorator_pattern(decorator: ast.expr) -> str | None:
    """The literal pattern string for a `@step(...)`-family decorator, or None if computed.

    A handful of custom steps (all under `steps/api/`, none observed under
    `steps/ui/` -- see the collab plan) build their pattern with an f-string
    (e.g. `@step(f'I create a {visibility.lower()} project ...')`). Those are
    registered under their *rendered* text at runtime, which by definition
    cannot appear verbatim in any `.feature` file either, so skipping them here
    loses nothing Pass A could have joined against.
    """
    if not (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Name)):
        return None
    if decorator.func.id not in _STEP_DECORATOR_NAMES:
        return None
    if not decorator.args or not isinstance(decorator.args[0], ast.Constant):
        return None
    value = decorator.args[0].value
    return value if isinstance(value, str) else None


def _module_functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    """Every function defined anywhere in the module, keyed by name (last one wins)."""
    functions: dict[str, ast.FunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            functions[node.name] = node
    return functions


def _run_steps_call_sites(
    func_def: ast.FunctionDef,
    functions_by_name: dict[str, ast.FunctionDef],
    visited: frozenset[str] = frozenset(),
    depth: int = 0,
) -> list[ast.Call]:
    """Every `run_steps(...)` call reachable by statically walking FUNC_DEF's body.

    Many templatized steps in this repo do not call `run_steps` directly --
    the decorated function delegates to a plain module-level helper that
    contains the actual call (e.g. `tags_steps.py`'s `create_new_tag_step` ->
    `create_new_tag_steps`; `extension_steps.py`'s multi-hop
    `create_automl_extension_admin_project_and_publish_extension` ->
    `..._steps` -> `prepare_automl_extension_admin_project_steps` /
    `publish_app_and_create_extension_steps`). This follows one call-graph hop
    at a time into any same-module function called by name, bounded by
    `_MAX_HELPER_FOLLOW_DEPTH` and a per-chain `visited` set so self- or
    mutual-recursion cannot loop forever. `ast.walk` already descends into
    nested `def`s (e.g. a closure a helper returns and hands to
    `register_after_this_scenario_hook`), so those are covered for free
    without any extra handling.

    This does not execute anything -- it only follows `Call(func=Name(...))`
    edges that resolve to a function defined in the same file.
    """
    call_sites: list[ast.Call] = []
    for node in ast.walk(func_def):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == "run_steps":
            call_sites.append(node)
        elif (
            isinstance(node.func, ast.Name)
            and node.func.id in functions_by_name
            and node.func.id not in visited
            and depth < _MAX_HELPER_FOLLOW_DEPTH
        ):
            call_sites.extend(
                _run_steps_call_sites(
                    functions_by_name[node.func.id],
                    functions_by_name,
                    visited | {node.func.id},
                    depth + 1,
                )
            )
    return call_sites


@dataclass
class _DiscoveryResult:
    call_sites_by_pattern: dict[str, list[RunStepsCallSite]] = field(default_factory=dict)
    skipped_computed_patterns: list[str] = field(default_factory=list)


def discover_templated_steps(repo_root: Path) -> _DiscoveryResult:
    """AST-scan every `tests/ui/features/steps/**/*.py` file for templatized steps.

    A "templatized step" here means: a `@step(...)`-decorated function whose
    pattern is a plain string constant, and which (directly, or transitively
    through same-module helper functions -- see `_run_steps_call_sites`) calls
    `run_steps(...)`. Decorated functions with no reachable `run_steps` call
    are ordinary UI-action steps and are simply not templatized -- they are
    not recorded here at all (not an error, not "unresolvable").
    """
    result = _DiscoveryResult()
    steps_dir = repo_root / STEPS_DIR

    for path in sorted(steps_dir.rglob("*.py")):
        file_label = path.relative_to(repo_root).as_posix()
        tree = ast.parse(path.read_text(), filename=file_label)
        functions_by_name = _module_functions(tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            patterns = [_decorator_pattern(decorator) for decorator in node.decorator_list]
            patterns = [pattern for pattern in patterns if pattern is not None]
            if not patterns:
                # Not `@step`-decorated at all (a plain helper), or decorated by
                # something else entirely -- not a templatized-step entry point
                # in its own right (it may still be reached via `_run_steps_call_sites`
                # from whatever *does* call it).
                is_step_decorated = any(
                    isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Name)
                    and dec.func.id in _STEP_DECORATOR_NAMES
                    for dec in node.decorator_list
                )
                if is_step_decorated:
                    result.skipped_computed_patterns.append(
                        f"{file_label}:{node.lineno} {node.name} (computed pattern)"
                    )
                continue

            call_sites = _run_steps_call_sites(node, functions_by_name)
            if not call_sites:
                continue

            for pattern in patterns:
                sites = result.call_sites_by_pattern.setdefault(pattern, [])
                sites.extend(
                    RunStepsCallSite(call=call, enclosing_func=node, file=file_label, line=call.lineno)
                    for call in call_sites
                )

    return result


def _unwrap_dedent(node: ast.expr) -> ast.expr:
    """`textwrap.dedent(X)` / `dedent(X)` -> X. Only strips leading whitespace, never
    changes literal text content, so it is always safe to see through."""
    if isinstance(node, ast.Call) and len(node.args) == 1:
        func = node.func
        is_dedent = (isinstance(func, ast.Name) and func.id == "dedent") or (
            isinstance(func, ast.Attribute) and func.attr == "dedent"
        )
        if is_dedent:
            return node.args[0]
    return node


def _sentinel_text(node: ast.expr) -> str | None:
    """Reduce NODE to its literal text with every f-string interpolation replaced by
    `SENTINEL`, if NODE is a plain string constant or an f-string (`ast.JoinedStr`)
    made only of `Constant` and `FormattedValue` parts. Returns None for any other
    shape (concatenation, `.format()`, a conditional expression, ...) -- those are
    not evaluated, per this module's docstring."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                parts.append(SENTINEL)
            else:
                return None
        return "".join(parts)
    return None


def _single_static_assignment(name: str, func_def: ast.FunctionDef) -> ast.expr | None:
    """A local variable's value, IF it is assigned exactly once at FUNC_DEF's top
    level (`login_steps.py`'s `steps = f\"\"\"...\"\"\"` followed by
    `run_steps(ctx, steps)` is the common shape this exists for). Reassignment, or
    an assignment found only inside a branch/loop/try (not scanned -- only
    `func_def.body`'s own statements are), makes the value ambiguous without
    executing the function, so this returns None and the caller marks the whole
    call unresolvable rather than guessing which value is live."""
    assignments = [
        stmt.value
        for stmt in func_def.body
        if isinstance(stmt, ast.Assign)
        and len(stmt.targets) == 1
        and isinstance(stmt.targets[0], ast.Name)
        and stmt.targets[0].id == name
    ]
    return assignments[0] if len(assignments) == 1 else None


def _extract_run_steps_text(call: ast.Call, enclosing_func: ast.FunctionDef) -> tuple[str | None, str | None]:
    """(sentinel_substituted_text, unresolvable_reason) for one `run_steps(...)` call.

    Exactly one of the two is None. Handles: a direct string/f-string argument,
    one level of `textwrap.dedent(...)` wrapping, and one level of local-variable
    back-reference to a single, unambiguous assignment (also dedent-unwrapped).
    """
    if len(call.args) >= 2:
        arg = call.args[1]
    else:
        arg = next((keyword.value for keyword in call.keywords if keyword.arg in ("steps", "text")), None)
        if arg is None:
            return None, "run_steps call has no text argument"

    arg = _unwrap_dedent(arg)
    if isinstance(arg, ast.Name):
        assigned = _single_static_assignment(arg.id, enclosing_func)
        if assigned is None:
            return None, f"variable '{arg.id}' has no single unambiguous static assignment"
        arg = _unwrap_dedent(assigned)

    text = _sentinel_text(arg)
    if text is None:
        return None, "run_steps argument is not a plain string/f-string (concatenation, .format(), a conditional, ...)"
    return text, None


def _parse_embedded_steps(text: str) -> list[Step]:
    """Parse TEXT (a `run_steps` body, sentinel-substituted) into `Step` objects via
    behave's real Gherkin parser, wrapped in a minimal synthetic Feature/Scenario.

    Re-indenting every non-blank line to the same fixed depth (rather than trusting
    the triple-quoted string's own indentation, which varies call-site to call-site)
    is what makes an arbitrary `run_steps` body parse as a valid embedded scenario.
    Raises `behave.parser.ParserError` if TEXT is not valid Gherkin once
    reconstructed -- e.g. a dynamically-built DataTable or `{VAR}`-spliced step
    block (`helpers/table_steps.py`'s `poll_refresh_and_see_table_with_rows`) whose
    FormattedValue sentinel lands on its own line with no leading step keyword.
    Callers catch this per call-site rather than aborting the whole Pass B run.
    """
    lines = ["    " + line.strip() if line.strip() else "" for line in text.splitlines()]
    synthetic = "Feature: synthetic\n  Scenario: synthetic\n" + "\n".join(lines)
    feature = parse_feature(synthetic, filename="<templated-step>")
    if feature is None or not feature.scenarios:
        return []
    return list(feature.scenarios[0].steps)


@dataclass(frozen=True)
class _OwnLiteralsAndDependencies:
    """One templatized step pattern's own (non-recursive) literals, which other
    templatized-step patterns it recursed into (to be merged in too), and whether
    resolving any of its own `run_steps` call sites failed."""

    literals: list[dict]
    dependency_patterns: list[str]
    any_failure: bool


def _own_literals_and_dependencies(
    pattern: str,
    call_sites: list[RunStepsCallSite],
    static_vars: dict[str, str],
) -> _OwnLiteralsAndDependencies:
    """Resolve PATTERN's own `run_steps` call sites against the (already-bootstrapped)
    step registry. Does not recurse -- a matched step that is itself one of this
    repo's templatized-step patterns is recorded in `dependency_patterns` for the
    caller to merge in (see `_merge_pattern_literals`), rather than followed here,
    so cycle detection lives in exactly one place.
    """
    literals: list[dict] = []
    dependency_patterns: list[str] = []
    any_failure = False

    for site in call_sites:
        source = Source(kind="templated", file=site.file, line=site.line)
        text, _reason = _extract_run_steps_text(site.call, site.enclosing_func)
        if text is None:
            any_failure = True
            continue
        try:
            steps = _parse_embedded_steps(text)
        except ParserError:
            any_failure = True
            continue

        for step in steps:
            found = find_match_with_pattern(step)
            if found is None:
                continue
            step_pattern, match = found
            step_text = step.name.replace(SENTINEL, "{...}")
            for argument in match.arguments or []:
                # Unnamed args (e.g. cucu's `section_step` heading markers) aren't
                # real content a step uses, and neither are cucu variable-name /
                # regex parse fields -- see literals.literals_from_arguments and
                # _STRUCTURAL_ARGUMENT_NAMES.
                if (
                    argument.name is not None
                    and argument.name not in _STRUCTURAL_ARGUMENT_NAMES
                    and isinstance(argument.value, str)
                    and SENTINEL not in argument.value
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
            if step.table is not None:
                for heading in step.table.headings:
                    if SENTINEL not in heading:
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
            if step_pattern != pattern:
                dependency_patterns.append(step_pattern)

    return _OwnLiteralsAndDependencies(literals, dependency_patterns, any_failure)


def _merge_pattern_literals(
    pattern: str,
    literals_and_deps_by_pattern: dict[str, _OwnLiteralsAndDependencies],
    resolved: dict[str, list[dict]],
    in_progress: frozenset[str] = frozenset(),
    depth: int = 0,
) -> list[dict]:
    """Recursively merge PATTERN's own literals with those of every templatized-step
    pattern it (transitively) calls into, breaking cycles via IN_PROGRESS.

    Pure and registry-independent -- this is the piece that guards against a
    templatized step recursing into itself, directly or transitively (a real
    example: `automl_extension_steps.py`'s `create_standard_automl_training_job`
    recurses into `open_automl_extension_and_wait_for_dashboard`'s own resolved
    literals). A pattern not present in LITERALS_AND_DEPS_BY_PATTERN at all is not
    itself templatized (e.g. a leaf UI-action step matched inside another
    templatized step's expansion) and contributes nothing of its own.
    """
    if pattern in resolved:
        return resolved[pattern]
    if pattern not in literals_and_deps_by_pattern:
        return []
    if pattern in in_progress or depth >= _MAX_PATTERN_RECURSION_DEPTH:
        return []

    own = literals_and_deps_by_pattern[pattern]
    merged = list(own.literals)
    next_in_progress = in_progress | {pattern}
    for dependency in own.dependency_patterns:
        merged.extend(
            _merge_pattern_literals(dependency, literals_and_deps_by_pattern, resolved, next_in_progress, depth + 1)
        )

    resolved[pattern] = _dedupe_literals(merged)
    return resolved[pattern]


def resolve_templated_literals(repo_root: Path) -> tuple[dict[str, list[dict]], list[str]]:
    """Pass B entry point: statically resolve literals hardcoded inside templatized steps.

    Returns `(pattern -> literal-dependency dicts, list of unresolvable pattern texts)`.
    A pattern lands in the unresolvable list if resolving *any* of its own
    `run_steps` call sites failed (text extraction failed, or the reconstructed
    text did not parse as Gherkin) or if it took part in a call cycle; whatever
    literals other, successfully-resolved call sites for that same pattern did
    produce are still kept in the returned dict -- partial information is better
    than none, and nothing here is fabricated (see this module's docstring).
    """
    bootstrap_step_registry(repo_root)
    static_vars = load_static_vars(repo_root / CUCURC_PATH)
    discovery = discover_templated_steps(repo_root)

    literals_and_deps_by_pattern = {
        pattern: _own_literals_and_dependencies(pattern, call_sites, static_vars)
        for pattern, call_sites in discovery.call_sites_by_pattern.items()
    }

    resolved: dict[str, list[dict]] = {}
    for pattern in literals_and_deps_by_pattern:
        _merge_pattern_literals(pattern, literals_and_deps_by_pattern, resolved)

    unresolvable = {pattern for pattern, own in literals_and_deps_by_pattern.items() if own.any_failure}
    return resolved, sorted(unresolvable)
