"""Recommend the smallest set of `/test-e2e` invocations that covers a batch of TestRail
case IDs, from the routing data the indexer stored in the per-revision sqlite (`db.py`'s
`entries` / `invocation_candidates` / `entry_labels`; see `routing.py` for how it is made).

The selection logic -- test_type preference tiers, "never `--vcluster`, never `is_core`",
greedy set cover over (test_type, flags) keys, and `--tags=` line splitting -- is ported
from `~/_s/map_test_trigger_coverage.py` (a standalone copy of `internal-e2e-tests-service`'s
`build/circleci/map_test_trigger_coverage.py`). Unlike that script this reads precomputed
data instead of importing the test repo, and a candidate's flags are generic: `on_prem`,
`vcluster` and `is_core` are just flag names, and only `NEVER_EMIT_FLAGS` / `FLAG_ARGS`
below know anything about them. Tag substitution (`--extra-cases-allowed` and friends) is
deliberately not ported yet -- see deferred.md.

Only UI entries drive recommendations by default (`domains`): this service exists to
notice changed UI literals, and for a case shared with an API test the UI-selecting
test_type (gpu, cloud-staging) already runs both halves.
"""

from __future__ import annotations

import sqlite3

DEFAULT_MAX_LENGTH = 300

# Flags a candidate may carry that must never appear in a recommendation: `--vcluster` by
# policy, and `is_core` since it only runs inside `--run_core_workflow`, not as a
# single-type trigger. A candidate carrying any of them is not usable.
NEVER_EMIT_FLAGS = frozenset({"vcluster", "is_core"})

# Arguments implied by a flag: the runner needs these alongside it. (key, value) pairs,
# emitted as `--key=value` after the flags and reported as `args` in the structured form.
FLAG_ARGS: dict[str, dict[str, str]] = {"on_prem": {"flavor": "qe-aws-eks"}}

_PREFERRED_TEST_TYPES = {"cucu", "system-standard"}

Key = tuple[str, tuple[str, ...]]  # (test_type, sorted flag names)


def _test_type_tier(test_type: str) -> int:
    """Lower is more preferred: cucu/system-standard, then other types, then
    nexus/global-config types."""
    if test_type in _PREFERRED_TEST_TYPES:
        return 1
    if "nexus" in test_type or "global-config" in test_type:
        return 3
    return 2


def _requirement_keys(candidates: list[tuple[str, frozenset[str]]]) -> list[Key]:
    """(test_type, flags) keys that can trigger one entry, limited to its best preference
    tier so a cucu/system-standard case never falls back to nexus/global-config."""
    usable = [(test_type, flags) for test_type, flags in candidates if not flags & NEVER_EMIT_FLAGS]
    if not usable:
        return []
    best_tier = min(_test_type_tier(test_type) for test_type, _ in usable)
    return sorted({(test_type, tuple(sorted(flags))) for test_type, flags in usable if _test_type_tier(test_type) == best_tier})


def _minimize_invocations(requirements: list[dict]) -> list[tuple[Key, list[int]]]:
    """Greedy set cover over `requirements` (dicts with "case_id" and "keys"). Picks the key
    covering the most uncovered requirements, breaking ties toward already-used keys, then
    lower tier.

    Returns (key, case_ids) pairs, one per invocation."""
    pending = list(range(len(requirements)))
    invocations: list[tuple[Key, list[int]]] = []
    used_keys: set[Key] = set()
    while pending:
        candidate_keys = {key for i in pending for key in requirements[i]["keys"]}
        ranked = [
            (
                key not in used_keys,
                -sum(1 for i in pending if key in requirements[i]["keys"]),
                _test_type_tier(key[0]),
                key,
            )
            for key in candidate_keys
        ]
        best_key = min(ranked)[3]
        covered = [i for i in pending if best_key in requirements[i]["keys"]]
        case_ids = list(dict.fromkeys(requirements[i]["case_id"] for i in covered))
        invocations.append((best_key, case_ids))
        used_keys.add(best_key)
        pending = [i for i in pending if i not in covered]
    return invocations


def _args_for(flags: tuple[str, ...]) -> dict[str, str]:
    args: dict[str, str] = {}
    for flag in flags:
        args.update(FLAG_ARGS.get(flag, {}))
    return args


def _command_prefix(test_type: str, flags: tuple[str, ...]) -> str:
    """Everything in a `/test-e2e` line before the comma-joined tags."""
    parts = [test_type, *(f"--{flag}" for flag in flags), *(f"--{k}={v}" for k, v in _args_for(flags).items())]
    return "/test-e2e " + " ".join(parts) + " --tags="


def _split_case_ids(prefix: str, case_ids: list[int], max_length: int) -> list[list[int]]:
    """Greedily chunk `case_ids` so each line is at most `max_length` chars (0 = no limit),
    splitting only between tags. A tag that alone exceeds the limit gets its own chunk."""
    if not max_length:
        return [case_ids]
    chunks: list[list[int]] = []
    current: list[int] = []
    current_len = len(prefix)
    for case_id in case_ids:
        tag_len = len(f"testrail({case_id})")
        added = tag_len + (1 if current else 0)  # +1 for the joining comma
        if current and current_len + added > max_length:
            chunks.append(current)
            current = []
            current_len = len(prefix)
            added = tag_len
        current.append(case_id)
        current_len += added
    if current:
        chunks.append(current)
    return chunks


def _render(invocations: list[tuple[Key, list[int]]], max_length: int) -> tuple[list[str], list[dict]]:
    commands: list[str] = []
    structured: list[dict] = []
    for (test_type, flags), case_ids in invocations:
        prefix = _command_prefix(test_type, flags)
        for chunk in _split_case_ids(prefix, case_ids, max_length):
            tags = [f"testrail({case_id})" for case_id in chunk]
            commands.append(prefix + ",".join(tags))
            structured.append({"test_type": test_type, "tags": tags, "flags": list(flags), "args": _args_for(flags)})
    return commands, structured


def _load_entries(
    conn: sqlite3.Connection, case_ids: list[int], domains: tuple[str, ...]
) -> dict[int, list[dict]]:
    """case_id -> entries (each with its `candidates` as (test_type, flags) pairs)."""
    if not case_ids:
        return {}
    case_marks = ",".join("?" * len(case_ids))
    domain_marks = ",".join("?" * len(domains))
    entries: dict[int, dict] = {}
    for entry_id, case_id, domain, path, name in conn.execute(
        f"""
        SELECT entries.id, entries.case_id, entries.domain, source_files.path, entries.name
        FROM entries JOIN source_files ON source_files.id = entries.source_file
        WHERE entries.case_id IN ({case_marks}) AND entries.domain IN ({domain_marks})
        ORDER BY entries.id
        """,
        [*case_ids, *domains],
    ):
        entries[entry_id] = {"case_id": case_id, "file": path, "name": name, "candidates": {}}
    if entries:
        entry_marks = ",".join("?" * len(entries))
        for candidate_id, entry_id, test_type in conn.execute(
            f"SELECT id, entry_id, test_type FROM invocation_candidates WHERE entry_id IN ({entry_marks})",
            list(entries),
        ):
            entries[entry_id]["candidates"][candidate_id] = (test_type, set())
        for entry_id, candidate_id, name in conn.execute(
            f"SELECT entry_id, candidate_id, name FROM entry_labels "
            f"WHERE kind = 'flag' AND entry_id IN ({entry_marks})",
            list(entries),
        ):
            entries[entry_id]["candidates"][candidate_id][1].add(name)

    by_case: dict[int, list[dict]] = {}
    for entry in entries.values():
        entry["candidates"] = [(test_type, frozenset(flags)) for test_type, flags in entry["candidates"].values()]
        by_case.setdefault(entry["case_id"], []).append(entry)
    return by_case


def recommend_invocations(
    db_file,
    case_ids,
    *,
    max_length: int = DEFAULT_MAX_LENGTH,
    domains: tuple[str, ...] = ("ui",),
) -> dict:
    """The `recommended_invocations` stanza of a `/changed-literals-impact` result for
    `case_ids`, from the per-revision db at `db_file`:

        {"commands": ["/test-e2e cucu --tags=testrail(1),testrail(2)", ...],
         "structured": [{"test_type": "cucu", "tags": ["testrail(1)", ...], "flags": [], "args": {}}, ...],
         "uncovered": ["<case id>: <why no invocation>", ...],
         "warnings": [...]}

    `flags` are the boolean flags that are on (bare names, e.g. `on_prem`); `args` are the
    `--key=value` arguments they imply (e.g. `{"flavor": "qe-aws-eks"}`). `uncovered` lists
    cases that have no usable invocation. If the db has no routing data (an index built
    where the routing files were absent or failed), `commands`/`structured` are empty and
    `warnings` says why.
    """
    wanted = sorted({int(case_id) for case_id in case_ids})
    stanza: dict = {"commands": [], "structured": [], "uncovered": [], "warnings": []}
    if not wanted:
        return stanza

    conn = sqlite3.connect(db_file)
    try:
        try:
            meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        except sqlite3.OperationalError:
            meta = {}
        if meta.get("routing_indexed") != "1":
            reason = meta.get("routing_error") or "routing data was not indexed for this revision"
            stanza["warnings"].append(f"no invocation recommendations: {reason}")
            return stanza
        entries_by_case = _load_entries(conn, wanted, domains)
    finally:
        conn.close()

    requirements = []
    for case_id in wanted:
        entries = entries_by_case.get(case_id)
        if not entries:
            stanza["uncovered"].append(f"{case_id}: no @testrail({case_id}) entry found in the indexed test repo")
            continue
        for entry in entries:
            keys = _requirement_keys(entry["candidates"])
            if not keys:
                stanza["uncovered"].append(
                    f"{case_id}: {entry['file']}::{entry['name']} only selectable via --vcluster or "
                    "--run_core_workflow, which are not recommended"
                )
                continue
            requirements.append({"case_id": case_id, "keys": keys})

    stanza["commands"], stanza["structured"] = _render(_minimize_invocations(requirements), max_length)
    return stanza
