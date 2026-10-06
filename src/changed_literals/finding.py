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
