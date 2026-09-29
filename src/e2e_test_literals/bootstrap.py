"""Populate behave's real step registry without starting a browser.

Mirrors what `cucu run` does at process startup -- just enough of it to get
every `@given/@when/@then/@step` matcher this suite uses (cucu's built-ins plus
this repo's custom steps) registered in `behave.step_registry.registry`, with
no deployment, no Selenium session, and nothing executed.
"""

from __future__ import annotations

import contextlib
import importlib.abc
import importlib.machinery
import inspect
import sys
import types
from pathlib import Path

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


class _FallbackStubFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Last-resort stand-in for whatever `import steps` can't resolve on disk --
    swagger/openapi-generated clients (`domino_client_v4`, `domino_public_client`,
    `steps.openapi`, and any other client added to the target repo the same way,
    present or future) chief among them. See tests/api-system-tests.md and
    tests/.gitignore in that repo. They carry no UI-literal content and this tool
    never calls their methods -- it only needs the `import` to succeed so the step
    registry populates. Rather than requiring a live deployment + `openapi-generator`
    (`tests/bin/setup.sh`) just to run a static-analysis tool, or enumerating every
    such package by name (brittle: a rename, or a new client added the same way,
    silently reintroduces the `ImportError` this exists to avoid), this is *appended*
    to `sys.meta_path`, never inserted. `find_spec` is only reached once every real
    finder -- including `PathFinder` over `sys.path`, which is what finds a client
    that *has* been generated on disk -- has already said it can't resolve `fullname`,
    so a real, resolvable module is never shadowed.

    Installed only for the duration of `import steps` in `bootstrap_step_registry`
    (see `_stub_unresolved_imports` below), never around `import cucu.steps` --
    that's an ordinary, fully-pip-installed dependency, and stubbing an unresolved
    name inside *its* import graph risks papering over one of its own transitive
    dependencies' legitimate `try: import optional_thing / except ImportError:`
    fallback (confirmed: this broke `urllib3`'s optional zstd support that way
    during testing) instead of a genuinely-missing generated client. Narrower than
    that, an unrelated genuine `ImportError` elsewhere in this tool still surfaces
    normally too.

    That same risk reappears *inside* `import steps` itself, one level deeper than
    the module above anticipated: `steps` transitively imports plenty of ordinary,
    fully-installed real packages (`boto3`, `bs4`, `cryptography`, ...) that do their
    own internal optional-dependency/platform probing the same way (`bs4.builder`
    probing for `html5lib`, `multiprocessing.connection` probing for `_winapi`,
    `botocore.httpsession` probing for `OpenSSL`) -- confirmed to break the same way
    those do, each time this fired. A genuinely-missing generated client is always
    imported directly by *this repo's own* code (`tests/...`), never from three or
    six frames deep inside an already-resolved, real dependency's own module -- so
    `find_spec` only stubs when the nearest non-importlib frame on the stack (the
    actual `import`/`from ... import` statement) lives under `repo_root`. Anywhere
    else, declining (returning `None`) lets the real `ModuleNotFoundError` surface
    so that dependency's own guard handles it the way it was written to.
    """

    def __init__(self, repo_root: Path) -> None:
        self._repo_root = repo_root.resolve()
        self.stubbed: set[str] = set()

    def _importing_file_is_under_repo(self) -> bool:
        for frame_info in inspect.stack(context=0):
            filename = frame_info.filename
            if "importlib" in filename or filename.startswith("<frozen"):
                continue
            try:
                return Path(filename).resolve().is_relative_to(self._repo_root)
            except (OSError, ValueError):
                return False
        return False

    def find_spec(self, fullname, path, target=None):
        # Platform-specific stdlib modules (`_winapi`, `winreg`, `msvcrt`, ...) are
        # unresolvable here for the same reason an ungenerated client is -- no real
        # finder can find them on this platform -- but they're never generated
        # clients, and CPython's own stdlib already guards every import of one behind
        # `try/except ImportError`. `sys.stdlib_module_names` lists every stdlib
        # module across all platforms, including ones absent on this one, so this
        # needs no manual, brittle list.
        top_level = fullname.split(".", 1)[0]
        if top_level in sys.stdlib_module_names:
            return None
        if not self._importing_file_is_under_repo():
            return None
        self.stubbed.add(fullname)
        return importlib.machinery.ModuleSpec(fullname, self, is_package=True)

    def create_module(self, spec):
        return types.ModuleType(spec.name)

    def exec_module(self, module):
        module.__path__ = []
        module.__getattr__ = lambda name: _StubAttr()


@contextlib.contextmanager
def _stub_unresolved_imports(repo_root: Path):
    """Make any import that every real finder fails to resolve, and whose own
    `import` statement lives under `repo_root`, succeed as an empty stub instead of
    raising -- see `_FallbackStubFinder`. Reports what it stubbed on the way out, so
    a genuinely-missing real dependency (as opposed to an ungenerated client)
    doesn't just silently disappear."""
    finder = _FallbackStubFinder(repo_root)
    sys.meta_path.append(finder)
    try:
        yield
    finally:
        sys.meta_path.remove(finder)
        if finder.stubbed:
            print(
                "e2e-test-literals: stubbed unresolved imports (assumed generated "
                f"API clients, never called): {', '.join(sorted(finder.stubbed))}",
                file=sys.stderr,
            )


def bootstrap_step_registry(repo_root: Path) -> None:
    """Register every matcher `tests/ui/features/steps/__init__.py` wires in.

    Adds `tests/ui/features` (so `import steps` resolves as the top-level
    `steps` package, matching cucu/behave's own base-dir convention -- see
    `Runner.setup_paths` in behave's `runner.py`) and `tests` (so the
    `helpers`/`common` packages the custom steps import resolve) to `sys.path`,
    initializes cucu's hook-variable machinery (`cucu.environment` reaches into
    it at import time), then imports `cucu.steps` followed by this repo's `steps`
    package -- the latter under `_stub_unresolved_imports` (see that function and
    `_FallbackStubFinder`), so an ungenerated API client it transitively imports
    doesn't need a live deployment or codegen just for this static-analysis tool.
    Idempotent -- a second call is a no-op.
    """
    global _bootstrapped
    if _bootstrapped:
        return

    for relative_dir in _STEPS_PATH_ENTRIES:
        path = str(repo_root / relative_dir)
        if path not in sys.path:
            sys.path.insert(0, path)

    from cucu import init_global_hook_variables

    init_global_hook_variables()

    import cucu.steps  # noqa: F401 -- registers cucu's built-in steps -- an ordinary pip
    # dependency, so left outside the stub window: any ImportError here is real, not an
    # ungenerated client, and stubbing it could paper over e.g. urllib3's own optional-
    # dependency try/except ImportError fallbacks (confirmed to break that way in testing).

    with _stub_unresolved_imports(repo_root):
        import steps  # noqa: F401 -- registers this repo's custom steps

    _bootstrapped = True
