"""Populate behave's real step registry without starting a browser.

Mirrors what `cucu run` does at process startup -- just enough of it to get
every `@given/@when/@then/@step` matcher this suite uses (cucu's built-ins plus
this repo's custom steps) registered in `behave.step_registry.registry`, with
no deployment, no Selenium session, and nothing executed.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
import types
from pathlib import Path

# These are swagger/openapi-generated clients (see tests/api-system-tests.md and
# tests/.gitignore) that some custom step modules import transitively. They carry
# no UI-literal content and this tool never calls their methods -- it only needs
# `import steps` to succeed so the registry populates. Rather than requiring a
# live deployment + `openapi-generator` (`tests/bin/setup.sh`) just to run a
# static-analysis tool, stand in for whichever of these are not already generated.
_GENERATED_CLIENT_DIRS = {
    "domino_client_v4": "tests/domino_client_v4",
    "domino_public_client": "tests/domino_public_client",
    "steps.openapi": "tests/ui/features/steps/openapi",
}

_STEPS_PATH_ENTRIES = ("tests/ui/features", "tests")

_bootstrapped = False


class _StubAttr:
    """Generic placeholder returned for any attribute access on a stubbed module.

    Callable (so `SomeGeneratedClass(...)` inside an un-executed function body
    would not itself raise ImportError-adjacent surprises) and infinitely
    attribute-accessible.
    """

    def __call__(self, *args, **kwargs):
        return self

    def __getattr__(self, name):
        return self


class _GeneratedClientStubFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Synthesizes import-only stand-ins for the ungenerated packages in PREFIXES."""

    def __init__(self, prefixes: tuple[str, ...]) -> None:
        self._prefixes = prefixes

    def find_spec(self, fullname, path, target=None):
        if not any(fullname == prefix or fullname.startswith(f"{prefix}.") for prefix in self._prefixes):
            return None
        return importlib.machinery.ModuleSpec(fullname, self, is_package=True)

    def create_module(self, spec):
        return types.ModuleType(spec.name)

    def exec_module(self, module):
        module.__path__ = []
        module.__getattr__ = lambda name: _StubAttr()


def _install_generated_client_stubs(repo_root: Path) -> None:
    missing = tuple(
        prefix for prefix, relative_dir in _GENERATED_CLIENT_DIRS.items() if not (repo_root / relative_dir).is_dir()
    )
    if missing:
        sys.meta_path.insert(0, _GeneratedClientStubFinder(missing))


def bootstrap_step_registry(repo_root: Path) -> None:
    """Register every matcher `tests/ui/features/steps/__init__.py` wires in.

    Adds `tests/ui/features` (so `import steps` resolves as the top-level
    `steps` package, matching cucu/behave's own base-dir convention -- see
    `Runner.setup_paths` in behave's `runner.py`) and `tests` (so the
    `helpers`/`common` packages the custom steps import resolve) to `sys.path`,
    initializes cucu's hook-variable machinery (`cucu.environment` reaches into
    it at import time), then imports `cucu.steps` followed by this repo's
    `steps` package. Idempotent -- a second call is a no-op.
    """
    global _bootstrapped
    if _bootstrapped:
        return

    _install_generated_client_stubs(repo_root)

    for relative_dir in _STEPS_PATH_ENTRIES:
        path = str(repo_root / relative_dir)
        if path not in sys.path:
            sys.path.insert(0, path)

    from cucu import init_global_hook_variables

    init_global_hook_variables()

    import cucu.steps  # noqa: F401 -- registers cucu's built-in steps
    import steps  # noqa: F401 -- registers this repo's custom steps

    _bootstrapped = True
