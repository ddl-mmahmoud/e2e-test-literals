"""Static `{VAR}` resolution against `tests/ui/features/cucurc.yml`."""

from __future__ import annotations

import re
from pathlib import Path

from ruamel.yaml import YAML

CUCURC_PATH = "tests/ui/features/cucurc.yml"

# Per-run dynamic built-ins that can never be resolved from the static, version-
# controlled cucurc.yml:
#   - SCENARIO_RUN_ID / FEATURE_RUN_ID: set per-scenario/feature by cucu itself
#     (cucu/environment.py before_scenario/before_feature hooks).
#   - SCENARIO_RUN_PWD: generated per-scenario by this repo's own
#     tests/ui/features/environment.py before_scenario hook.
#   - SCENARIO_RESULTS_DIR / SCENARIO_DOWNLOADS_DIR / SCENARIO_LOGS_DIR: per-scenario
#     result paths, also set by cucu/environment.py.
#   - BASEURL / API_BASEURL / DOMINO_HOST: deployment-specific, written into the
#     generated, gitignored tests/ui/cucurc.yml at setup time
#     (tests/setup/setup_test_deployment.py) -- see tests/ui/README.md.
KNOWN_DYNAMIC_VARS = frozenset(
    {
        "SCENARIO_RUN_ID",
        "SCENARIO_RUN_PWD",
        "FEATURE_RUN_ID",
        "SCENARIO_RESULTS_DIR",
        "SCENARIO_DOWNLOADS_DIR",
        "SCENARIO_LOGS_DIR",
        "BASEURL",
        "API_BASEURL",
        "DOMINO_HOST",
    }
)

_VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Any {UPPER_CASE_VAR}-shaped token left in a value (whole value or substring) means
# resolve_var could not statically resolve it -- either a known per-run built-in
# (KNOWN_DYNAMIC_VARS) or something absent from cucurc.yml entirely. Either way it is
# not a fixed literal we can compare across runs.
_UPPER_VAR_TOKEN_RE = re.compile(r"\{[A-Z0-9_]+\}")

# A value that is nothing but a number (optionally signed/decimal) carries no
# comparable content of its own -- e.g. a bare count or index captured by a step.
_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")

# Structurally test-infrastructure values that can never be a product UI string
# literal, regardless of which step captured them -- confirmed against a sample
# of scraped frontend string literals: none of these shapes ever matched (see
# oneoffs/match_ndjson_literals.py). Kept conservative (explicit extension list,
# no bare-word hex heuristics) to avoid dropping real product copy.
_URL_RE = re.compile(r"^https?://\S+$")
_GIT_REMOTE_RE = re.compile(r"^\S+@[\w.-]+:\S+\.git$")
_EMAIL_RE = re.compile(r"^[^\s@/:]+@[^\s@/:]+\.[^\s@/:]+$")
# git short/full commit SHAs and Mongo ObjectIds are both bare lowercase-hex
# tokens in this length range; hex-only keeps this from ever matching an
# English word.
_HEX_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_FILE_PATH_EXTENSIONS = (
    "py|sh|csv|json|ya?ml|ipynb|txt|png|jpe?g|gif|pdf|zip|tar|gz|md|rmd|r|"
    "cfg|ini|log|xlsx|parquet|h5|pkl|pt|onnx|xpt"
)
_FILE_PATH_RE = re.compile(rf"^[\w./-]+\.({_FILE_PATH_EXTENSIONS})$", re.IGNORECASE)


def load_static_vars(cucurc_path: Path) -> dict[str, str]:
    """Top-level scalar keys from a cucurc.yml-shaped file, stringified.

    Only plain top-level scalars (str/int/float/bool) are "statically
    resolvable" literal values; anything else (a list, a nested mapping) is
    not a resolvable {VAR} substitution and is skipped.
    """
    data = YAML(typ="safe").load(cucurc_path.read_text()) or {}
    return {key: str(value) for key, value in data.items() if isinstance(value, (str, int, float, bool))}


def resolve_var(value: str, static_vars: dict[str, str]) -> tuple[str, bool]:
    """Substitute `{NAME}` tokens in VALUE from STATIC_VARS.

    Returns `(resolved_value, dynamic)`. `dynamic` is True whenever any `{NAME}`
    token in VALUE could not be statically resolved -- either it names a known
    per-run built-in (KNOWN_DYNAMIC_VARS) or it is simply absent from
    STATIC_VARS. We never fabricate a value for those: the raw `{NAME}` token is
    left in place in the returned string.
    """
    if "{" not in value:
        return value, False

    dynamic = False

    def _substitute(match: re.Match[str]) -> str:
        nonlocal dynamic
        name = match.group(1)
        if name not in KNOWN_DYNAMIC_VARS and name in static_vars:
            return static_vars[name]
        dynamic = True
        return match.group(0)

    return _VAR_RE.sub(_substitute, value), dynamic


def is_noise_value(value: str) -> bool:
    """True for a (post-`resolve_var`) literal value that is cucu-variable noise, not
    a product constant that can mismatch.

    - A value that is exactly one `{VAR}` token (e.g. `{USER_NAME}`) is itself a
      cucu variable reference, never a fixed literal.
    - Any `{UPPER_CASE_VAR}`-shaped token left inside a value, even as part of a
      larger value (e.g. `Project-{SCENARIO_RUN_ID}`), means it is per-run-unique
      or otherwise unresolved -- never a fixed literal that can mismatch.
    - Empty or whitespace-only values carry no comparable content.
    - A bare number (e.g. `3`, `-1`, `2.5`) carries no comparable content either.
    - A URL, git remote (scp-style `user@host:path.git`), email address, bare
      hex token (commit SHA / Mongo ObjectId), or file path with a recognized
      extension is test-infrastructure data, not product UI copy.
    """
    if not value.strip():
        return True
    if _VAR_RE.fullmatch(value):
        return True
    if _UPPER_VAR_TOKEN_RE.search(value):
        return True
    if _NUMBER_RE.fullmatch(value.strip()):
        return True
    if _URL_RE.match(value) or _GIT_REMOTE_RE.match(value) or _EMAIL_RE.match(value):
        return True
    if _HEX_TOKEN_RE.fullmatch(value):
        return True
    return bool(_FILE_PATH_RE.match(value))
