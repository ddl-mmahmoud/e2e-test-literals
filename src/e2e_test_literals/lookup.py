"""Match a batch of changed product literals against the case-index sqlite db.

Reads a newline-delimited list of literal values (positional file argument,
or stdin), matches each one against `literals.value` in the sqlite database
produced by `python -m e2e_test_literals` (default:
./cucu_literal_deps.sqlite), and prints the TestRail case ids, files,
and line numbers that depend on each one.

The provided values are the literal strings that are *changing* in the
product; the recorded `literals.value` entries are what the tests actually
check, which is often just a smaller part of that changing string. How a
recorded literal is compared depends on what kind of string it is (its
`string_kind`, see step_kinds.py): a button/link/tab/... label must equal the
provided value, freeform text only has to occur in it, test-data names match
exactly at a low confidence, and opaque values (usernames, ids, ...) never
match. This is the same per-kind matching, 3-character floor and confidence
scoring the impact service uses (see matching.py); `--min-confidence` is its
`min_literal_match_confidence`. Pass `--reverse` to flip the containment
direction for the substring-matched kinds: match when the provided value is a
substring of the recorded literal. Pass `--exact` to treat every kind as
exact (`--exact` and `--reverse` are mutually exclusive, since exact matching
has no direction).

Default output is human-readable text; `--format tsv/csv/json/ndjson` is for
piping into other tools (`cut`, `jq`, a second script, ...).

By default, occurrences of the same literal within the same case/file are
collapsed into one record whose `step_text` is a list of the distinct step
wordings involved. Pass `--group-by-step` to instead group records by
(file, step_text) across every literal and case: each group lists the
distinct-step-wording records (case, literal, lines, patterns) that share a
file and a step wording -- the same as grouping the per-step-wording record
stream by the concatenation of its `file` and `step_text` fields (e.g. jq's
`group_by`). Not supported with `--format tsv/csv`, since `step_text` isn't
part of their columns.
"""

from __future__ import annotations

import csv
import json
import sqlite3
import sys
from pathlib import Path

import click

from .matching import DEFAULT_MIN_LITERAL_MATCH_CONFIDENCE, match_confidence

DEFAULT_DB = "cucu_literal_deps.sqlite"
FIELDS = [
    "literal",
    "case_id",
    "scenario",
    "situation",
    "string_kind",
    "source_kind",
    "dynamic",
    "match_confidence",
    "file",
    "lines",
]


def parse_literals(raw: str) -> list[str]:
    """Split newline-delimited input into a deduped, order-preserving list of literal
    values (blank lines dropped, quotes left exactly as typed -- see `_candidates`)."""
    values: dict[str, None] = {}
    for line in raw.splitlines():
        value = line.strip()
        if value:
            values.setdefault(value, None)
    return list(values)


def _candidates(value: str) -> list[str]:
    """A pasted line that's wrapped in a matching pair of quotes could be either a
    literal that genuinely contains quotes (some recorded values do, e.g. `"EXECUTION_ID"`)
    or clipboard noise from copying quoted step text. Query both forms rather than
    guessing -- silently stripping quotes would risk a false negative on the former."""
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return [value, value[1:-1]]
    return [value]


def query_matches(
    db_path: Path,
    literals: list[str],
    *,
    reverse: bool = False,
    exact: bool = False,
    min_confidence: float = DEFAULT_MIN_LITERAL_MATCH_CONFIDENCE,
) -> list[dict]:
    """Join `literals` -> `cases` -> `source_files` for every value in `literals` (and
    each value's quote-stripped candidate, see `_candidates`), one row per (literal,
    case, occurrence).

    Each returned row carries a `patterns` list: the original `literals` entry (or
    entries -- a quoted and its stripped form can both trace back to different
    originally-provided values) whose candidate produced that row's match, plus the
    `match_algorithm` and `match_confidence` it matched with.

    A recorded literal matches a provided value per its `string_kind` (see
    `matching.match_confidence`) at or above `min_confidence`. By default it must occur
    *in* the provided value (the provided values are the changing product literals; the
    recorded ones are typically just the part a test actually checks); `reverse=True`
    flips that for substring-matched kinds, and `exact=True` makes every kind exact
    (`reverse` is ignored in that case since equality has no direction)."""
    candidate_to_patterns: dict[str, set[str]] = {}
    for value in literals:
        for candidate in _candidates(value):
            candidate_to_patterns.setdefault(candidate, set()).add(value)
    candidates = sorted(candidate_to_patterns)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        recorded = conn.execute(
            """
            SELECT
                l.value AS literal,
                c.id AS case_id,
                c.scenario_name AS scenario,
                l.situation AS situation,
                l.string_kind AS string_kind,
                l.source_kind AS source_kind,
                l.dynamic AS dynamic,
                sf.path AS file,
                l.source_line AS line,
                l.step_text AS step_text
            FROM literals l
            JOIN cases c ON c.id = l.case_id
            JOIN source_files sf ON sf.id = l.source_file
            """
        ).fetchall()
    finally:
        conn.close()

    # The same (value, string_kind) recurs on many rows; decide each match once per candidate.
    rows_by_literal: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in recorded:
        rows_by_literal.setdefault((row["literal"], row["string_kind"]), []).append(row)

    matched_rows = []
    for candidate in candidates:
        for (value, string_kind), rows in rows_by_literal.items():
            matched = match_confidence(value, candidate, string_kind, reverse=reverse, force_exact=exact)
            if matched is None or matched[1] < min_confidence:
                continue
            algorithm, confidence = matched
            matched_rows.extend(
                {
                    "query": candidate,
                    **dict(row),
                    "match_algorithm": algorithm,
                    "match_confidence": confidence,
                    "patterns": sorted(candidate_to_patterns[candidate]),
                }
                for row in rows
            )
    matched_rows.sort(key=lambda row: (row["query"], row["case_id"], row["file"], row["line"]))
    return matched_rows


def aggregate_by_file(rows: list[dict], *, split_steps: bool = False) -> list[dict]:
    """Collapse one row per (literal, case, file, line) into one row per (literal,
    case, file): `lines` replaces the single `line` column with a sorted, deduped,
    space-delimited string (e.g. "113 122"). `step_text` is similarly collapsed into
    a sorted, deduped list of the distinct step text(s) that matched -- almost always
    one entry, but a literal can occur in more than one step wording within the same
    file. `patterns` collapses to the sorted, deduped union of every row's `patterns`
    -- the originally-provided value(s) that matched this literal/case/file, which can
    be more than one when several provided values match the same recorded literal.
    `match_confidence` is the highest any provided value scored against the group's literal.
    Neither `step_text`, `patterns` nor `match_algorithm` is part of `FIELDS`, so they only
    surface in `--format json/ndjson`.

    Pass `split_steps=True` to keep distinct step wordings as separate rows instead of
    folding them into one row's `step_text` list: the group key then also includes
    `step_text`, so a case/file with two different step wordings produces two rows
    (each with its own `lines`), and each row's `step_text` is a plain string rather
    than a list."""
    groups: dict[tuple, dict] = {}
    for row in rows:
        key = (
            row["literal"],
            row["case_id"],
            row["scenario"],
            row["situation"],
            row["string_kind"],
            row["source_kind"],
            row["file"],
        )
        if split_steps:
            key += (row["step_text"],)
        group = groups.setdefault(
            key,
            {
                "literal": row["literal"],
                "case_id": row["case_id"],
                "scenario": row["scenario"],
                "situation": row["situation"],
                "string_kind": row["string_kind"],
                "source_kind": row["source_kind"],
                "match_algorithm": row["match_algorithm"],
                "match_confidence": row["match_confidence"],
                "file": row["file"],
                "dynamic": 0,
                "_line_numbers": set(),
                "_step_texts": set(),
                "_patterns": set(),
            },
        )
        group["dynamic"] = group["dynamic"] or row["dynamic"]
        group["match_confidence"] = max(group["match_confidence"], row["match_confidence"])
        group["_line_numbers"].add(row["line"])
        group["_step_texts"].add(row["step_text"])
        group["_patterns"].update(row["patterns"])

    aggregated = []
    for group in groups.values():
        line_numbers = group.pop("_line_numbers")
        step_texts = group.pop("_step_texts")
        patterns = group.pop("_patterns")
        group["lines"] = " ".join(str(line_number) for line_number in sorted(line_numbers))
        group["step_text"] = next(iter(step_texts)) if split_steps else sorted(step_texts)
        group["patterns"] = sorted(patterns)
        aggregated.append(group)
    return aggregated


def group_records_by_step(rows: list[dict]) -> list[list[dict]]:
    """Group the per-(literal, case, file, step-wording) records -- as produced by
    `aggregate_by_file(rows, split_steps=True)` -- by (file, step_text), so every
    occurrence of the same step wording in the same file lands in one group,
    regardless of which literal or case produced it. Equivalent to piping that
    per-step-wording record stream through
    `jq -s -c 'group_by("\\(.file)\\(.step_text)")[]'`: each returned group is the list
    of records sharing that (file, step_text) pair, not merged further, so every record
    keeps its own `literal`, `case_id`, `scenario`, `lines`, and `patterns`. Groups are
    sorted by (file, step_text)."""
    split = aggregate_by_file(rows, split_steps=True)
    groups: dict[tuple[str, str], list[dict]] = {}
    for record in split:
        groups.setdefault((record["file"], record["step_text"]), []).append(record)
    return [groups[key] for key in sorted(groups)]


def _write_delimited(rows: list[dict], delimiter: str, header: bool) -> None:
    writer = csv.writer(sys.stdout, delimiter=delimiter, lineterminator="\n")
    if header:
        writer.writerow(FIELDS)
    for row in rows:
        writer.writerow([row[field] for field in FIELDS])


def _write_ndjson(rows: list[dict]) -> None:
    for row in rows:
        click.echo(json.dumps(row))


def _write_text(rows: list[dict], literals: list[str]) -> None:
    matched_queries = {row["query"] for row in rows}
    occurrences_by_literal: dict[str, list[dict]] = {}
    for row in rows:
        occurrences_by_literal.setdefault(row["literal"], []).append(row)

    aggregated_by_literal: dict[str, list[dict]] = {}
    for row in aggregate_by_file(rows):
        aggregated_by_literal.setdefault(row["literal"], []).append(row)

    for literal, occurrences in occurrences_by_literal.items():
        case_ids = sorted({row["case_id"] for row in occurrences})
        click.echo(f'"{literal}" -- {len(occurrences)} occurrence(s) across {len(case_ids)} case(s)')
        for row in aggregated_by_literal[literal]:
            dynamic = " [dynamic]" if row["dynamic"] else ""
            click.echo(
                f"  case {row['case_id']} ({row['scenario']}) {row['file']}:{row['lines']} "
                f"[{row['string_kind']} {row['match_confidence']:.2f}, {row['situation']}/{row['source_kind']}{dynamic}]"
            )

    unmatched = [value for value in literals if not matched_queries.intersection(_candidates(value))]
    if unmatched:
        click.echo("")
        click.echo("No recorded literal matched:")
        for value in unmatched:
            click.echo(f"  - {value}")


def _write_text_grouped(rows: list[dict], literals: list[str]) -> None:
    matched_queries = {row["query"] for row in rows}

    for group in group_records_by_step(rows):
        file = group[0]["file"]
        step_text = group[0]["step_text"]
        click.echo(f'{file} -- "{step_text}" -- {len(group)} record(s)')
        for record in group:
            dynamic = " [dynamic]" if record["dynamic"] else ""
            click.echo(
                f'  literal "{record["literal"]}" case {record["case_id"]} ({record["scenario"]}) '
                f"lines {record['lines']} [{record['string_kind']} {record['match_confidence']:.2f}, "
                f"{record['situation']}/{record['source_kind']}{dynamic}]"
            )

    unmatched = [value for value in literals if not matched_queries.intersection(_candidates(value))]
    if unmatched:
        click.echo("")
        click.echo("No recorded literal matched:")
        for value in unmatched:
            click.echo(f"  - {value}")


@click.command()
@click.argument(
    "literals_file",
    required=False,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help=f"Path to the case-index sqlite db (default: ./{DEFAULT_DB}).",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "tsv", "csv", "json", "ndjson"]),
    default="text",
    help="Output format (default: text).",
)
@click.option(
    "--no-header",
    is_flag=True,
    default=False,
    help="Omit the header row for --format tsv/csv.",
)
@click.option(
    "--reverse",
    is_flag=True,
    default=False,
    help=(
        "Reverse the match direction of the substring-matched kinds: match when a given "
        "value is a substring of a recorded literal, instead of the default (a recorded "
        "literal is a substring of a given value)."
    ),
)
@click.option(
    "--exact",
    is_flag=True,
    default=False,
    help=(
        "Match every kind of literal exactly (equal after whitespace normalization), "
        "instead of per-kind matching. Mutually exclusive with --reverse."
    ),
)
@click.option(
    "--min-confidence",
    type=click.FloatRange(0.0, 1.0),
    default=DEFAULT_MIN_LITERAL_MATCH_CONFIDENCE,
    show_default=True,
    help="Minimum match confidence for a recorded literal to count (the service's min_literal_match_confidence).",
)
@click.option(
    "--group-by-step",
    is_flag=True,
    default=False,
    help=(
        "Group records by (file, step_text) across every literal and case, instead of "
        "collapsing step wordings into one record per literal/case/file. Not supported "
        "with --format tsv/csv."
    ),
)
def cli(
    literals_file: Path | None,
    db_path: Path | None,
    output_format: str,
    no_header: bool,
    reverse: bool,
    exact: bool,
    min_confidence: float,
    group_by_step: bool,
) -> None:
    """Match changed literals (LITERALS_FILE, or stdin if omitted -- one literal per line)
    against the case-index sqlite db, and print which TestRail cases, files, and line
    numbers depend on each one."""
    if exact and reverse:
        raise click.UsageError("--exact and --reverse are mutually exclusive.")
    if group_by_step and output_format in ("tsv", "csv"):
        raise click.UsageError("--group-by-step is not supported with --format tsv/csv; use text, json, or ndjson.")
    db = db_path or Path(DEFAULT_DB)
    if not db.exists():
        raise click.UsageError(f"No case-index db at {db} -- run `python -m e2e_test_literals` to generate it first.")
    raw = literals_file.read_text() if literals_file else sys.stdin.read()
    literals = parse_literals(raw)
    if not literals:
        raise click.UsageError("No literals given (empty input).")

    rows = query_matches(db, literals, reverse=reverse, exact=exact, min_confidence=min_confidence)

    if output_format == "text":
        if group_by_step:
            _write_text_grouped(rows, literals)
        else:
            _write_text(rows, literals)
        return

    if group_by_step:
        groups = group_records_by_step(rows)
        if output_format == "json":
            json.dump(groups, sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            _write_ndjson(groups)
        return

    aggregated = aggregate_by_file(rows)
    if output_format == "json":
        json.dump(aggregated, sys.stdout, indent=2)
        sys.stdout.write("\n")
    elif output_format == "ndjson":
        _write_ndjson(aggregated)
    else:
        _write_delimited(aggregated, delimiter="\t" if output_format == "tsv" else ",", header=not no_header)


if __name__ == "__main__":
    cli()
