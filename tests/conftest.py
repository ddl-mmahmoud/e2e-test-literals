"""Shared fixtures.

`bootstrapped_registry` gives tests a minimal on-disk tree that satisfies
`bootstrap.bootstrap_step_registry`'s needs -- an importable `steps` package under
`tests/ui/features/steps/` -- without requiring a real `internal-e2e-tests-service`
checkout. It's enough to register cucu's own built-in steps (e.g. "I click the
button", "I should see the text") since this fixture's `steps` package is
deliberately empty; it contributes no custom steps of its own.

`bootstrap_step_registry` is idempotent per-process (see bootstrap.py's `_bootstrapped`
global), so this is session-scoped: it runs exactly once regardless of which test
triggers it first, and later calls (with this fixture or a different root) are no-ops.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from e2e_test_literals.bootstrap import bootstrap_step_registry


@pytest.fixture(scope="session")
def bootstrapped_registry(tmp_path_factory: pytest.TempPathFactory) -> Path:
    repo_root = tmp_path_factory.mktemp("fake-repo-root")
    steps_dir = repo_root / "tests" / "ui" / "features" / "steps"
    steps_dir.mkdir(parents=True)
    (steps_dir / "__init__.py").write_text("")
    bootstrap_step_registry(repo_root)
    return repo_root
