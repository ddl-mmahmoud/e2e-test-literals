"""Fallback linguistic signal for a literal without a clear contextual match.

Port of linguisticScore() from frontend/extract-ui-shared.ts in the
frontend-web-ui-service repo -- kept in one place so every language adapter's
fallback scoring stays consistent.
"""

from __future__ import annotations

import re

_IDENTIFIER_RE = re.compile(r"^[a-z0-9_-]+$", re.IGNORECASE)
_STARTS_CAPITAL_RE = re.compile(r"^[A-Z]")
_ENDS_PUNCT_RE = re.compile(r"[.!?:]$")
_STOP_WORDS_RE = re.compile(
    r"\b(the|a|an|is|are|to|for|and|of|your|you|please|this|with)\b", re.IGNORECASE
)


def linguistic_score(text: str) -> float:
    trimmed = text.strip()
    if not trimmed:
        return 0.0

    # Looks like an identifier / slug / css-class rather than prose.
    if _IDENTIFIER_RE.match(trimmed):
        return 0.15

    has_space = " " in trimmed
    has_multiple_words = len(trimmed.split()) >= 2
    starts_capital = bool(_STARTS_CAPITAL_RE.match(trimmed))
    ends_punct = bool(_ENDS_PUNCT_RE.search(trimmed))
    has_stop_word = bool(_STOP_WORDS_RE.search(trimmed))

    score = 0.3
    if has_space:
        score += 0.2
    if has_multiple_words:
        score += 0.15
    if starts_capital:
        score += 0.1
    if ends_punct:
        score += 0.15
    if has_stop_word:
        score += 0.15
    return min(score, 0.9)
