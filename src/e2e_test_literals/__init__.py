"""Map TestRail cases to the UI string-literal / table-column values their cucu scenarios depend on.

Walks `tests/ui/features/**/*.feature` in an `internal-e2e-tests-service` checkout, and
for every Scenario tagged @testrail(####), emits a sqlite database listing every
string-valued captured step argument and every DataTable header cell that scenario's
execution depends on (its own steps + its file's Background). Every such value is
treated as a literal dependency -- there is no literal-vs-data classification table.

Two independently-toggleable passes feed the same database:

- Pass A (always runs): literals visible directly in .feature files.
- Pass B (`templated.py`, skip with --skip-templated): literals hardcoded
  inside templatized custom steps (`run_steps(ctx, <text>)` bodies) that never
  appear in any .feature file. Statically resolved via AST inspection only --
  a templatized step whose run_steps argument is too dynamic to read
  statically is recorded as "unresolvable" rather than guessed.

This project doesn't carry its own copy of `internal-e2e-tests-service` -- it either
points at an existing local checkout (`--repo-root`, e.g. for local dev against a
worktree) or clones/caches one itself (`--repo`/`--ref`/`--repo-cache`, see
`repo_checkout.py`) and materializes just the `tests/` subtree it needs.

To look up which cases are affected by a batch of changed product strings,
use `lookup.py` (in this package): it matches a newline-delimited list of
literals (a file argument, or stdin) against `literals.value` and prints the
case ids, files, and line numbers that depend on each one -- see
`python -m e2e_test_literals.lookup --help`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import click

from .aggregate import build_case_index
from .db import write_sqlite
from .literals import UnmatchedStep
from .repo_checkout import checkout_tree
from .templated import resolve_templated_literals

DEFAULT_OUT = "cucu_literal_deps.sqlite"
DEFAULT_REF = "main"


@dataclass
class IndexResult:
    """Everything one indexing run produces -- kept as a plain struct (rather than the
    CLI printing straight from locals) so a future HTTP wrapper can call
    `index_from_git`/`index_from_local_checkout` directly and hand this back as a
    response body, the same way changed-literals' app.py hands back `extract()`'s
    `list[Finding]`."""

    cases: dict[int, dict]
    unmatched: list[UnmatchedStep]
    templated_patterns_total: int
    unresolvable_templated: list[str]


def index_from_local_checkout(repo_root: Path, *, skip_templated: bool = False) -> IndexResult:
    """Run the full indexing pipeline (Pass A, optionally Pass B) against an existing
    on-disk checkout -- `repo_root` must have `tests/ui/`, `tests/common/`, and
    `tests/helpers/` present (a full `internal-e2e-tests-service` checkout, or the
    materialized subtree `checkout_tree`/`index_from_git` produce)."""
    if skip_templated:
        templated_literals_by_step_pattern = None
        unresolvable_templated: list[str] = []
    else:
        templated_literals_by_step_pattern, unresolvable_templated = resolve_templated_literals(repo_root)

    cases, unmatched = build_case_index(repo_root, templated_literals_by_step_pattern=templated_literals_by_step_pattern)

    return IndexResult(
        cases=cases,
        unmatched=unmatched,
        templated_patterns_total=len(templated_literals_by_step_pattern or {}),
        unresolvable_templated=unresolvable_templated,
    )


def index_from_git(
    repo_url: str,
    ref: str = DEFAULT_REF,
    *,
    repo_cache: Path | None = None,
    skip_templated: bool = False,
) -> IndexResult:
    """Materialize `ref`'s `tests/` subtree out of `repo_url` (see `repo_checkout.checkout_tree`)
    and run the same pipeline as `index_from_local_checkout` against it."""
    with checkout_tree(repo_url, ref, repo_cache=repo_cache) as tree_root:
        return index_from_local_checkout(tree_root, skip_templated=skip_templated)


def _print_unmatched_examples(unmatched: list[UnmatchedStep], limit: int = 5) -> None:
    for step in unmatched[:limit]:
        click.echo(f"  - {step.feature_file}:{step.line} [{step.scenario_name}] {step.step_text}")


def _print_unresolvable_examples(unresolvable: list[str], limit: int = 5) -> None:
    for pattern in unresolvable[:limit]:
        click.echo(f"  - {pattern}")


@click.command()
@click.option(
    "--repo-root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="An existing local internal-e2e-tests-service checkout. Mutually exclusive with --repo.",
)
@click.option(
    "--repo",
    default=None,
    help="Clonable internal-e2e-tests-service git URL or local path. Mutually exclusive with --repo-root.",
)
@click.option("--ref", default=DEFAULT_REF, show_default=True, help="Ref to index when using --repo.")
@click.option(
    "--repo-cache",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help=(
        "Directory for a reusable bare clone of --repo, populated on first use and left in "
        "place afterward. Without this, a full bare clone is made in a temp dir and discarded "
        "when the run finishes."
    ),
)
@click.option(
    "--out",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=f"Output sqlite database path (default: ./{DEFAULT_OUT}).",
)
@click.option(
    "--report-unmatched",
    is_flag=True,
    default=False,
    help="Print a sample of steps that had no registered match.",
)
@click.option(
    "--skip-templated",
    is_flag=True,
    default=False,
    help="Skip Pass B (templated-step AST scan); emit Pass-A-only literals.",
)
@click.option(
    "--report-unresolvable-templated",
    is_flag=True,
    default=False,
    help="Print a sample of templatized steps Pass B could not statically resolve.",
)
def cli(
    repo_root: Path | None,
    repo: str | None,
    ref: str,
    repo_cache: Path | None,
    out: Path | None,
    report_unmatched: bool,
    skip_templated: bool,
    report_unresolvable_templated: bool,
) -> None:
    """Index an internal-e2e-tests-service checkout's tests/ui/features/**/*.feature and
    emit literal/table-column deps per TestRail ID."""
    if bool(repo_root) == bool(repo):
        raise click.UsageError("Exactly one of --repo-root or --repo is required.")

    out_path = out or Path(DEFAULT_OUT)

    if repo_root is not None:
        result = index_from_local_checkout(repo_root, skip_templated=skip_templated)
    else:
        result = index_from_git(repo, ref, repo_cache=repo_cache, skip_templated=skip_templated)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_sqlite(result.cases, out_path)

    literal_count = sum(len(case["literals"]) for case in result.cases.values())
    click.echo(f"TestRail cases: {len(result.cases)}")
    click.echo(f"Literals: {literal_count}")
    click.echo(f"Unmatched steps: {len(result.unmatched)}")
    if not skip_templated:
        resolved_count = result.templated_patterns_total - len(result.unresolvable_templated)
        click.echo(f"Templated step patterns resolved: {resolved_count}")
        click.echo(f"Templated step patterns unresolvable: {len(result.unresolvable_templated)}")
    click.echo(f"Wrote {out_path}")

    if report_unmatched and result.unmatched:
        click.echo("Sample unmatched steps:")
        _print_unmatched_examples(result.unmatched)

    if report_unresolvable_templated and result.unresolvable_templated:
        click.echo("Sample unresolvable templated step patterns:")
        _print_unresolvable_examples(result.unresolvable_templated)
