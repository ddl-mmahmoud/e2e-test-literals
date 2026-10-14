"""Phase tracking for an impact job: which phase it is in, and how long each phase has taken.

A copy of `changed_literals/profiling.py` plus `PhaseTracker.adopt`, which takes over the
phases changed-literals reports for its part of the work -- the two packages stay independent
of each other, the same way `git_ops.py` / `repo_checkout.py` are.

A job's status body carries `PhaseTracker.snapshot()`:

    {"total_seconds": 4.2,
     "phases": {"populating_test_db": {"state": "done", "seconds": 1.1, "caches": {"test_db": "miss"}},
                "analyzing_literals_diff": {"state": "running", "seconds": 3.1, "caches": {...}},
                ...,
                "optimizing_invocation_tags": None}}

A phase that has not started (or never runs for this job) is `None`. A running phase reports
the time it has taken so far.

`caches` names every cache a phase consulted and what it did: "hit", "miss", "expired" (the
entry was there but too old), "invalidated" (the entry was there but unusable), "bypassed"
(the caller asked to skip it), "disabled" (not configured).
"""

from __future__ import annotations

import contextlib
import dataclasses
import threading
import time
from typing import Iterator, Sequence


RESOLVING_TEST_REVISION = "resolving_test_revision"  # fetch the test repo's ref, resolve it to a sha
POPULATING_TEST_DB = "populating_test_db"  # build the sha's literal db, unless it is already built
LOADING_TEST_LITERALS = "loading_test_literals"
# The next four are changed-literals' own phases, adopted from its job status.
RESOLVING_PULL_REQUEST = "resolving_pull_request"  # only for a job named by `pr_number`
CHECKING_FINDINGS_CACHE = "checking_findings_cache"  # reuse an earlier result for the same diff, if any
FETCHING_LITERALS_REPO = "fetching_literals_repo"  # only after a findings cache miss
ANALYZING_LITERALS_DIFF = "analyzing_literals_diff"  # only after a findings cache miss
MATCHING_LITERALS = "matching_literals"  # collect its findings, match them against the test literals
COMPUTING_INVOCATIONS = "computing_invocations"
OPTIMIZING_INVOCATION_TAGS = "optimizing_invocation_tags"  # only when tag substitution is asked for
PHASES = (
    RESOLVING_TEST_REVISION,
    POPULATING_TEST_DB,
    LOADING_TEST_LITERALS,
    RESOLVING_PULL_REQUEST,
    CHECKING_FINDINGS_CACHE,
    FETCHING_LITERALS_REPO,
    ANALYZING_LITERALS_DIFF,
    MATCHING_LITERALS,
    COMPUTING_INVOCATIONS,
    OPTIMIZING_INVOCATION_TAGS,
)


@dataclasses.dataclass
class PhaseEntry:
    started: float  # time.monotonic()
    duration: float | None = None  # set once the phase has ended
    state: str = "running"  # "running" | "done" | "failed"
    caches: dict[str, str] = dataclasses.field(default_factory=dict)
    extra: dict = dataclasses.field(default_factory=dict)  # any other facts worth reporting

    def render(self, now: float) -> dict:
        seconds = self.duration if self.duration is not None else now - self.started
        body: dict = {"state": self.state, "seconds": round(seconds, 3)}
        if self.caches:
            body["caches"] = dict(self.caches)
        return {**body, **self.extra}


class PhaseTracker:
    def __init__(self, phases: Sequence[str]):
        self._lock = threading.Lock()
        self._phases = tuple(phases)
        self._entries: dict[str, PhaseEntry] = {}
        self._started = time.monotonic()
        self._finished: float | None = None
        self.current: str | None = None

    @contextlib.contextmanager
    def phase(self, name: str) -> Iterator[PhaseEntry]:
        """Time the `with` body as phase `name`. The body can record the caches it consulted
        on the yielded entry (`entry.caches["test_db"] = "hit"`), and any other facts in
        `entry.extra`. An exception escaping the body marks the phase "failed"."""
        with self._lock:
            entry = PhaseEntry(started=time.monotonic())
            self._entries[name] = entry
            self.current = name
        try:
            yield entry
        except BaseException:
            self._end(entry, "failed")
            raise
        self._end(entry, "done")

    def adopt(self, current: str | None, phases: dict) -> None:
        """Take over phases another service reports (its status body's "phase" and
        `profiling["phases"]`). A running phase keeps ticking from the elapsed time it
        reported, so a poll's staleness doesn't show up in `seconds`."""
        with self._lock:
            now = time.monotonic()
            for name, reported in phases.items():
                if reported is None or name not in self._phases:
                    continue
                seconds = reported["seconds"]
                running = reported["state"] == "running"
                self._entries[name] = PhaseEntry(
                    started=now - seconds,
                    duration=None if running else seconds,
                    state=reported["state"],
                    caches=dict(reported.get("caches", {})),
                    extra={k: v for k, v in reported.items() if k not in ("state", "seconds", "caches")},
                )
            if current in self._phases:
                self.current = current

    def _end(self, entry: PhaseEntry, state: str) -> None:
        with self._lock:
            entry.duration = time.monotonic() - entry.started
            entry.state = state

    def finish(self) -> None:
        """The job is over: freeze `total_seconds`."""
        with self._lock:
            self._finished = time.monotonic()

    def snapshot(self) -> dict:
        with self._lock:
            now = time.monotonic()
            end = self._finished if self._finished is not None else now
            return {
                "total_seconds": round(end - self._started, 3),
                "phases": {
                    name: entry.render(now) if (entry := self._entries.get(name)) else None for name in self._phases
                },
            }


def track(tracker: PhaseTracker | None, name: str) -> contextlib.AbstractContextManager[PhaseEntry]:
    """`tracker.phase(name)`, or a throwaway entry when nothing is tracking (a library or CLI
    caller)."""
    if tracker is None:
        return contextlib.nullcontext(PhaseEntry(started=0.0))
    return tracker.phase(name)
