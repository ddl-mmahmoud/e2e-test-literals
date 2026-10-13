"""A fake `internal-e2e-tests-service` tree with just enough routing surface for
`routing.compute_entries`: stand-ins for `build/circleci/run_ui_tests.py` and
`step_run_api_tests.py` (same public names and the same tag/marks logic as the real ones,
trimmed), two feature files and one API test module.

These are stand-ins, not the real files -- tests built on them check this repo's mapping
and storage logic, not that the real routing files still have this shape.
"""

from __future__ import annotations

from pathlib import Path

FAKE_RUN_UI_TESTS = '''
import logging
import sys

logging.basicConfig(stream=sys.stdout, level=logging.INFO)  # like the real file: noisy on import
log = logging.getLogger(__name__)

_ROOT = "features/domino"
_EXCLUDE = ["--tags=~gpu", "--tags=~global-config"]
TEST_TYPE_ROUTING = {
    "cucu": [_ROOT, *_EXCLUDE],
    "smoke": [_ROOT, "--tags=smoke", *_EXCLUDE],
    "gpu": [_ROOT, "--tags=gpu"],
    "global-config": [_ROOT, "--tags=global-config"],
    "nexus-global-config": [_ROOT, "--tags=global-config", "--tags=nexus-only", "--tags=~no-nexus"],
    "nexus": [_ROOT, "--tags=~no-nexus", "--tags=~nexus-workspace", *_EXCLUDE],
    "nexus-only": [_ROOT, "--tags=nexus-only", "--tags=~no-nexus", "--tags=~nexus-workspace", *_EXCLUDE],
    "nexus-workspace": [_ROOT, "--tags=nexus-only", "--tags=~no-nexus", *_EXCLUDE],
    "cloud-staging": ["features/cloud_staging"],
}


def _build_test_args(test_type, results_dir):
    log.info("routing %s", test_type)
    return ["--results", f"{results_dir}/results_ui", *TEST_TYPE_ROUTING[test_type]]
'''

FAKE_STEP_RUN_API_TESTS = '''
API_TEST_TYPES = frozenset({"system-standard", "system-aihub", "system-global-config", "cloud-staging", "gpu"})


def _compute_system_test_marks(test_type, is_core, on_prem):
    if test_type == "system-aihub":
        marks = "aihub and not global_config and not cloud_staging and not gpu"
    elif test_type == "system-standard":
        marks = "not aihub and not global_config and not cloud_staging and not gpu"
    elif test_type.endswith("global-config"):
        marks = "global_config and not cloud_staging and not gpu"
    elif test_type == "cloud-staging":
        marks = "cloud_staging and not global_config and not gpu"
    else:
        marks = "gpu and not global_config and not cloud_staging"
    if is_core:
        marks += " and not extended"
    marks += " and not cloud_only" if on_prem else " and not no_cloud"
    return marks
'''

LOGIN_FEATURE = """\
Feature: Login

  @testrail(1234) @smoke
  Scenario: Login and logout
    Then I should see the text "Welcome"

  @testrail(67895) @nexus-only @no-cloud
  Scenario: Provision a nexus-only workspace
    Then I should see the text "Workspace"
"""

STAGING_FEATURE = """\
Feature: Staging

  @testrail(555)
  Scenario: Staging only
    Then I should see the text "Staging"
"""

GPU_API_TEST = """\
import pytest


@pytest.mark.gpu
@pytest.mark.testrail(7001)
def test_gpu_jobs():
    pass
"""


def write_fake_routing_repo(root: Path) -> Path:
    """Write the fake tree under `root` and return it."""
    circleci = root / "build" / "circleci"
    circleci.mkdir(parents=True)
    (circleci / "run_ui_tests.py").write_text(FAKE_RUN_UI_TESTS)
    (circleci / "step_run_api_tests.py").write_text(FAKE_STEP_RUN_API_TESTS)

    domino = root / "tests" / "ui" / "features" / "domino" / "login"
    domino.mkdir(parents=True)
    (domino / "login.feature").write_text(LOGIN_FEATURE)
    staging = root / "tests" / "ui" / "features" / "cloud_staging"
    staging.mkdir(parents=True)
    (staging / "staging.feature").write_text(STAGING_FEATURE)

    api = root / "tests" / "api" / "tests"
    api.mkdir(parents=True)
    (api / "test_gpu.py").write_text(GPU_API_TEST)
    return root
