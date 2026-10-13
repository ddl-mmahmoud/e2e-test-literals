"""Coverage report for step-argument classification (step_kinds.py): how a case-index db's
literals split across `string_kind`, and which step wordings still come out `unknown`
(candidates for a noun/override rule). Usage: uv run python oneoffs/report_string_kinds.py DB.sqlite
"""

import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])

print("rows by string_kind:")
for string_kind, rows, distinct in conn.execute(
    "SELECT string_kind, COUNT(*), COUNT(DISTINCT value) FROM literals GROUP BY 1 ORDER BY 2 DESC"
):
    print(f"  {string_kind:14} rows={rows:6} distinct={distinct}")

print("\nstep wordings with the most distinct `unknown` literals:")
for step_text, distinct in conn.execute(
    "SELECT step_text, COUNT(DISTINCT value) FROM literals WHERE string_kind = 'unknown' "
    "GROUP BY step_text ORDER BY 2 DESC LIMIT 25"
):
    print(f"  {distinct:4} {step_text[:120]}")
