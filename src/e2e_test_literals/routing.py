"""Index-time routing data: which `/test-e2e` test_type/flag combinations select each
TestRail-tagged UI scenario and API test of an `internal-e2e-tests-service` checkout.

The mapping logic is ported from `~/_s/map_test_trigger_coverage.py` (itself a standalone
copy of that repo's `build/circleci/map_test_trigger_coverage.py`): it imports the test
repo's own routing code (`run_ui_tests._build_test_args`,
`step_run_api_tests._compute_system_test_marks`) so the mapping tracks routing changes
rather than hand-copying them. What this module adds is that the *result* is plain data
(see `compute_entries`), meant to be written into the per-revision sqlite (`db.py`'s
`entries`, `entry_labels` and `invocation_candidates` tables) so impact-time code needs
no source from the test repo.

Deliberately stdlib-only and free of package-relative imports: `index_routing` runs it as
a standalone script in an isolated `python -I` subprocess. The repo's routing modules
call `logging.basicConfig` at import and log at INFO on every call, and the script this
was ported from silences that with a process-wide `logging.disable` -- neither is
acceptable inside the long-lived, multi-threaded service, so the evaluation happens in a
throwaway process instead. Any failure there (missing files at an old revision, a repo
refactor, an import error) surfaces as `RoutingError`, which callers record rather than
letting it fail the literal index.

Each entry is one `@testrail(N)`-tagged UI scenario or `@pytest.mark.testrail(N)` API test:

    {"case_id": 1234, "domain": "ui", "file": "tests/ui/features/...", "name": "...",
     "root": "domino" | "cloud_staging" | None,
     "labels": [["tag", "smoke"], ...],              # UI: bare scenario tags; API: pytest marks
     "candidates": [{"test_type": "cucu", "flags": ["on_prem"]}, ...]}

A candidate is one (test_type, flag set) combination whose selectors match the entry.
`flags` only ever lists the flags that are *on*; absent means off. Routing policy (never
`--vcluster`, never `is_core`, test_type preference) is deliberately not applied here --
that is `service/invocations.py`'s job, so a policy change needs no db rebuild.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

RUN_UI_TESTS_SUBPATH = "build/circleci/run_ui_tests.py"
STEP_RUN_API_TESTS_SUBPATH = "build/circleci/step_run_api_tests.py"
UI_FEATURES_SUBPATH = "tests/ui/features"
API_TESTS_SUBPATH = "tests/api/tests"

# What routing needs on top of `repo_checkout.TESTS_SUBPATHS` (which already carries
# tests/ui, hence the feature files). Optional at checkout time: a revision that predates
# these files must still get its literal index.
ROUTING_SUBPATHS = (RUN_UI_TESTS_SUBPATH, STEP_RUN_API_TESTS_SUBPATH, API_TESTS_SUBPATH)

_WORKER_TIMEOUT_SECONDS = 300

# test_types reachable from a real `/test-e2e` trigger. The routing table also has "cloud"
# and "cloud-global-config", but those are dead (not in setup_test_e2e.TEST_TYPE_CONFIGS or
# any e2e-tests-build.yml workflow-template). Hardcoded because the routing files don't
# express it as data.
UI_TEST_TYPES = [
    "cucu",
    "smoke",
    "gpu",
    "global-config",
    "nexus-global-config",
    "nexus",
    "nexus-only",
    "nexus-workspace",
    "cloud-staging",
]

# test_types with a `--vcluster` path, per the `when:` conditions on the `*-vcluster-config`
# workflow-template entries in e2e-tests-build.yml. Also not expressed as data there.
VCLUSTER_ELIGIBLE_UI_TYPES = {"cucu", "global-config", "nexus", "nexus-global-config", "nexus-only"}

_TAG_RE = re.compile(r"@[\w()-]+")
_TESTRAIL_RE = re.compile(r"@testrail\((\d+)\)")


class RoutingError(RuntimeError):
    """Routing data could not be computed for a checkout."""


def _load_module(name: str, path: Path):
    """Import `path` as `name`, registered in sys.modules so its top-level code works."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _extract_ui_scenarios(features_root: Path):
    """Yield a dict for every `@testrail(N)`-tagged scenario (`Feature:` tags inherited)."""
    for feature_file in sorted(features_root.rglob("*.feature")):
        pending: set[str] = set()
        feature_tags: set[str] = set()
        for raw_line in feature_file.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("@"):
                pending |= set(_TAG_RE.findall(line))
                continue
            if line.startswith("Feature:"):
                feature_tags = pending
                pending = set()
                continue
            if line.startswith(("Scenario:", "Scenario Outline:")):
                scenario_tags = feature_tags | pending
                pending = set()
                name = line.split(":", 1)[1].strip()
                for case_id in _TESTRAIL_RE.findall(" ".join(scenario_tags)):
                    yield {
                        "case_id": int(case_id),
                        "tags": scenario_tags,
                        "file": feature_file,
                        "name": name,
                        # "cloud-staging" selects by path, everything else by tag from features/domino.
                        "root": "cloud_staging" if "cloud_staging" in feature_file.parts else "domino",
                    }
                continue
            # Background:, step lines, etc. orphan any pending tags.
            pending = set()


def _mark_attr(node):
    """Return the mark name if `node` is `pytest.mark.<name>`, else None."""
    if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)):
        return None
    inner = node.value
    if isinstance(inner.value, ast.Name) and inner.value.id == "pytest" and inner.attr == "mark":
        return node.attr
    return None


def _decorator_marks(decorator_list):
    marks = set()
    testrail_id = None
    for dec in decorator_list:
        node = dec.func if isinstance(dec, ast.Call) else dec
        name = _mark_attr(node)
        if name is None:
            continue
        marks.add(name)
        if name == "testrail" and isinstance(dec, ast.Call) and dec.args:
            arg = dec.args[0]
            if isinstance(arg, ast.Constant):
                testrail_id = str(arg.value)
    return marks, testrail_id


def _pytestmark_marks(value_node):
    marks = set()
    items = value_node.elts if isinstance(value_node, (ast.List, ast.Tuple)) else [value_node]
    for item in items:
        node = item.func if isinstance(item, ast.Call) else item
        name = _mark_attr(node)
        if name is not None:
            marks.add(name)
    return marks


def _module_pytestmark(module_body):
    marks = set()
    for node in module_body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "pytestmark" for target in node.targets
        ):
            marks |= _pytestmark_marks(node.value)
    return marks


def _module_list_assignments(module_body):
    """Map name -> literal for top-level `NAME = [...]`, so parametrize can reference one."""
    lists = {}
    for node in module_body:
        if isinstance(node, ast.Assign) and isinstance(node.value, (ast.List, ast.Tuple)):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    lists[target.id] = node.value
    return lists


def _parametrize_entries(decorator_list, module_lists):
    """Yield (marks, testrail_id, display_id) for each `pytest.param(...)` entry of a
    `@pytest.mark.parametrize` that carries its own `pytest.mark.testrail(N)`.

    The argvalues can be an inline literal or a name from `module_lists`; anything else
    (imported names, comprehensions) is not resolved."""
    for dec in decorator_list:
        if not isinstance(dec, ast.Call):
            continue
        if _mark_attr(dec.func) != "parametrize" or len(dec.args) < 2:
            continue
        argvalues = dec.args[1]
        if isinstance(argvalues, ast.Name):
            argvalues = module_lists.get(argvalues.id)
        if not isinstance(argvalues, (ast.List, ast.Tuple)):
            continue
        for index, entry in enumerate(argvalues.elts):
            if not (
                isinstance(entry, ast.Call) and isinstance(entry.func, ast.Attribute) and entry.func.attr == "param"
            ):
                continue
            marks, testrail_id, display_id = set(), None, str(index)
            for kw in entry.keywords:
                if kw.arg == "id" and isinstance(kw.value, ast.Constant):
                    display_id = str(kw.value.value)
                if kw.arg == "marks":
                    mark_nodes = kw.value.elts if isinstance(kw.value, (ast.List, ast.Tuple)) else [kw.value]
                    for mark_node in mark_nodes:
                        mark_call = mark_node.func if isinstance(mark_node, ast.Call) else mark_node
                        name = _mark_attr(mark_call)
                        if name is None:
                            continue
                        marks.add(name)
                        if name == "testrail" and isinstance(mark_node, ast.Call) and mark_node.args:
                            arg0 = mark_node.args[0]
                            if isinstance(arg0, ast.Constant):
                                testrail_id = str(arg0.value)
            if testrail_id:
                yield marks, testrail_id, display_id


def _walk_tests(body, inherited_marks, py_file, module_lists):
    for node in body:
        if isinstance(node, ast.ClassDef):
            class_marks, _ = _decorator_marks(node.decorator_list)
            class_marks |= _module_pytestmark(node.body)
            yield from _walk_tests(node.body, inherited_marks | class_marks, py_file, module_lists)
        elif isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            marks, testrail_id = _decorator_marks(node.decorator_list)
            all_marks = inherited_marks | marks
            found_parametrized = False
            for entry_marks, entry_testrail_id, display_id in _parametrize_entries(node.decorator_list, module_lists):
                found_parametrized = True
                yield {
                    "case_id": entry_testrail_id,
                    "marks": all_marks | entry_marks,
                    "file": py_file,
                    "name": f"{node.name}[{display_id}]",
                }
            if not found_parametrized and testrail_id:
                yield {
                    "case_id": testrail_id,
                    "marks": all_marks,
                    "file": py_file,
                    "name": node.name,
                }


def _extract_api_tests(tests_root: Path):
    for py_file in sorted(tests_root.rglob("test_*.py")):
        tree = ast.parse(py_file.read_text(), filename=str(py_file))
        module_marks = _module_pytestmark(tree.body)
        module_lists = _module_list_assignments(tree.body)
        yield from _walk_tests(tree.body, module_marks, py_file, module_lists)


def _tag_args_to_required_excluded(tag_args):
    required, excluded = set(), set()
    for arg in tag_args:
        if not arg.startswith("--tags="):
            continue
        value = arg.removeprefix("--tags=")
        if value.startswith("~"):
            excluded.add(value[1:])
        else:
            required.add(value)
    return required, excluded


def _ui_selectors(run_ui_tests) -> list[dict]:
    """Every UI (test_type, on_prem, vcluster) combo with the cucu tag filter it implies.
    Computed once per checkout rather than once per scenario -- the filters don't depend
    on the scenario."""
    selectors = []
    for test_type in UI_TEST_TYPES:
        base_args = run_ui_tests._build_test_args(test_type, "RESULTS")
        nexus_flag = test_type.startswith("nexus")
        vcluster_options = [False, True] if test_type in VCLUSTER_ELIGIBLE_UI_TYPES else [False]
        for on_prem in (False, True):
            for vcluster in vcluster_options:
                tag_args = [
                    *base_args,
                    "--tags=~cloud-only" if on_prem else "--tags=~no-cloud",
                    "--tags=~sanity",
                ]
                if vcluster:
                    tag_args.append("--tags=~skip-vcluster")
                if not nexus_flag:
                    tag_args.append("--tags=~nexus-only")
                required, excluded = _tag_args_to_required_excluded(tag_args)
                flags = [flag for flag, on in (("on_prem", on_prem), ("vcluster", vcluster)) if on]
                selectors.append({"test_type": test_type, "flags": flags, "required": required, "excluded": excluded})
    return selectors


def _ui_candidates(selectors, tags: set[str], root: str) -> list[dict]:
    """Every combo whose cucu tag filter matches `tags`. `root` gates "cloud-staging",
    which selects by feature path, not tag."""
    return [
        {"test_type": s["test_type"], "flags": s["flags"]}
        for s in selectors
        if (s["test_type"] == "cloud-staging") == (root == "cloud_staging")
        and s["required"] <= tags
        and not (s["excluded"] & tags)
    ]


def _eval_and_not_expr(expr: str, marks: set[str]) -> bool:
    """Evaluate the `a and not b and not c` expressions `_compute_system_test_marks`
    produces. Not a general boolean parser; extend it if that function adds `or`/parens."""
    for clause in expr.split(" and "):
        clause = clause.strip()
        if clause.startswith("not "):
            if clause[4:].strip() in marks:
                return False
        elif clause not in marks:
            return False
    return True


def _api_selectors(step_run_api_tests) -> list[dict]:
    selectors = []
    for test_type in sorted(step_run_api_tests.API_TEST_TYPES):
        for on_prem in (False, True):
            for is_core in (False, True):
                expr = step_run_api_tests._compute_system_test_marks(test_type, is_core, on_prem)
                flags = [flag for flag, on in (("on_prem", on_prem), ("is_core", is_core)) if on]
                selectors.append({"test_type": test_type, "flags": flags, "expr": expr})
    return selectors


def compute_entries(repo_root: Path) -> list[dict]:
    """Routing entries for every TestRail-tagged UI scenario and API test under `repo_root`
    (see the module docstring for the shape). Imports the checkout's routing modules, so
    only call this in a throwaway process (`index_routing`) or a test."""
    run_ui_tests_path = repo_root / RUN_UI_TESTS_SUBPATH
    step_run_api_tests_path = repo_root / STEP_RUN_API_TESTS_SUBPATH
    for path in (run_ui_tests_path, step_run_api_tests_path):
        if not path.is_file():
            raise RoutingError(f"{path.relative_to(repo_root)} is not present at this revision")

    run_ui_tests = _load_module("run_ui_tests", run_ui_tests_path)
    step_run_api_tests = _load_module("step_run_api_tests", step_run_api_tests_path)
    ui_selectors = _ui_selectors(run_ui_tests)
    api_selectors = _api_selectors(step_run_api_tests)

    entries = []
    features_root = repo_root / UI_FEATURES_SUBPATH
    if features_root.is_dir():
        for scenario in _extract_ui_scenarios(features_root):
            bare_tags = {tag.lstrip("@") for tag in scenario["tags"]}
            entries.append(
                {
                    "case_id": scenario["case_id"],
                    "domain": "ui",
                    "file": scenario["file"].relative_to(repo_root).as_posix(),
                    "name": scenario["name"],
                    "root": scenario["root"],
                    "labels": [["tag", tag] for tag in sorted(bare_tags)],
                    "candidates": _ui_candidates(ui_selectors, bare_tags, scenario["root"]),
                }
            )

    tests_root = repo_root / API_TESTS_SUBPATH
    if tests_root.is_dir():
        for test in _extract_api_tests(tests_root):
            entries.append(
                {
                    "case_id": int(test["case_id"]),
                    "domain": "api",
                    "file": test["file"].relative_to(repo_root).as_posix(),
                    "name": test["name"],
                    "root": None,
                    "labels": [["mark", mark] for mark in sorted(test["marks"])],
                    "candidates": [
                        {"test_type": s["test_type"], "flags": s["flags"]}
                        for s in api_selectors
                        if _eval_and_not_expr(s["expr"], test["marks"])
                    ],
                }
            )
    return entries


def index_routing(repo_root: Path) -> list[dict]:
    """`compute_entries(repo_root)` evaluated in an isolated subprocess (see the module
    docstring for why). Raises `RoutingError` on any failure."""
    try:
        result = subprocess.run(
            [sys.executable, "-I", str(Path(__file__).resolve()), str(repo_root)],
            capture_output=True,
            text=True,
            timeout=_WORKER_TIMEOUT_SECONDS,
            check=False,  # a non-zero exit is reported below, with the worker's own message
        )
    except subprocess.TimeoutExpired as exc:
        raise RoutingError(f"routing worker timed out after {_WORKER_TIMEOUT_SECONDS}s") from exc
    if result.returncode != 0:
        lines = result.stderr.strip().splitlines()
        raise RoutingError(lines[-1] if lines else f"routing worker exited {result.returncode}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RoutingError(f"routing worker produced unparseable output: {exc}") from exc


def _main(argv: list[str]) -> int:
    # The repo's routing modules log (and `basicConfig` onto stdout) on import and on
    # every call; stdout here carries the JSON result, so silence all of it.
    logging.disable(logging.CRITICAL)
    try:
        entries = compute_entries(Path(argv[1]))
    except RoutingError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 -- the routing modules are arbitrary repo code
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    json.dump(entries, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
