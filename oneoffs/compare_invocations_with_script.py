"""Compare service/invocations.py's recommendations with ~/_s/map_test_trigger_coverage.py's,
both reading the same real test-repo checkout.

Usage: uv run python oneoffs/compare_invocations_with_script.py ~/_tests
"""

from __future__ import annotations

import random
import subprocess
import sys
import tempfile
from pathlib import Path

from e2e_test_literals import routing
from e2e_test_literals.db import write_sqlite
from e2e_test_literals.service.invocations import recommend_invocations

repo_root = Path(sys.argv[1]).expanduser()
script_dir = Path("~/_s").expanduser()

entries = routing.index_routing(repo_root)
ui_ids = {e["case_id"] for e in entries if e["domain"] == "ui"}
api_ids = {e["case_id"] for e in entries if e["domain"] == "api"}
print(f"entries={len(entries)} ui_cases={len(ui_ids)} api_cases={len(api_ids)} shared={len(ui_ids & api_ids)}")

db_file = Path(tempfile.mkdtemp()) / "db.sqlite"
write_sqlite({}, db_file, routing=entries)


def script_lines(case_ids: list[int]) -> tuple[list[str], list[str]]:
    result = subprocess.run(
        ["uv", "run", "map_test_trigger_coverage.py", "--repo-root", str(repo_root), "--max-length", "300",
         *[arg for c in case_ids for arg in ("--case-id", str(c))]],
        cwd=script_dir, capture_output=True, text=True, check=True,
        stdin=subprocess.DEVNULL,  # the script reads stdin when given no case-id file
    )
    lines = result.stdout.splitlines()
    return [l for l in lines if l.startswith("/test-e2e")], [l for l in lines if l.startswith("#")]


def compare(label: str, case_ids: list[int], domains=("ui",)) -> bool:
    case_ids = sorted(case_ids)
    ours = recommend_invocations(db_file, case_ids, max_length=300, domains=domains)
    theirs, notes = script_lines(case_ids)
    same = ours["commands"] == theirs
    print(f"{label}: ids={len(case_ids)} ours={len(ours['commands'])} script={len(theirs)} identical={same} "
          f"uncovered ours={len(ours['uncovered'])} script={len(notes)}")
    if not same:
        for a, b in zip(ours["commands"], theirs):
            if a != b:
                print("  ours  :", a[:200]); print("  script:", b[:200]); break
        print("  only ours:", sorted(set(ours["commands"]) - set(theirs))[:3])
        print("  only script:", sorted(set(theirs) - set(ours["commands"]))[:3])
    return same


ui_only = sorted(ui_ids - api_ids)
rng = random.Random(0)
ok = compare("all ui-only cases", ui_only)
for i in range(3):
    ok &= compare(f"random subset {i}", rng.sample(ui_only, 40))
ok &= compare("small subset", rng.sample(ui_only, 3))
shared = sorted(ui_ids & api_ids)
if shared:
    ok &= compare("shared ui+api cases (domains=ui,api)", shared, domains=("ui", "api"))
print("ALL IDENTICAL" if ok else "DIFFERENCES FOUND")
