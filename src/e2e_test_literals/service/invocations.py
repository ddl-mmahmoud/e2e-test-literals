"""Recommend the smallest set of `/test-e2e` invocations that covers a batch of TestRail
case IDs, from the routing data the indexer stored in the per-revision sqlite (`db.py`'s
`entries` / `invocation_candidates` / `entry_labels`; see `routing.py` for how it is made).

The selection logic -- test_type preference tiers, "never `--vcluster`, never `is_core`",
greedy set cover over (test_type, flags) keys, and `--tags=` line splitting -- is ported
from `~/_s/map_test_trigger_coverage.py` (a standalone copy of `internal-e2e-tests-service`'s
`build/circleci/map_test_trigger_coverage.py`). Unlike that script this reads precomputed
data instead of importing the test repo, and a candidate's flags are generic: `on_prem`,
`vcluster` and `is_core` are just flag names, and only `NEVER_EMIT_FLAGS` / `FLAG_ARGS`
below know anything about them. Tag substitution (`--extra-cases-allowed` /
`--missing-cases-allowed`) is optional and lives in `tag_planning.py`; this module builds
each invocation's tag universe from the same per-revision data and renders the result.

Only UI entries drive recommendations by default (`domains`): this service exists to
notice changed UI literals, and for a case shared with an API test the UI-selecting
test_type (gpu, cloud-staging) already runs both halves.
"""

from __future__ import annotations

import math
import re
import sqlite3

from .tag_planning import TagSelectionError, plan_tag_selection

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

_TESTRAIL_TAG_RE = re.compile(r"testrail\(\d+\)")


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


def _split_tags(prefix: str, tags: list[str], max_length: int) -> list[list[str]]:
    """Greedily chunk `tags` so each line is at most `max_length` chars (0 = no limit),
    splitting only between tags. A tag that alone exceeds the limit gets its own chunk."""
    if not max_length:
        return [tags]
    chunks: list[list[str]] = []
    current: list[str] = []
    current_len = len(prefix)
    for tag in tags:
        added = len(tag) + (1 if current else 0)  # +1 for the joining comma
        if current and current_len + added > max_length:
            chunks.append(current)
            current = []
            current_len = len(prefix)
            added = len(tag)
        current.append(tag)
        current_len += added
    if current:
        chunks.append(current)
    return chunks


def _render(
    invocations: list[tuple[Key, list[int]]],
    max_length: int,
    substitutions: dict[Key, tuple[str, ...]] | None = None,
    universes: dict[Key, dict[str, set[int]]] | None = None,
) -> tuple[list[str], list[dict]]:
    """Command strings and structured dicts, index-aligned. With `substitutions` (key ->
    shared tags chosen for that invocation; see `_substitute_tags`) the tags are those
    followed by an explicit `testrail(N)` for each case no chosen tag selects, and every
    structured dict gains `extra_cases`: the cases that line's shared tags also run beyond
    the ones asked for (empty for a line that kept plain tags)."""
    commands: list[str] = []
    structured: list[dict] = []
    for key, case_ids in invocations:
        test_type, flags = key
        shared = (substitutions or {}).get(key, ())
        selected: dict[str, set[int]] = {tag: (universes or {})[key][tag] for tag in shared}
        selected_ids = set().union(*selected.values())
        tags = [*shared, *(f"testrail({case_id})" for case_id in case_ids if case_id not in selected_ids)]
        prefix = _command_prefix(test_type, flags)
        for chunk in _split_tags(prefix, tags, max_length):
            commands.append(prefix + ",".join(chunk))
            item = {"test_type": test_type, "tags": chunk, "flags": list(flags), "args": _args_for(flags)}
            if substitutions is not None:
                chunk_ids = set().union(*(selected[tag] for tag in chunk if tag in selected))
                item["extra_cases"] = sorted(chunk_ids - set(case_ids))
            structured.append(item)
    return commands, structured


def _load_entries(
    conn: sqlite3.Connection, domains: tuple[str, ...], case_ids: list[int] | None = None
) -> dict[int, list[dict]]:
    """case_id -> entries, for every entry in `domains` (or only those of `case_ids`). Each
    entry has its `candidates` as (test_type, flags) pairs and its bare scenario `tags`."""
    where = f"entries.domain IN ({','.join('?' * len(domains))})"
    params: list = [*domains]
    if case_ids is not None:
        where += f" AND entries.case_id IN ({','.join('?' * len(case_ids))})"
        params += case_ids
    entries: dict[int, dict] = {}
    for entry_id, case_id, path, name in conn.execute(
        f"""
        SELECT entries.id, entries.case_id, source_files.path, entries.name
        FROM entries JOIN source_files ON source_files.id = entries.source_file
        WHERE {where}
        ORDER BY entries.id
        """,
        params,
    ):
        entries[entry_id] = {"case_id": case_id, "file": path, "name": name, "candidates": {}, "tags": set()}
    for candidate_id, entry_id, test_type in conn.execute(
        f"""
        SELECT invocation_candidates.id, invocation_candidates.entry_id, invocation_candidates.test_type
        FROM invocation_candidates JOIN entries ON entries.id = invocation_candidates.entry_id
        WHERE {where}
        """,
        params,
    ):
        entries[entry_id]["candidates"][candidate_id] = (test_type, set())
    for entry_id, candidate_id, kind, name in conn.execute(
        f"""
        SELECT entry_labels.entry_id, entry_labels.candidate_id, entry_labels.kind, entry_labels.name
        FROM entry_labels JOIN entries ON entries.id = entry_labels.entry_id
        WHERE entry_labels.kind IN ('flag', 'tag') AND {where}
        """,
        params,
    ):
        if kind == "flag":
            entries[entry_id]["candidates"][candidate_id][1].add(name)
        else:
            entries[entry_id]["tags"].add(name)

    by_case: dict[int, list[dict]] = {}
    for entry in entries.values():
        entry["candidates"] = [(test_type, frozenset(flags)) for test_type, flags in entry["candidates"].values()]
        by_case.setdefault(entry["case_id"], []).append(entry)
    return by_case


def _api_test_types(conn: sqlite3.Connection) -> set[str]:
    """test_types that also run API tests. Their `--tags=` is handled by a plugin that keeps
    only numeric IDs, so a non-numeric tag there would silently drop the API half."""
    return {
        test_type
        for (test_type,) in conn.execute(
            """
            SELECT DISTINCT invocation_candidates.test_type
            FROM invocation_candidates JOIN entries ON entries.id = invocation_candidates.entry_id
            WHERE entries.domain = 'api'
            """
        )
    }


def _case_tag_universe(ui_entries: list[dict], key: Key) -> dict[str, set[int]]:
    """tag -> case IDs of every UI scenario that bare tag selects under `key`. Each
    scenario's own `testrail(N)` tag is excluded, since that is what substitution replaces."""
    universe: dict[str, set[int]] = {}
    for entry in ui_entries:
        if key not in _requirement_keys(entry["candidates"]):
            continue
        for tag in entry["tags"]:
            if not _TESTRAIL_TAG_RE.fullmatch(tag):
                universe.setdefault(tag, set()).add(entry["case_id"])
    return universe


def _substitute_tags(
    invocations: list[tuple[Key, list[int]]],
    ui_entries: list[dict],
    api_test_types: set[str],
    extra_cases_allowed: int | None,
    missing_cases_allowed: int | None,
) -> tuple[dict[Key, tuple[str, ...]], dict[Key, dict[str, set[int]]], bool]:
    """Shared tags for the eligible invocations (key -> tags), their tag universes, and
    whether the search was cut short. Raises `TagSelectionError` if the allowances can't be
    met. An allowance left as None is unbounded."""
    keys = [key for key, _ in invocations if key[0] not in api_test_types]
    universes = {key: _case_tag_universe(ui_entries, key) for key in keys}
    case_ids_by_key = dict(invocations)
    plan, exhausted = plan_tag_selection(
        [(case_ids_by_key[key], universes[key]) for key in keys],
        math.inf if extra_cases_allowed is None else extra_cases_allowed,
        math.inf if missing_cases_allowed is None else missing_cases_allowed,
    )
    return dict(zip(keys, plan)), universes, exhausted


def recommend_invocations(
    db_file,
    case_ids,
    *,
    max_length: int = DEFAULT_MAX_LENGTH,
    domains: tuple[str, ...] = ("ui",),
    extra_cases_allowed: int | None = None,
    missing_cases_allowed: int | None = None,
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

    Tag substitution is off unless `extra_cases_allowed` and/or `missing_cases_allowed` is
    given (the other is then unbounded). On, shared scenario tags replace `testrail(N)`
    tags wherever the bounds, summed over all lines, allow it (see `tag_planning.py`), and
    every structured dict gains `extra_cases`, the case IDs its tags run beyond the batch.
    Test types that also run API tests keep plain tags. If no selection fits the bounds,
    the plain-tag lines are returned and `warnings` carries the achievable minima.
    """
    wanted = sorted({int(case_id) for case_id in case_ids})
    stanza: dict = {"commands": [], "structured": [], "uncovered": [], "warnings": []}
    if not wanted:
        return stanza
    substituting = extra_cases_allowed is not None or missing_cases_allowed is not None

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
        entries_by_case = _load_entries(conn, domains, wanted)
        # The tag universe spans every UI scenario, not just the batch: a shared tag is only
        # safe to use if we know everything else it would run.
        ui_entries = (
            [entry for entries in _load_entries(conn, ("ui",)).values() for entry in entries] if substituting else []
        )
        api_test_types = _api_test_types(conn) if substituting else set()
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

    minimized = _minimize_invocations(requirements)
    substitutions = universes = None
    if substituting:
        try:
            substitutions, universes, exhausted = _substitute_tags(
                minimized, ui_entries, api_test_types, extra_cases_allowed, missing_cases_allowed
            )
        except TagSelectionError as exc:
            stanza["warnings"].append(f"tag substitution not applied, using testrail(N) tags: {exc}")
        else:
            if exhausted:
                stanza["warnings"].append("tag search hit its node limit; the tag selection may not be minimal")
    stanza["commands"], stanza["structured"] = _render(minimized, max_length, substitutions, universes)
    return stanza
