"""SQLite schema and writer for the aggregated case index (see aggregate.build_case_index)."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

SCHEMA = """
CREATE TABLE source_files (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE
);

CREATE TABLE cases (
    id INTEGER PRIMARY KEY,
    feature_file INTEGER NOT NULL REFERENCES source_files(id),
    scenario_name TEXT NOT NULL
);

CREATE TABLE tags (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    tag TEXT NOT NULL
);

CREATE TABLE literals (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    value TEXT NOT NULL,
    situation TEXT NOT NULL,         -- 'argument' | 'table_column'
    string_kind TEXT NOT NULL,       -- what the string is (step_kinds.py); picks its match algorithm
    dynamic INTEGER NOT NULL,
    source_kind TEXT NOT NULL,
    source_file INTEGER NOT NULL REFERENCES source_files(id),
    source_line INTEGER NOT NULL,
    step_text TEXT NOT NULL
);

CREATE TABLE patterns (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    pattern TEXT NOT NULL
);

CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per @testrail(N)-tagged UI scenario or API test (see routing.py). Unlike `cases`
-- this is not one-per-TestRail-ID: a repeated ID keeps every scenario, and API tests are
-- included. `case_id` therefore has no FK to `cases`.
CREATE TABLE entries (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL,
    domain TEXT NOT NULL,            -- 'ui' | 'api'
    source_file INTEGER NOT NULL REFERENCES source_files(id),
    name TEXT NOT NULL,
    root TEXT                        -- UI only: 'domino' | 'cloud_staging'
);

-- One row per (test_type, flag set) combination whose selectors match the entry. Raw
-- routing output: no policy (e.g. dropping --vcluster) is applied.
CREATE TABLE invocation_candidates (
    id INTEGER PRIMARY KEY,
    entry_id INTEGER NOT NULL REFERENCES entries(id),
    test_type TEXT NOT NULL
);

-- kind 'tag' (UI scenario tag) / 'mark' (API pytest mark): `name` is the bare tag/mark,
-- `value` NULL, `candidate_id` NULL.
-- kind 'flag': a runner flag that is ON for that candidate, so `candidate_id` is set (a flag
-- is only meaningful together with the test_type it was matched under -- an entry can be
-- selected by `nexus` with on_prem and by `cucu` without). `name` is the bare flag name
-- ('on_prem', 'vcluster', 'is_core', ...); `value` is NULL for a boolean flag, or the
-- argument's value for a key=value argument. Absence of a row means the flag is off.
CREATE TABLE entry_labels (
    id INTEGER PRIMARY KEY,
    entry_id INTEGER NOT NULL REFERENCES entries(id),
    candidate_id INTEGER REFERENCES invocation_candidates(id),
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    value TEXT
);

CREATE INDEX idx_cases_feature_file ON cases(feature_file);
CREATE INDEX idx_tags_case_id ON tags(case_id);
CREATE INDEX idx_literals_case_id ON literals(case_id);
CREATE INDEX idx_literals_value ON literals(value);
CREATE INDEX idx_literals_source_file ON literals(source_file);
CREATE INDEX idx_patterns_case_id ON patterns(case_id);
CREATE INDEX idx_entries_case_id ON entries(case_id);
CREATE INDEX idx_invocation_candidates_entry_id ON invocation_candidates(entry_id);
CREATE INDEX idx_entry_labels_entry_id ON entry_labels(entry_id);
CREATE INDEX idx_entry_labels_candidate_id ON entry_labels(candidate_id);
"""


def _source_file_id(conn: sqlite3.Connection, ids_by_path: dict[str, int], path: str) -> int:
    """Return the id of `path` in `source_files`, inserting it on first use."""
    file_id = ids_by_path.get(path)
    if file_id is None:
        cursor = conn.execute("INSERT INTO source_files (path) VALUES (?)", (path,))
        file_id = cursor.lastrowid
        ids_by_path[path] = file_id
    return file_id


def _write_routing(conn: sqlite3.Connection, ids_by_path: dict[str, int], entries: list[dict]) -> None:
    for entry in entries:
        entry_id = conn.execute(
            "INSERT INTO entries (case_id, domain, source_file, name, root) VALUES (?, ?, ?, ?, ?)",
            (
                entry["case_id"],
                entry["domain"],
                _source_file_id(conn, ids_by_path, entry["file"]),
                entry["name"],
                entry["root"],
            ),
        ).lastrowid
        conn.executemany(
            "INSERT INTO entry_labels (entry_id, kind, name) VALUES (?, ?, ?)",
            [(entry_id, kind, name) for kind, name in entry["labels"]],
        )
        for candidate in entry["candidates"]:
            candidate_id = conn.execute(
                "INSERT INTO invocation_candidates (entry_id, test_type) VALUES (?, ?)",
                (entry_id, candidate["test_type"]),
            ).lastrowid
            conn.executemany(
                "INSERT INTO entry_labels (entry_id, candidate_id, kind, name, value) VALUES (?, ?, 'flag', ?, ?)",
                [(entry_id, candidate_id, flag, None) for flag in candidate["flags"]],
            )


def write_sqlite(
    cases: dict[int, dict],
    out_path: Path,
    *,
    routing: list[dict] | None = None,
    routing_error: str | None = None,
) -> None:
    """Write the aggregated case index to a fresh sqlite database at `out_path`.

    `case["scenarios"]` is a list for historical reasons (see aggregate.py) but
    every TestRail ID is expected to resolve to exactly one scenario in
    practice -- `cases.feature_file`/`cases.scenario_name` come from the first
    entry. If a TestRail ID is ever reused across scenarios (a data problem
    elsewhere in the corpus, not something this writer should paper over), a
    warning is printed and that ID's tags are still merged across all of its
    scenario entries so no tag is silently dropped.

    `source_files` dedupes every path referenced by `cases.feature_file` and
    `literals.source_file` (feature files for direct literals, python step
    modules for templated ones) into a single table, keyed by path.

    `routing` is `routing.index_routing`'s entries (None if routing was not, or could not
    be, computed -- `routing_error` then says why). Either way `meta.routing_indexed`
    records which, so a reader can tell "no data" from "nothing selects this case".
    """
    if out_path.exists():
        out_path.unlink()

    conn = sqlite3.connect(out_path)
    try:
        conn.executescript(SCHEMA)
        source_file_ids: dict[str, int] = {}
        for case_id, case in cases.items():
            scenarios = case["scenarios"]
            if len(scenarios) > 1:
                print(
                    f"warning: testrail id {case_id} maps to {len(scenarios)} scenarios; "
                    f"using {scenarios[0]['feature_file']!r} for cases.feature_file/scenario_name",
                    file=sys.stderr,
                )
            primary = scenarios[0]
            feature_file_id = _source_file_id(conn, source_file_ids, primary["feature_file"])
            conn.execute(
                "INSERT INTO cases (id, feature_file, scenario_name) VALUES (?, ?, ?)",
                (case_id, feature_file_id, primary["scenario_name"]),
            )

            tags = dict.fromkeys(tag for scenario in scenarios for tag in scenario["tags"])
            conn.executemany(
                "INSERT INTO tags (case_id, tag) VALUES (?, ?)",
                [(case_id, tag) for tag in tags],
            )

            literal_rows = [
                (
                    case_id,
                    literal["value"],
                    literal["situation"],
                    literal["string_kind"],
                    int(literal["dynamic"]),
                    literal["source_kind"],
                    _source_file_id(conn, source_file_ids, literal["source_file"]),
                    literal["source_line"],
                    literal["step_text"],
                )
                for literal in case["literals"]
            ]
            conn.executemany(
                "INSERT INTO literals "
                "(case_id, value, situation, string_kind, dynamic, source_kind, source_file, source_line, step_text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                literal_rows,
            )

            conn.executemany(
                "INSERT INTO patterns (case_id, pattern) VALUES (?, ?)",
                [(case_id, pattern) for pattern in case["patterns"]],
            )

        if routing is not None:
            _write_routing(conn, source_file_ids, routing)
        meta = [("routing_indexed", "1" if routing is not None else "0")]
        if routing_error is not None:
            meta.append(("routing_error", routing_error))
        conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", meta)
        conn.commit()
    finally:
        conn.close()
