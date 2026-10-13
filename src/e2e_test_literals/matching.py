"""Does a recorded test literal match a (changed) product string? One algorithm per `string_kind`.

Shared by the impact service (`service/impact.py`) and the `lookup` CLI so the two cannot
disagree. Which algorithm a literal gets is `step_kinds.policy_for(string_kind)`:

- `exact`: equality after whitespace normalization. A case-sensitive match gets the
  policy's fixed confidence (1.0 unless the kind says otherwise, e.g. `data`); one that
  differs only by case (CSS casing, or a test that matches case-insensitively) is still
  reported, but at `CASE_INSENSITIVE_CONFIDENCE` (capped by the policy's own), so these
  corner cases sit at the bottom of the default range rather than dominating it.
- `substring_and_partial_ratio`: case-insensitive containment is a hard gate, and
  rapidfuzz's `partial_ratio` only *grades* the confidence of matches that already passed
  it (a case difference or other minor drift still costs points). Pure `partial_ratio` alone
  is too permissive -- a literal that merely resembles some window of the text would match.
  Containment is a less rigorous match than equality, so the graded score is scaled by
  `SUBSTRING_CONFIDENCE_FACTOR`: a clean containment tops out at 0.8, near the high end of the
  default range, while a weak one (a loose hit inside a longer word, a case mismatch) falls
  below the default `min_literal_match_confidence`.
- `never`: the kind (`opaque`) cannot plausibly equal product text.

Literals shorter than `MIN_LITERAL_LENGTH_FOR_MATCHING` never match under any algorithm: a
1-2 character literal is a near-guaranteed spurious hit in unrelated text, and below that
length `partial_ratio` degenerates to a binary 0-or-100 result anyway.
"""

from __future__ import annotations

from rapidfuzz import fuzz

from .step_kinds import EXACT, NEVER, policy_for

# Low enough that an exact match differing only by case still clears it.
DEFAULT_MIN_LITERAL_MATCH_CONFIDENCE = 0.70
CASE_INSENSITIVE_CONFIDENCE = 0.70
SUBSTRING_CONFIDENCE_FACTOR = 0.8
MIN_LITERAL_LENGTH_FOR_MATCHING = 3


def normalize_whitespace(text: str) -> str:
    return " ".join(text.split())


def match_confidence(
    value: str,
    text: str,
    string_kind: str,
    *,
    reverse: bool = False,
    force_exact: bool = False,
) -> tuple[str, float] | None:
    """`(algorithm, confidence)` if test literal `value` matches `text` (the changed product
    string) given its `string_kind`, else None. The caller applies its own confidence floor.

    By default a literal matches when it occurs *in* `text` -- `text` is the changing product
    string, the literal is typically just the part a test checks. `reverse=True` flips the
    containment direction (`text` must occur in `value`). `partial_ratio` itself is
    direction-agnostic -- it always searches the shorter string inside the longer one -- so
    without the length guard below, a literal *longer* than `text` would silently flip the
    check into "is this short text contained in this long literal", a near-guaranteed spurious
    match for any common short word. `force_exact` treats every non-`never` kind as exact
    (the CLI's `--exact`)."""
    if len(value) < MIN_LITERAL_LENGTH_FOR_MATCHING:
        return None
    policy = policy_for(string_kind)
    if policy.algorithm == NEVER:
        return None
    if policy.algorithm == EXACT or force_exact:
        value, text = normalize_whitespace(value), normalize_whitespace(text)
        confidence = policy.confidence if policy.algorithm == EXACT else 1.0
        if value == text:
            return EXACT, confidence
        if value.casefold() == text.casefold():
            return EXACT, min(confidence, CASE_INSENSITIVE_CONFIDENCE)
        return None

    needle, haystack = (text, value) if reverse else (value, text)
    if len(needle) > len(haystack) or needle.casefold() not in haystack.casefold():
        return None
    return policy.algorithm, SUBSTRING_CONFIDENCE_FACTOR * fuzz.partial_ratio(value, text) / 100.0
