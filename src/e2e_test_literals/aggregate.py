"""Aggregate per-step literal dependencies into one entry per TestRail case."""

from __future__ import annotations

from pathlib import Path

from .bootstrap import bootstrap_step_registry
from .cucurc import CUCURC_PATH, is_noise_value, load_static_vars
from .features import discover_feature_files, parse_testrail_scenarios
from .literals import UnmatchedStep, find_match_with_pattern, literals_for_step


def _dedupe_literals(literals: list[dict]) -> list[dict]:
    """De-dupe literal dependencies, dropping cucu-variable noise (see `cucurc.is_noise_value`)
    along the way -- the single choke point both Pass A and Pass B literals pass through."""
    deduped: dict[tuple, dict] = {}
    for literal in literals:
        if is_noise_value(literal["value"]):
            continue
        key = (
            literal["value"],
            literal["kind"],
            literal["dynamic"],
            literal["source_kind"],
            literal["source_file"],
            literal["source_line"],
        )
        deduped.setdefault(key, literal)
    return list(deduped.values())


def collect_direct_literals(repo_root: Path) -> tuple[dict[int, dict], list[UnmatchedStep]]:
    """Pass A: literals visible directly in .feature files (steps + DataTables).

    Returns a dict keyed by TestRail ID (scenario metadata + literals) and the
    list of steps that had no registered match.
    """
    bootstrap_step_registry(repo_root)
    static_vars = load_static_vars(repo_root / CUCURC_PATH)

    cases: dict[int, dict] = {}
    unmatched: list[UnmatchedStep] = []

    for feature_path in discover_feature_files(repo_root):
        for scenario in parse_testrail_scenarios(feature_path, repo_root):
            case = cases.setdefault(scenario.testrail_id, {"scenarios": [], "literals": [], "patterns": set()})
            case["scenarios"].append(
                {
                    "feature_file": scenario.feature_file,
                    "scenario_name": scenario.scenario_name,
                    "tags": list(scenario.tags),
                }
            )
            for step in scenario.steps:
                step_literals, unmatched_step = literals_for_step(
                    step, scenario.feature_file, scenario.scenario_name, static_vars
                )
                case["literals"].extend(step_literals)
                if unmatched_step is not None:
                    unmatched.append(unmatched_step)
                else:
                    # Track which registered step pattern this step matched (distinct
                    # from the literals just extracted from it) so a Pass B run can
                    # later join its own resolved literals onto this same case by
                    # exact pattern-text equality -- see merge_templated_literals.
                    found = find_match_with_pattern(step)
                    if found is not None:
                        case["patterns"].add(found[0])

    for case in cases.values():
        case["literals"] = _dedupe_literals(case["literals"])
        case["patterns"] = sorted(case["patterns"])

    return cases, unmatched


def merge_templated_literals(
    cases: dict[int, dict],
    templated_literals_by_step_pattern: dict[str, list[dict]] | None,
) -> dict[int, dict]:
    """Merge in "Pass B" literals: hardcoded UI text found inside templatized custom
    steps' `run_steps` bodies (see `templated.py`), keyed by registered step-pattern
    text.

    For every case, for every distinct pattern one of its steps matched
    (`case["patterns"]`, populated by `collect_direct_literals`), any literals
    `templated_literals_by_step_pattern` has for that same pattern text are added
    to the case's literal list and deduped. Those literal dicts already carry
    their own `source_kind: "templated"` / `source_file` / `source_line`
    (pointing at the `run_steps` call site that hardcoded them, per
    `templated.resolve_templated_literals`), so no re-tagging is needed here.

    Passing `None` or `{}` (Pass B skipped, e.g. `--skip-templated`) is a no-op.
    """
    if not templated_literals_by_step_pattern:
        return cases
    for case in cases.values():
        for pattern in case.get("patterns", []):
            templated_literals = templated_literals_by_step_pattern.get(pattern)
            if templated_literals:
                case["literals"].extend(templated_literals)
        case["literals"] = _dedupe_literals(case["literals"])
    return cases


def build_case_index(
    repo_root: Path,
    *,
    templated_literals_by_step_pattern: dict[str, list[dict]] | None = None,
) -> tuple[dict[int, dict], list[UnmatchedStep]]:
    """Full pipeline: bootstrap, parse, match, resolve vars, aggregate (Pass A), then
    merge in Pass B's templated-step literals if TEMPLATED_LITERALS_BY_STEP_PATTERN
    is given (see `templated.resolve_templated_literals` and `merge_templated_literals`).
    """
    cases, unmatched = collect_direct_literals(repo_root)
    cases = merge_templated_literals(cases, templated_literals_by_step_pattern)
    return cases, unmatched
