"""Unit tests for e2e_test_literals/lookup.py.

Covers input parsing, the quote-candidate expansion that lets a literal
recorded *with* quotes (e.g. `"EXECUTION_ID"`, see db.py's real corpus data)
still be found without silently guessing which form the user meant, the
per-file line-number aggregation, and the CLI's error handling for a missing
db / empty input.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import pytest_check as check
from click.testing import CliRunner

from e2e_test_literals import lookup

_SCHEMA = """
CREATE TABLE source_files (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE);
CREATE TABLE cases (id INTEGER PRIMARY KEY, feature_file INTEGER NOT NULL, scenario_name TEXT NOT NULL);
CREATE TABLE literals (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL,
    value TEXT NOT NULL,
    kind TEXT NOT NULL,
    dynamic INTEGER NOT NULL,
    source_kind TEXT NOT NULL,
    source_file INTEGER NOT NULL,
    source_line INTEGER NOT NULL,
    step_text TEXT NOT NULL
);
"""


def _build_db(tmp_path: Path) -> Path:
    """A minimal case-index db: case 101 clicks "Save" twice in the same file (lines 10
    and 15, exercising line-number aggregation) and also has a literal that genuinely
    contains quotes (`"EXECUTION_ID"`), matching real corpus data."""
    db_path = tmp_path / "case_index.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO source_files (id, path) VALUES (1, 'a.feature')")
    conn.execute("INSERT INTO cases (id, feature_file, scenario_name) VALUES (101, 1, 'Scenario A')")
    conn.executemany(
        "INSERT INTO literals (case_id, value, kind, dynamic, source_kind, source_file, source_line, step_text) "
        "VALUES (101, 'Save', 'argument', 0, 'direct', 1, ?, 'I click the button \"Save\"')",
        [(10,), (15,)],
    )
    conn.execute(
        "INSERT INTO literals (case_id, value, kind, dynamic, source_kind, source_file, source_line, step_text) "
        "VALUES (101, '\"EXECUTION_ID\"', 'argument', 0, 'direct', 1, 20, 'some step')"
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("Save\nAdd User\n", ["Save", "Add User"], id="dedupe-preserves-order"),
        pytest.param("Save\n\nSave\n  \nAdd User", ["Save", "Add User"], id="blank-lines-and-duplicates-dropped"),
        pytest.param('"EXECUTION_ID"\n', ['"EXECUTION_ID"'], id="quotes-preserved-as-typed"),
        pytest.param("  Save  \n", ["Save"], id="surrounding-whitespace-trimmed"),
    ],
)
def test_parse_literals(raw: str, expected: list[str]) -> None:
    assert lookup.parse_literals(raw) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("Save", ["Save"], id="unquoted-is-its-own-only-candidate"),
        pytest.param('"EXECUTION_ID"', ['"EXECUTION_ID"', "EXECUTION_ID"], id="quoted-adds-stripped-candidate"),
        pytest.param('"', ['"'], id="single-quote-character-not-treated-as-wrapped"),
    ],
)
def test_candidates(value: str, expected: list[str]) -> None:
    assert lookup._candidates(value) == expected


def test_query_matches_finds_plain_and_quote_containing_literals(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)

    rows = lookup.query_matches(db_path, ["Save", '"EXECUTION_ID"'])

    check.equal(sorted({row["literal"] for row in rows}), ['"EXECUTION_ID"', "Save"])
    check.equal(sorted(row["line"] for row in rows if row["literal"] == "Save"), [10, 15])
    check.equal([row["line"] for row in rows if row["literal"] == '"EXECUTION_ID"'], [20])
    check.equal(
        {row["patterns"][0] for row in rows if row["literal"] == '"EXECUTION_ID"'},
        {'"EXECUTION_ID"'},
        msg="the quoted literal in the db is only found via the as-typed quoted candidate",
    )


def test_aggregate_by_file_collapses_lines_within_same_case_and_file(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)
    rows = lookup.query_matches(db_path, ["Save"])

    aggregated = lookup.aggregate_by_file(rows)

    check.equal(len(aggregated), 1, msg="both Save occurrences are case 101 / a.feature -- one aggregated row")
    check.equal(aggregated[0]["lines"], "10 15")
    check.equal(aggregated[0]["case_id"], 101)
    check.equal("line" in aggregated[0], False, msg="the singular `line` column is replaced by `lines`")
    check.equal(aggregated[0]["step_text"], ['I click the button "Save"'])
    check.equal(aggregated[0]["patterns"], ["Save"])


def test_aggregate_by_file_dedupes_step_text_across_lines_but_keeps_distinct_wordings(tmp_path: Path) -> None:
    db_path = tmp_path / "case_index.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO source_files (id, path) VALUES (1, 'a.feature')")
    conn.execute("INSERT INTO cases (id, feature_file, scenario_name) VALUES (101, 1, 'Scenario A')")
    conn.executemany(
        "INSERT INTO literals (case_id, value, kind, dynamic, source_kind, source_file, source_line, step_text) "
        "VALUES (101, 'Save', 'argument', 0, 'direct', 1, ?, ?)",
        [(10, 'I click the button "Save"'), (15, 'I click the button "Save"'), (20, 'I wait to click "Save"')],
    )
    conn.commit()
    conn.close()

    rows = lookup.query_matches(db_path, ["Save"])
    aggregated = lookup.aggregate_by_file(rows)

    check.equal(len(aggregated), 1)
    check.equal(aggregated[0]["lines"], "10 15 20")
    check.equal(aggregated[0]["step_text"], ['I click the button "Save"', 'I wait to click "Save"'])


def test_aggregate_by_file_split_steps_keeps_distinct_wordings_as_separate_records(tmp_path: Path) -> None:
    db_path = tmp_path / "case_index.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO source_files (id, path) VALUES (1, 'a.feature')")
    conn.execute("INSERT INTO cases (id, feature_file, scenario_name) VALUES (101, 1, 'Scenario A')")
    conn.executemany(
        "INSERT INTO literals (case_id, value, kind, dynamic, source_kind, source_file, source_line, step_text) "
        "VALUES (101, 'Save', 'argument', 0, 'direct', 1, ?, ?)",
        [(10, 'I click the button "Save"'), (15, 'I click the button "Save"'), (20, 'I wait to click "Save"')],
    )
    conn.commit()
    conn.close()

    rows = lookup.query_matches(db_path, ["Save"])
    aggregated = sorted(lookup.aggregate_by_file(rows, split_steps=True), key=lambda row: row["lines"])

    check.equal(len(aggregated), 2, msg="the two distinct step wordings become separate records")
    check.equal([row["lines"] for row in aggregated], ["10 15", "20"])
    check.equal([row["step_text"] for row in aggregated], ['I click the button "Save"', 'I wait to click "Save"'])
    check.equal(
        all(isinstance(row["step_text"], str) for row in aggregated),
        True,
        msg="step_text is a plain string when split_steps is set, not a list",
    )


def test_aggregate_by_file_keeps_different_files_separate() -> None:
    rows = [
        {
            "literal": "Save",
            "case_id": 1,
            "scenario": "s",
            "kind": "argument",
            "source_kind": "direct",
            "dynamic": 0,
            "file": "a.feature",
            "line": 5,
            "step_text": 'I click the button "Save"',
            "patterns": ["Save"],
        },
        {
            "literal": "Save",
            "case_id": 1,
            "scenario": "s",
            "kind": "argument",
            "source_kind": "direct",
            "dynamic": 1,
            "file": "b.feature",
            "line": 7,
            "step_text": 'I click the button "Save"',
            "patterns": ["Please click Save now"],
        },
    ]

    aggregated = sorted(lookup.aggregate_by_file(rows), key=lambda row: row["file"])

    check.equal([row["file"] for row in aggregated], ["a.feature", "b.feature"])
    check.equal([row["lines"] for row in aggregated], ["5", "7"])
    check.equal([row["dynamic"] for row in aggregated], [0, 1], msg="dynamic is OR'd only within a group")
    check.equal([row["patterns"] for row in aggregated], [["Save"], ["Please click Save now"]])


def test_aggregate_by_file_collects_every_pattern_that_matched_the_same_record(tmp_path: Path) -> None:
    """Two different provided values ("Save" and a larger string containing it) both
    match the same recorded "Save" literal in case 101 / a.feature -- the aggregated
    record's `patterns` should list both, not just whichever query ran last."""
    db_path = _build_db(tmp_path)

    rows = lookup.query_matches(db_path, ["Save", "Please click Save now"])
    aggregated = lookup.aggregate_by_file(rows)

    check.equal(len(aggregated), 1)
    check.equal(aggregated[0]["patterns"], ["Please click Save now", "Save"])


def test_query_matches_default_finds_recorded_literal_as_substring_of_a_larger_input(tmp_path: Path) -> None:
    """The provided value is the changing product string; by default it matches when a
    *smaller* recorded literal is contained within it."""
    db_path = _build_db(tmp_path)

    rows = lookup.query_matches(db_path, ["Please click Save now"])

    check.equal([row["literal"] for row in rows], ["Save", "Save"])
    check.equal(sorted(row["line"] for row in rows), [10, 15])


def test_query_matches_default_does_not_match_when_input_is_smaller_than_recorded_literal(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)

    rows = lookup.query_matches(db_path, ["Sav"])

    assert rows == []


def test_query_matches_reverse_matches_when_input_is_substring_of_recorded_literal(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)

    rows = lookup.query_matches(db_path, ["Sav"], reverse=True)

    check.equal([row["literal"] for row in rows], ["Save", "Save"])
    check.equal(sorted(row["line"] for row in rows), [10, 15])


def test_query_matches_reverse_does_not_match_when_recorded_literal_is_smaller(tmp_path: Path) -> None:
    """With `reverse=True`, a larger input is no longer found via a smaller recorded
    literal -- that's the default direction's job, not reverse's."""
    db_path = _build_db(tmp_path)

    rows = lookup.query_matches(db_path, ["Please click Save now"], reverse=True)

    assert rows == []


def test_query_matches_exact_matches_only_equal_values(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)

    rows = lookup.query_matches(db_path, ["Save"], exact=True)

    check.equal([row["literal"] for row in rows], ["Save", "Save"])
    check.equal(sorted(row["line"] for row in rows), [10, 15])


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("Please click Save now", id="larger-input-not-matched"),
        pytest.param("Sav", id="smaller-input-not-matched"),
    ],
)
def test_query_matches_exact_does_not_substring_match(tmp_path: Path, value: str) -> None:
    db_path = _build_db(tmp_path)

    rows = lookup.query_matches(db_path, [value], exact=True)

    assert rows == []


def test_query_matches_does_not_invent_a_quoted_candidate_for_bare_input(tmp_path: Path) -> None:
    """Candidate expansion only goes one direction (quoted input -> also try the
    stripped form, see `_candidates`). Pasting the bare `EXECUTION_ID` must not
    magically find the recorded `"EXECUTION_ID"` literal -- that would require
    guessing the user meant the quoted form, which is exactly what `_candidates`
    avoids doing."""
    db_path = _build_db(tmp_path)

    rows = lookup.query_matches(db_path, ["EXECUTION_ID"])

    assert rows == []


def test_cli_json_output_reports_matches_and_case_metadata(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)
    literals_file = tmp_path / "literals.txt"
    literals_file.write_text("Save\nNot A Real Literal\n")

    result = CliRunner().invoke(lookup.cli, ["--db", str(db_path), "--format", "json", str(literals_file)])

    check.equal(result.exit_code, 0, msg=result.output)
    payload = json.loads(result.output)
    check.equal(len(payload), 1, msg="the two Save occurrences aggregate to one entry")
    check.equal(payload[0]["case_id"], 101)
    check.equal(payload[0]["file"], "a.feature")
    check.equal(payload[0]["lines"], "10 15")
    check.equal(payload[0]["step_text"], ['I click the button "Save"'])
    check.equal(payload[0]["patterns"], ["Save"])


def test_cli_ndjson_output_emits_one_json_record_per_line(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)
    literals_file = tmp_path / "literals.txt"
    literals_file.write_text("Save\nNot A Real Literal\n")

    result = CliRunner().invoke(lookup.cli, ["--db", str(db_path), "--format", "ndjson", str(literals_file)])

    check.equal(result.exit_code, 0, msg=result.output)
    lines = result.output.splitlines()
    check.equal(len(lines), 1, msg="the two Save occurrences aggregate to one ndjson record")
    record = json.loads(lines[0])
    check.equal(record["case_id"], 101)
    check.equal(record["file"], "a.feature")
    check.equal(record["lines"], "10 15")
    check.equal(record["step_text"], ['I click the button "Save"'])
    check.equal(record["patterns"], ["Save"])


def _build_cross_case_step_db(tmp_path: Path) -> Path:
    """Two cases (101, 102) both use step wording A on "Save" in a.feature (at
    different lines); case 101 also uses a distinct step wording B on "Save" in the
    same file. Exercises grouping across case/literal boundaries by (file, step_text)."""
    db_path = tmp_path / "case_index.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO source_files (id, path) VALUES (1, 'a.feature')")
    conn.execute("INSERT INTO cases (id, feature_file, scenario_name) VALUES (101, 1, 'Scenario A')")
    conn.execute("INSERT INTO cases (id, feature_file, scenario_name) VALUES (102, 1, 'Scenario B')")
    conn.executemany(
        "INSERT INTO literals (case_id, value, kind, dynamic, source_kind, source_file, source_line, step_text) "
        "VALUES (?, 'Save', 'argument', 0, 'direct', 1, ?, ?)",
        [
            (101, 10, 'I click the button "Save"'),
            (101, 15, 'I click the button "Save"'),
            (102, 30, 'I click the button "Save"'),
            (101, 20, 'I wait to click "Save"'),
        ],
    )
    conn.commit()
    conn.close()
    return db_path


def test_group_records_by_step_groups_across_cases_and_literals_sharing_file_and_step_text(tmp_path: Path) -> None:
    db_path = _build_cross_case_step_db(tmp_path)
    rows = lookup.query_matches(db_path, ["Save"])

    groups = lookup.group_records_by_step(rows)

    check.equal(len(groups), 2, msg="two distinct step wordings in a.feature -- two groups")
    by_step_text = {group[0]["step_text"]: group for group in groups}
    click_group = by_step_text['I click the button "Save"']
    wait_group = by_step_text['I wait to click "Save"']
    check.equal(len(click_group), 2, msg="cases 101 and 102 both used the click wording -- grouped together")
    check.equal(sorted(record["case_id"] for record in click_group), [101, 102])
    check.equal(sorted(record["lines"] for record in click_group), ["10 15", "30"])
    check.equal(len(wait_group), 1)
    check.equal(wait_group[0]["case_id"], 101)
    check.equal(wait_group[0]["lines"], "20")


def test_cli_group_by_step_flag_emits_one_ndjson_line_per_file_step_text_group(tmp_path: Path) -> None:
    db_path = _build_cross_case_step_db(tmp_path)
    literals_file = tmp_path / "literals.txt"
    literals_file.write_text("Save\n")

    result = CliRunner().invoke(
        lookup.cli, ["--db", str(db_path), "--format", "ndjson", "--group-by-step", str(literals_file)]
    )

    check.equal(result.exit_code, 0, msg=result.output)
    groups = [json.loads(line) for line in result.output.splitlines()]
    check.equal(len(groups), 2, msg="one ndjson line per (file, step_text) group")
    group_sizes = sorted(len(group) for group in groups)
    check.equal(group_sizes, [1, 2])


def test_cli_group_by_step_flag_rejected_with_tsv_and_csv(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)
    literals_file = tmp_path / "literals.txt"
    literals_file.write_text("Save\n")

    tsv_result = CliRunner().invoke(
        lookup.cli, ["--db", str(db_path), "--format", "tsv", "--group-by-step", str(literals_file)]
    )
    csv_result = CliRunner().invoke(
        lookup.cli, ["--db", str(db_path), "--format", "csv", "--group-by-step", str(literals_file)]
    )

    check.not_equal(tsv_result.exit_code, 0)
    check.not_equal(csv_result.exit_code, 0)


def test_cli_tsv_output_omits_step_text_and_patterns(tmp_path: Path) -> None:
    """Neither `step_text` nor `patterns` is part of `FIELDS`, so tsv/csv output is unchanged."""
    db_path = _build_db(tmp_path)
    literals_file = tmp_path / "literals.txt"
    literals_file.write_text("Save\n")

    result = CliRunner().invoke(lookup.cli, ["--db", str(db_path), "--format", "tsv", str(literals_file)])

    check.equal(result.exit_code, 0, msg=result.output)
    check.is_in("lines", result.output.splitlines()[0])
    check.equal("step_text" in result.output.splitlines()[0], False)
    check.equal("patterns" in result.output.splitlines()[0], False)


def test_cli_text_output_lists_unmatched_literals(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)
    literals_file = tmp_path / "literals.txt"
    literals_file.write_text("Save\nNot A Real Literal\n")

    result = CliRunner().invoke(lookup.cli, ["--db", str(db_path), str(literals_file)])

    check.equal(result.exit_code, 0, msg=result.output)
    check.is_in('"Save"', result.output)
    check.is_in("a.feature:10 15", result.output)
    check.is_in("Not A Real Literal", result.output)


def test_cli_text_output_matches_recorded_literal_within_a_larger_changed_string(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)
    literals_file = tmp_path / "literals.txt"
    literals_file.write_text("Please click Save now\n")

    result = CliRunner().invoke(lookup.cli, ["--db", str(db_path), str(literals_file)])

    check.equal(result.exit_code, 0, msg=result.output)
    check.is_in('"Save"', result.output)
    check.is_in("a.feature:10 15", result.output)


def test_cli_reverse_flag_matches_input_within_a_larger_recorded_literal(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)
    literals_file = tmp_path / "literals.txt"
    literals_file.write_text("Sav\n")

    without_reverse = CliRunner().invoke(lookup.cli, ["--db", str(db_path), str(literals_file)])
    with_reverse = CliRunner().invoke(lookup.cli, ["--db", str(db_path), "--reverse", str(literals_file)])

    check.equal(without_reverse.exit_code, 0, msg=without_reverse.output)
    check.equal(with_reverse.exit_code, 0, msg=with_reverse.output)
    check.is_in("No recorded literal matched", without_reverse.output)
    check.is_in('"Save"', with_reverse.output)
    check.is_in("a.feature:10 15", with_reverse.output)


def test_cli_exact_flag_matches_only_equal_values(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)
    literals_file = tmp_path / "literals.txt"
    literals_file.write_text("Please click Save now\n")

    result = CliRunner().invoke(lookup.cli, ["--db", str(db_path), "--exact", str(literals_file)])

    check.equal(result.exit_code, 0, msg=result.output)
    check.is_in("No recorded literal matched", result.output)


def test_cli_rejects_exact_and_reverse_together(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)
    literals_file = tmp_path / "literals.txt"
    literals_file.write_text("Save\n")

    result = CliRunner().invoke(lookup.cli, ["--db", str(db_path), "--exact", "--reverse", str(literals_file)])

    assert result.exit_code != 0


def test_cli_errors_clearly_when_db_is_missing(tmp_path: Path) -> None:
    literals_file = tmp_path / "literals.txt"
    literals_file.write_text("Save\n")
    missing_db = tmp_path / "missing.sqlite"

    result = CliRunner().invoke(lookup.cli, ["--db", str(missing_db), str(literals_file)])

    assert result.exit_code != 0


def test_cli_errors_clearly_on_empty_input(tmp_path: Path) -> None:
    db_path = _build_db(tmp_path)
    literals_file = tmp_path / "literals.txt"
    literals_file.write_text("\n\n")

    result = CliRunner().invoke(lookup.cli, ["--db", str(db_path), str(literals_file)])

    assert result.exit_code != 0
