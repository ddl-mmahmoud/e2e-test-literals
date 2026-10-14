"""Phase tracking for a job: which phase it is in, and how long each phase has taken.

`e2e_test_literals/service/profiling.py` is a copy of this with a few additions (it can
also adopt phases reported by this service) -- the two packages stay independent of each
other, the same way `git_ops.py` / `repo_checkout.py` are.

A job's status body carries `PhaseTracker.snapshot()`:

    {"total_seconds": 4.2,
     "phases": {"fetching_literals_repo": {"state": "done", "seconds": 1.1,
                                           "caches": {"repo_clone": "hit", "findings": "miss"}},
                "analyzing_literals_diff": {"state": "running", "seconds": 3.1, "caches": {...}},
                ...,
                "resolving_pull_request": None}}

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


RESOLVING_PULL_REQUEST = "resolving_pull_request"  # only for a job named by "pr"
FETCHING_LITERALS_REPO = "fetching_literals_repo"
ANALYZING_LITERALS_DIFF = "analyzing_literals_diff"
PHASES = (RESOLVING_PULL_REQUEST, FETCHING_LITERALS_REPO, ANALYZING_LITERALS_DIFF)


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
        # Applied to every phase that starts from now on, for a cache consulted before the
        # phases it affects begin (see app.py's findings cache).
        self.ambient_caches: dict[str, str] = {}

    @contextlib.contextmanager
    def phase(self, name: str) -> Iterator[PhaseEntry]:
        """Time the `with` body as phase `name`. The body can record the caches it consulted
        on the yielded entry (`entry.caches["test_db"] = "hit"`), and any other facts in
        `entry.extra`. An exception escaping the body marks the phase "failed"."""
        with self._lock:
            entry = PhaseEntry(started=time.monotonic(), caches=dict(self.ambient_caches))
            self._entries[name] = entry
            self.current = name
        try:
            yield entry
        except BaseException:
            self._end(entry, "failed")
            raise
        self._end(entry, "done")

    def skip(self, name: str, **caches: str) -> None:
        """Record `name` as satisfied without any work (a cache hit covered it)."""
        with self._lock:
            self._entries[name] = PhaseEntry(
                started=time.monotonic(), duration=0.0, state="done", caches={**self.ambient_caches, **caches}
            )
            self.current = name

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
