"""Parse a `git diff --unified=0 --no-renames` text into per-file line sets.

With --unified=0, a hunk contains only changed lines (no context), so the
added/removed line numbers can be read directly off each hunk header
(`@@ -oldStart,oldCount +newStart,newCount @@`) -- no need to walk the hunk
body tracking old/new line-number bookkeeping the way a full-context diff
would require.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class DiffFile:
    path: str = ""
    is_new_file: bool = False
    is_deleted_file: bool = False
    added_lines: set[int] = field(default_factory=set)
    removed_lines: set[int] = field(default_factory=set)


def _strip_prefix(raw_path: str, prefix: str) -> str:
    return raw_path[len(prefix) :] if raw_path.startswith(prefix) else raw_path


def parse_diff(text: str) -> list[DiffFile]:
    files: list[DiffFile] = []
    current: DiffFile | None = None
    a_path: str | None = None

    def flush() -> None:
        if current is not None and current.path and (current.added_lines or current.removed_lines):
            files.append(current)

    for line in text.splitlines():
        if line.startswith("diff --git "):
            flush()
            current = DiffFile()
            a_path = None
            continue

        if current is None:
            continue

        if line.startswith("new file mode"):
            current.is_new_file = True
            continue
        if line.startswith("deleted file mode"):
            current.is_deleted_file = True
            continue

        if line.startswith("--- "):
            raw = line[4:].strip()
            if raw == "/dev/null":
                current.is_new_file = True
                a_path = None
            else:
                a_path = _strip_prefix(raw, "a/")
            continue

        if line.startswith("+++ "):
            raw = line[4:].strip()
            if raw == "/dev/null":
                current.is_deleted_file = True
                b_path = None
            else:
                b_path = _strip_prefix(raw, "b/")
            current.path = b_path or a_path or ""
            continue

        match = _HUNK_HEADER_RE.match(line)
        if match:
            old_start_s, old_count_s, new_start_s, new_count_s = match.groups()
            old_start = int(old_start_s)
            old_count = int(old_count_s) if old_count_s is not None else 1
            new_start = int(new_start_s)
            new_count = int(new_count_s) if new_count_s is not None else 1
            current.removed_lines.update(range(old_start, old_start + old_count))
            current.added_lines.update(range(new_start, new_start + new_count))
            continue

    flush()
    return files
