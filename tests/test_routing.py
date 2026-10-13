"""Tests for routing.py against a fake test-repo tree (see routing_helpers.py)."""

from __future__ import annotations

from pathlib import Path

import pytest
from routing_helpers import write_fake_routing_repo

from e2e_test_literals import routing


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return write_fake_routing_repo(tmp_path / "repo")


def _entry(entries: list[dict], case_id: int, domain: str) -> dict:
    (entry,) = [e for e in entries if e["case_id"] == case_id and e["domain"] == domain]
    return entry


def _candidates(entry: dict) -> set[tuple[str, tuple[str, ...]]]:
    return {(c["test_type"], tuple(c["flags"])) for c in entry["candidates"]}


def test_ui_entry_without_special_tags_is_selected_by_cucu_smoke_and_nexus(repo):
    entries = routing.compute_entries(repo)

    entry = _entry(entries, 1234, "ui")

    assert entry["file"] == "tests/ui/features/domino/login/login.feature"
    assert entry["name"] == "Login and logout"
    assert entry["root"] == "domino"
    assert entry["labels"] == [["tag", "smoke"], ["tag", "testrail(1234)"]]
    assert _candidates(entry) == {
        # eligible for --vcluster, so every on_prem/vcluster combination is a candidate
        ("cucu", ()),
        ("cucu", ("on_prem",)),
        ("cucu", ("vcluster",)),
        ("cucu", ("on_prem", "vcluster")),
        ("nexus", ()),
        ("nexus", ("on_prem",)),
        ("nexus", ("vcluster",)),
        ("nexus", ("on_prem", "vcluster")),
        # not vcluster-eligible, and the scenario is tagged @smoke
        ("smoke", ()),
        ("smoke", ("on_prem",)),
    }


def test_no_cloud_tag_restricts_candidates_to_on_prem_and_nexus_only_selects_nexus_types(repo):
    entry = _entry(routing.compute_entries(repo), 67895, "ui")

    assert _candidates(entry) == {
        ("nexus", ("on_prem",)),
        ("nexus", ("on_prem", "vcluster")),
        ("nexus-only", ("on_prem",)),
        ("nexus-only", ("on_prem", "vcluster")),
        ("nexus-workspace", ("on_prem",)),
    }


def test_cloud_staging_features_are_selected_only_by_the_cloud_staging_type(repo):
    entry = _entry(routing.compute_entries(repo), 555, "ui")

    assert entry["root"] == "cloud_staging"
    assert _candidates(entry) == {("cloud-staging", ()), ("cloud-staging", ("on_prem",))}


def test_api_entry_uses_marks_and_carries_is_core_as_a_flag(repo):
    entry = _entry(routing.compute_entries(repo), 7001, "api")

    assert entry["file"] == "tests/api/tests/test_gpu.py"
    assert entry["name"] == "test_gpu_jobs"
    assert entry["root"] is None
    assert entry["labels"] == [["mark", "gpu"], ["mark", "testrail"]]
    assert _candidates(entry) == {
        ("gpu", ()),
        ("gpu", ("on_prem",)),
        ("gpu", ("is_core",)),
        ("gpu", ("on_prem", "is_core")),
    }


def test_compute_entries_raises_when_the_routing_files_are_absent(tmp_path):
    (tmp_path / "tests" / "ui" / "features").mkdir(parents=True)

    with pytest.raises(routing.RoutingError, match="run_ui_tests.py is not present"):
        routing.compute_entries(tmp_path)


def test_index_routing_matches_compute_entries_via_the_subprocess(repo):
    assert routing.index_routing(repo) == routing.compute_entries(repo)


def test_index_routing_is_not_polluted_by_the_repos_import_time_logging(repo):
    # The fake run_ui_tests basicConfig's onto stdout and logs per call, like the real one;
    # the worker's stdout must still be exactly the JSON result.
    assert routing.index_routing(repo)


def test_index_routing_reports_a_broken_routing_module_as_routing_error(repo):
    (repo / "build" / "circleci" / "run_ui_tests.py").write_text("raise RuntimeError('refactored away')\n")

    with pytest.raises(routing.RoutingError, match="refactored away"):
        routing.index_routing(repo)


def test_index_routing_reports_missing_files_as_routing_error(tmp_path):
    with pytest.raises(routing.RoutingError, match="not present"):
        routing.index_routing(tmp_path)
