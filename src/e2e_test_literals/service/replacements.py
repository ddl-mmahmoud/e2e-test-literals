"""Pair each removed finding with the added finding that most likely replaced it, so a
report can say "`Access` is now `Manage access`".

changed-literals tags every finding with the diff hunk (`hunk`) it came from; with
`--unified=0` the removed and added lines of a hunk are one edit, so a removed finding's
candidates are the added findings with the same `file` and `hunk`. Within a hunk, pairs are
assigned greedily by text similarity (rapidfuzz `ratio`, 0-100), best first, each added
finding replacing at most one removed finding -- a hunk that rewrote three labels pairs
each old label with its closest new one rather than all three with the same one.

Heuristic by design (see IMPACT-REPORT-PLAN.md): a hunk with equally many removed and added
findings is taken at its word and pairs them however similar they are; in a lopsided hunk a
pair must also reach `MIN_SIMILARITY` to count, since some of its strings were genuinely
removed or added rather than reworded.
"""

from __future__ import annotations

from rapidfuzz import fuzz

MIN_SIMILARITY = 30.0

_CANDIDATE_KEYS = ("file", "line", "column", "text", "context", "confidence")


def _candidate(added: dict, score: float, chosen: bool) -> dict:
    return {**{key: added[key] for key in _CANDIDATE_KEYS}, "score": round(score, 1), "chosen": chosen}


def attach_replacements(findings: list[dict]) -> list[dict]:
    """Returns `findings` with every removed finding given a `replacements` list: candidate
    added findings (`file/line/column/text/context/confidence` plus the similarity `score`
    and `chosen`), the assigned replacement first (the only one with `chosen` true, and
    none has it when no pairing was accepted), then the other same-hunk additions by score. Empty when
    the finding has no hunk, or nothing was added in it. Added findings are returned unchanged;
    inputs are not mutated."""
    removed_by_hunk: dict[tuple[str, int], list[int]] = {}
    added_by_hunk: dict[tuple[str, int], list[int]] = {}
    for index, finding in enumerate(findings):
        if finding.get("hunk") is None:
            continue
        bucket = removed_by_hunk if finding["change"] == "removed" else added_by_hunk
        bucket.setdefault((finding["file"], finding["hunk"]), []).append(index)

    replacements: dict[int, list[dict]] = {}
    for hunk_key, removed_indexes in removed_by_hunk.items():
        added_indexes = added_by_hunk.get(hunk_key, [])
        scored = [
            (fuzz.ratio(findings[r]["text"], findings[a]["text"]), r_pos, a_pos, r, a)
            for r_pos, r in enumerate(removed_indexes)
            for a_pos, a in enumerate(added_indexes)
        ]
        balanced = len(removed_indexes) == len(added_indexes)

        # Best score first; ties go to the pairing closest to source order.
        assigned: dict[int, int] = {}
        taken: set[int] = set()
        for score, _, _, r, a in sorted(scored, key=lambda t: (-t[0], abs(t[1] - t[2]), t[1])):
            if r in assigned or a in taken or not (balanced or score >= MIN_SIMILARITY):
                continue
            assigned[r] = a
            taken.add(a)

        for r in removed_indexes:
            ranked = sorted((item for item in scored if item[3] == r), key=lambda t: (-t[0], t[2]))
            ranked.sort(key=lambda t: t[4] != assigned.get(r))  # stable: the assignment leads
            replacements[r] = [_candidate(findings[a], score, assigned.get(r) == a) for score, _, _, _, a in ranked]

    return [
        {**finding, "replacements": replacements.get(index, [])} if finding["change"] == "removed" else finding
        for index, finding in enumerate(findings)
    ]


def chosen_replacement(finding: dict) -> dict | None:
    """The replacement a report should state for a removed finding, if one was accepted."""
    return next((c for c in finding.get("replacements") or [] if c["chosen"]), None)
