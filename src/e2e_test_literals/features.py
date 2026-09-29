"""Gherkin parsing: discover @testrail(####) scenarios and their effective steps."""

from __future__ import annotations

import os

# cucu sets this before it imports behave (cucu/behave_tweaks.py) so that a
# step written with a trailing colon before its DataTable (e.g. "...following:")
# still parses with the colon stripped, matching the (colon-less) registered
# pattern. behave reads the var once, at `behave.parser` import time, so this
# must happen before that import below -- setting it only inside our own
# registry-bootstrap (which imports cucu) would be too late if `behave.parser`
# was already imported by this module first.
os.environ.setdefault("BEHAVE_STRIP_STEPS_WITH_TRAILING_COLON", "yes")

import re
from dataclasses import dataclass
from pathlib import Path

from behave.model import Scenario, Step
from behave.parser import parse_file

FEATURES_DIR = "tests/ui/features"

_TESTRAIL_TAG_RE = re.compile(r"^testrail\((\d+)\)$")


@dataclass(frozen=True)
class TestrailScenario:
    """One @testrail(####)-tagged scenario, with its effective step sequence."""

    testrail_id: int
    feature_file: str  # relative to repo root, e.g. "tests/ui/features/domino/login/login_and_logout.feature"
    scenario_name: str
    tags: tuple[str, ...]
    steps: tuple[Step, ...]  # the file's Background steps (if any) followed by the scenario's own steps


def discover_feature_files(repo_root: Path) -> list[Path]:
    """Every .feature file under tests/ui/features, sorted for stable output."""
    return sorted((repo_root / FEATURES_DIR).rglob("*.feature"))


def _testrail_id(scenario: Scenario) -> int | None:
    for tag in scenario.effective_tags:
        match = _TESTRAIL_TAG_RE.match(tag)
        if match:
            return int(match.group(1))
    return None


def parse_testrail_scenarios(feature_path: Path, repo_root: Path) -> list[TestrailScenario]:
    """Parse one .feature file into its @testrail(####)-tagged scenarios.

    `setup`/`teardown` features may have no such scenarios (they may lack
    @testrail entirely, per repo convention) -- an empty result is expected
    and not an error.
    """
    feature = parse_file(str(feature_path))
    if feature is None:
        return []

    background_steps = tuple(feature.background.steps) if feature.background else ()
    relative_path = feature_path.relative_to(repo_root).as_posix()

    scenarios = []
    for scenario in feature.walk_scenarios():
        testrail_id = _testrail_id(scenario)
        if testrail_id is None:
            continue
        scenarios.append(
            TestrailScenario(
                testrail_id=testrail_id,
                feature_file=relative_path,
                scenario_name=scenario.name,
                tags=tuple(sorted(str(tag) for tag in scenario.effective_tags)),
                steps=background_steps + tuple(scenario.steps),
            )
        )
    return scenarios
