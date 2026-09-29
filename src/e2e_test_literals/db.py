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
    kind TEXT NOT NULL,
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

CREATE INDEX idx_cases_feature_file ON cases(feature_file);
CREATE INDEX idx_tags_case_id ON tags(case_id);
CREATE INDEX idx_literals_case_id ON literals(case_id);
CREATE INDEX idx_literals_value ON literals(value);
CREATE INDEX idx_literals_source_file ON literals(source_file);
CREATE INDEX idx_patterns_case_id ON patterns(case_id);
"""


def _source_file_id(conn: sqlite3.Connection, ids_by_path: dict[str, int], path: str) -> int:
    """Return the id of `path` in `source_files`, inserting it on first use."""
    file_id = ids_by_path.get(path)
    if file_id is None:
        cursor = conn.execute("INSERT INTO source_files (path) VALUES (?)", (path,))
        file_id = cursor.lastrowid
        ids_by_path[path] = file_id
    return file_id


def write_sqlite(cases: dict[int, dict], out_path: Path) -> None:
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
                    literal["kind"],
                    int(literal["dynamic"]),
                    literal["source_kind"],
                    _source_file_id(conn, source_file_ids, literal["source_file"]),
                    literal["source_line"],
                    literal["step_text"],
                )
                for literal in case["literals"]
            ]
            conn.executemany(
                "INSERT INTO literals (case_id, value, kind, dynamic, source_kind, source_file, source_line, step_text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                literal_rows,
            )

            conn.executemany(
                "INSERT INTO patterns (case_id, pattern) VALUES (?, ?)",
                [(case_id, pattern) for pattern in case["patterns"]],
            )
        conn.commit()
    finally:
        conn.close()
