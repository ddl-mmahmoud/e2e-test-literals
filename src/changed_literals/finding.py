"""The output record: one candidate UI string touched by the diff."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Change = Literal["added", "removed"]


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    column: int
    text: str
    context: str
    confidence: float
    change: Change
    # Index of the diff hunk (within `file`'s diff) that changed this literal; a removed and
    # an added finding with the same file and hunk are the two halves of one edit. None when
    # the finding didn't come from a parsed diff.
    hunk: int | None = None
