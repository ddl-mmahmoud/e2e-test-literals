"""Unit tests for service/profiling.py's PhaseTracker."""

from __future__ import annotations

import time

import pytest

from e2e_test_literals.service.profiling import PHASES, PhaseTracker, track


def _tracker() -> PhaseTracker:
    return PhaseTracker(PHASES)


def test_a_fresh_tracker_reports_every_phase_as_not_run():
    snapshot = _tracker().snapshot()

    assert list(snapshot["phases"]) == list(PHASES)
    assert set(snapshot["phases"].values()) == {None}
    assert snapshot["total_seconds"] >= 0


def test_a_running_phase_reports_the_time_so_far_and_becomes_current():
    tracker = _tracker()
    with tracker.phase("populating_test_db"):
        time.sleep(0.02)
        running = tracker.snapshot()["phases"]["populating_test_db"]
        assert tracker.current == "populating_test_db"

    assert running["state"] == "running"
    assert running["seconds"] >= 0.02
    done = tracker.snapshot()["phases"]["populating_test_db"]
    assert done["state"] == "done"
    assert done["seconds"] >= running["seconds"]


def test_a_finished_phase_stops_counting():
    tracker = _tracker()
    with tracker.phase("populating_test_db"):
        pass
    first = tracker.snapshot()["phases"]["populating_test_db"]["seconds"]
    time.sleep(0.02)

    assert tracker.snapshot()["phases"]["populating_test_db"]["seconds"] == first


def test_caches_and_extra_facts_are_reported():
    tracker = _tracker()
    with tracker.phase("populating_test_db") as phase:
        phase.caches["test_db"] = "hit"
        phase.extra["lock_wait_seconds"] = 0.5

    entry = tracker.snapshot()["phases"]["populating_test_db"]
    assert entry["caches"] == {"test_db": "hit"}
    assert entry["lock_wait_seconds"] == 0.5


def test_an_exception_marks_the_phase_failed_and_propagates():
    tracker = _tracker()
    with pytest.raises(RuntimeError):
        with tracker.phase("matching_literals"):
            raise RuntimeError("boom")

    assert tracker.snapshot()["phases"]["matching_literals"]["state"] == "failed"


def test_finish_freezes_total_seconds():
    tracker = _tracker()
    tracker.finish()
    total = tracker.snapshot()["total_seconds"]
    time.sleep(0.02)

    assert tracker.snapshot()["total_seconds"] == total


def test_adopt_takes_over_reported_phases_and_a_running_one_keeps_ticking():
    tracker = _tracker()
    tracker.adopt(
        "analyzing_literals_diff",
        {
            "resolving_pull_request": None,
            "fetching_literals_repo": {"state": "done", "seconds": 2.5, "caches": {"repo_clone": "miss"}},
            "analyzing_literals_diff": {"state": "running", "seconds": 1.0},
            "not_one_of_ours": {"state": "done", "seconds": 9.0},
        },
    )

    phases = tracker.snapshot()["phases"]
    assert phases["resolving_pull_request"] is None
    assert phases["fetching_literals_repo"] == {"state": "done", "seconds": 2.5, "caches": {"repo_clone": "miss"}}
    assert phases["analyzing_literals_diff"]["state"] == "running"
    assert phases["analyzing_literals_diff"]["seconds"] >= 1.0
    assert tracker.current == "analyzing_literals_diff"

    tracker.adopt("analyzing_literals_diff", {"analyzing_literals_diff": {"state": "done", "seconds": 3.0}})
    assert tracker.snapshot()["phases"]["analyzing_literals_diff"] == {"state": "done", "seconds": 3.0}


def test_adopt_ignores_a_phase_name_it_does_not_track():
    tracker = _tracker()
    tracker.adopt("queued", {})

    assert tracker.current is None


def test_track_without_a_tracker_is_a_harmless_no_op():
    with track(None, "populating_test_db") as phase:
        phase.caches["test_db"] = "hit"
