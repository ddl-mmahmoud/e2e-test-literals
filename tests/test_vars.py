"""Unit tests for e2e_test_literals/cucurc.py ({VAR} resolution)."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_check as check

from e2e_test_literals.cucurc import KNOWN_DYNAMIC_VARS, is_noise_value, load_static_vars, resolve_var


@pytest.fixture
def static_vars() -> dict[str, str]:
    return {"WORKSPACE_DEFAULT_HARDWARE_TIER": "Tiny k8s", "SMALL_ENVIRONMENT_HARDWARE_TIER": "Small"}


@pytest.mark.parametrize(
    ("value", "expected_value", "expected_dynamic"),
    [
        pytest.param("Save", "Save", False, id="no-braces-passthrough"),
        pytest.param("{WORKSPACE_DEFAULT_HARDWARE_TIER}", "Tiny k8s", False, id="exact-token-resolved-from-static-map"),
        pytest.param(
            "Select the {SMALL_ENVIRONMENT_HARDWARE_TIER} tier",
            "Select the Small tier",
            False,
            id="substring-token-resolved-from-static-map",
        ),
        pytest.param("{NOT_IN_CUCURC}", "{NOT_IN_CUCURC}", True, id="unknown-token-left-raw-and-marked-dynamic"),
        pytest.param(
            "{BASEURL}/u/{USER_NAME}",
            "{BASEURL}/u/{USER_NAME}",
            True,
            id="multiple-unresolved-tokens-left-raw",
        ),
        pytest.param(
            "{WORKSPACE_DEFAULT_HARDWARE_TIER} then {NOT_IN_CUCURC}",
            "Tiny k8s then {NOT_IN_CUCURC}",
            True,
            id="partial-resolution-still-marked-dynamic-overall",
        ),
    ],
)
def test_resolve_var(static_vars: dict[str, str], value: str, expected_value: str, expected_dynamic: bool) -> None:
    resolved, dynamic = resolve_var(value, static_vars)
    check.equal(resolved, expected_value)
    check.equal(dynamic, expected_dynamic)


def test_resolve_var_known_dynamic_name_wins_even_if_present_in_static_map() -> None:
    # SCENARIO_RUN_ID is a per-run built-in (cucu/environment.py); even if a stale
    # key of the same name existed in cucurc.yml, it must never be "resolved" to
    # a fabricated value.
    static_vars = {"SCENARIO_RUN_ID": "stale-leftover-value"}
    resolved, dynamic = resolve_var("user-{SCENARIO_RUN_ID}", static_vars)
    check.equal(resolved, "user-{SCENARIO_RUN_ID}")
    check.is_true(dynamic)


def test_known_dynamic_vars_includes_documented_builtins() -> None:
    for name in ("SCENARIO_RUN_ID", "SCENARIO_RUN_PWD", "BASEURL", "API_BASEURL", "DOMINO_HOST"):
        check.is_in(name, KNOWN_DYNAMIC_VARS)


def test_load_static_vars_keeps_only_scalar_top_level_keys(tmp_path: Path) -> None:
    cucurc_path = tmp_path / "cucurc.yml"
    cucurc_path.write_text(
        "\n".join(
            [
                'WORKSPACE_DEFAULT_HARDWARE_TIER: "Tiny k8s"',
                "CUCU_BROWSER_WINDOW_HEIGHT: 1080",
                "CUCU_SCREENSHOT_VIDEO: true",
                "CUCU_SECRETS_LIST:",
                "  - ADMIN_PASSWORD",
                "  - KEYCLOAK_PASSWORD",
                "NESTED_MAPPING:",
                "  inner: value",
            ]
        )
    )

    static_vars = load_static_vars(cucurc_path)

    check.equal(static_vars["WORKSPACE_DEFAULT_HARDWARE_TIER"], "Tiny k8s")
    check.equal(static_vars["CUCU_BROWSER_WINDOW_HEIGHT"], "1080")
    check.equal(static_vars["CUCU_SCREENSHOT_VIDEO"], "True")
    check.is_not_in("CUCU_SECRETS_LIST", static_vars, msg="a YAML list is not a resolvable scalar")
    check.is_not_in("NESTED_MAPPING", static_vars, msg="a nested mapping is not a resolvable scalar")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("{USER_NAME}", True, id="whole-value-is-one-var-token"),
        pytest.param("Project-{SCENARIO_RUN_ID}", True, id="contains-scenario-run-id-substring"),
        pytest.param("{SCENARIO_RUN_ID}", True, id="whole-value-is-scenario-run-id"),
        pytest.param("Project-{FEATURE_RUN_ID}", True, id="contains-other-upper-case-var-substring"),
        pytest.param("{BASEURL}/u/{USER_NAME}", True, id="multiple-upper-case-var-tokens"),
        pytest.param("", True, id="empty"),
        pytest.param("   ", True, id="whitespace-only"),
        pytest.param("3", True, id="bare-integer"),
        pytest.param("-1", True, id="bare-negative-integer"),
        pytest.param("2.5", True, id="bare-decimal"),
        pytest.param("Save", False, id="plain-literal"),
        pytest.param("3 items", False, id="number-decorated-with-text-not-bare"),
        pytest.param("Tiny k8s", False, id="resolved-static-value"),
    ],
)
def test_is_noise_value(value: str, expected: bool) -> None:
    check.equal(is_noise_value(value), expected)
