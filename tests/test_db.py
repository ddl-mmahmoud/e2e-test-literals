"""Unit tests for e2e_test_literals/db.py.

Covers the sqlite writer's table layout, foreign-key wiring to source_files,
and the multi-scenario-per-testrail-id edge case (see db.write_sqlite's
docstring).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest_check as check

from e2e_test_literals.db import write_sqlite


def test_write_sqlite_populates_all_four_tables(tmp_path: Path) -> None:
    cases = {
        101602: {
            "scenarios": [
                {"feature_file": "some.feature", "scenario_name": "some scenario", "tags": ["smoke", "workspace"]}
            ],
            "literals": [
                {
                    "value": "Save",
                    "kind": "argument",
                    "dynamic": False,
                    "source_kind": "direct",
                    "source_file": "some.feature",
                    "source_line": 5,
                    "step_text": 'I click the button "Save"',
                }
            ],
            "patterns": ['I click the button "{name}"'],
        }
    }
    out_path = tmp_path / "out.sqlite"

    write_sqlite(cases, out_path)

    conn = sqlite3.connect(out_path)
    case_row = conn.execute(
        "SELECT cases.id, source_files.path, cases.scenario_name "
        "FROM cases JOIN source_files ON source_files.id = cases.feature_file"
    ).fetchone()
    tag_rows = conn.execute("SELECT case_id, tag FROM tags ORDER BY tag").fetchall()
    literal_row = conn.execute(
        "SELECT literals.case_id, literals.value, literals.kind, literals.dynamic, literals.source_kind, "
        "source_files.path, literals.source_line, literals.step_text "
        "FROM literals JOIN source_files ON source_files.id = literals.source_file"
    ).fetchone()
    pattern_row = conn.execute("SELECT case_id, pattern FROM patterns").fetchone()
    source_file_paths = sorted(row[0] for row in conn.execute("SELECT path FROM source_files").fetchall())
    conn.close()

    check.equal(case_row, (101602, "some.feature", "some scenario"))
    check.equal(tag_rows, [(101602, "smoke"), (101602, "workspace")])
    check.equal(
        literal_row,
        (101602, "Save", "argument", 0, "direct", "some.feature", 5, 'I click the button "Save"'),
    )
    check.equal(pattern_row, (101602, 'I click the button "{name}"'))
    check.equal(
        source_file_paths,
        ["some.feature"],
        msg="the feature file is shared between cases and literals and should be deduped to one row",
    )


def test_write_sqlite_overwrites_a_preexisting_file(tmp_path: Path) -> None:
    out_path = tmp_path / "out.sqlite"
    first = {
        1: {
            "scenarios": [{"feature_file": "a.feature", "scenario_name": "a", "tags": []}],
            "literals": [],
            "patterns": [],
        }
    }
    second = {
        2: {
            "scenarios": [{"feature_file": "b.feature", "scenario_name": "b", "tags": []}],
            "literals": [],
            "patterns": [],
        }
    }

    write_sqlite(first, out_path)
    write_sqlite(second, out_path)

    conn = sqlite3.connect(out_path)
    ids = [row[0] for row in conn.execute("SELECT id FROM cases").fetchall()]
    source_file_paths = [row[0] for row in conn.execute("SELECT path FROM source_files").fetchall()]
    conn.close()
    check.equal(ids, [2], msg="the second write should start from a clean database, not append")
    check.equal(
        source_file_paths,
        ["b.feature"],
        msg="the second write should start from a clean database, not append",
    )


def test_write_sqlite_merges_tags_across_duplicate_scenarios_for_one_case(tmp_path: Path) -> None:
    cases = {
        1: {
            "scenarios": [
                {"feature_file": "a.feature", "scenario_name": "a", "tags": ["smoke"]},
                {"feature_file": "b.feature", "scenario_name": "b", "tags": ["smoke", "workspace"]},
            ],
            "literals": [],
            "patterns": [],
        }
    }
    out_path = tmp_path / "out.sqlite"

    write_sqlite(cases, out_path)

    conn = sqlite3.connect(out_path)
    case_row = conn.execute(
        "SELECT source_files.path, cases.scenario_name "
        "FROM cases JOIN source_files ON source_files.id = cases.feature_file WHERE cases.id = 1"
    ).fetchone()
    tags = sorted(row[0] for row in conn.execute("SELECT tag FROM tags WHERE case_id = 1").fetchall())
    conn.close()

    check.equal(case_row, ("a.feature", "a"), msg="cases row uses the first scenario entry")
    check.equal(tags, ["smoke", "workspace"], msg="tags are unioned, not dropped, across duplicate scenarios")


def _routing_entry(case_id: int, **overrides) -> dict:
    entry = {
        "case_id": case_id,
        "domain": "ui",
        "file": "tests/ui/features/some.feature",
        "name": "some scenario",
        "root": "domino",
        "labels": [["tag", "smoke"]],
        "candidates": [
            {"test_type": "cucu", "flags": []},
            {"test_type": "nexus", "flags": ["on_prem", "vcluster"]},
        ],
    }
    entry.update(overrides)
    return entry


def test_write_sqlite_stores_routing_entries_with_flags_attached_to_their_candidate(tmp_path: Path) -> None:
    out_path = tmp_path / "out.sqlite"

    write_sqlite({}, out_path, routing=[_routing_entry(1)])

    conn = sqlite3.connect(out_path)
    entry_rows = conn.execute(
        "SELECT entries.case_id, entries.domain, source_files.path, entries.name, entries.root "
        "FROM entries JOIN source_files ON source_files.id = entries.source_file"
    ).fetchall()
    candidate_rows = conn.execute("SELECT test_type FROM invocation_candidates ORDER BY id").fetchall()
    entry_level_labels = conn.execute(
        "SELECT kind, name, value, candidate_id FROM entry_labels WHERE kind != 'flag'"
    ).fetchall()
    flag_rows = conn.execute(
        "SELECT invocation_candidates.test_type, entry_labels.name, entry_labels.value "
        "FROM entry_labels JOIN invocation_candidates ON invocation_candidates.id = entry_labels.candidate_id "
        "WHERE entry_labels.kind = 'flag' ORDER BY entry_labels.id"
    ).fetchall()
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    conn.close()

    check.equal(entry_rows, [(1, "ui", "tests/ui/features/some.feature", "some scenario", "domino")])
    check.equal(candidate_rows, [("cucu",), ("nexus",)])
    check.equal(entry_level_labels, [("tag", "smoke", None, None)])
    check.equal(flag_rows, [("nexus", "on_prem", None), ("nexus", "vcluster", None)])
    check.equal(meta, {"routing_indexed": "1"})


def test_write_sqlite_keeps_every_entry_of_a_repeated_case_id(tmp_path: Path) -> None:
    out_path = tmp_path / "out.sqlite"
    entries = [_routing_entry(7), _routing_entry(7, domain="api", name="test_api", root=None, labels=[["mark", "gpu"]])]

    write_sqlite({}, out_path, routing=entries)

    conn = sqlite3.connect(out_path)
    rows = conn.execute("SELECT domain FROM entries WHERE case_id = 7 ORDER BY id").fetchall()
    conn.close()
    check.equal(rows, [("ui",), ("api",)])


def test_write_sqlite_records_when_routing_was_not_indexed(tmp_path: Path) -> None:
    out_path = tmp_path / "out.sqlite"

    write_sqlite({}, out_path, routing=None, routing_error="run_ui_tests.py is not present at this revision")

    conn = sqlite3.connect(out_path)
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    entry_count = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    conn.close()
    check.equal(
        meta,
        {"routing_indexed": "0", "routing_error": "run_ui_tests.py is not present at this revision"},
    )
    check.equal(entry_count, 0)
