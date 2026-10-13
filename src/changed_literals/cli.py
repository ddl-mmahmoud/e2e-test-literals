from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from .extractor import extract
from .git_ops import GitError
from .github import resolve_pull_request
from .languages import PATHSPECS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="changed-literals",
        description="Find candidate UI strings touched by the diff between two git refs.",
    )
    parser.add_argument("--repo", required=True, help="Clonable git repo URL or local path")
    parser.add_argument("--base", help="Base ref (pre-image)")
    parser.add_argument("--updated", help="Updated ref (post-image)")
    parser.add_argument(
        "--pr",
        type=int,
        help="GitHub pull request number, instead of --base/--updated: diffs the PR's base against its branch",
    )
    parser.add_argument("--out", help="Write JSON findings to this file instead of stdout")
    parser.add_argument(
        "--languages",
        help=f"Comma-separated subset of {{{','.join(sorted(PATHSPECS))}}} (default: all)",
    )
    parser.add_argument(
        "--repo-cache",
        help=(
            "Directory for a reusable bare clone of --repo, populated on first use and left "
            "in place afterward. Without this, a full clone is made in a temp dir and "
            "discarded when the run finishes."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.pr is None and not (args.base and args.updated):
        parser.error("give either --base and --updated, or --pr")
    if args.pr is not None and (args.base or args.updated):
        parser.error("--pr can't be combined with --base/--updated")
    languages = {name.strip() for name in args.languages.split(",")} if args.languages else None
    repo_cache = Path(args.repo_cache) if args.repo_cache else None

    try:
        base, updated = args.base, args.updated
        if args.pr is not None:
            base, updated = resolve_pull_request(args.repo, args.pr)
        findings = extract(args.repo, base, updated, languages, repo_cache=repo_cache, pull_request=args.pr)
    except GitError as exc:
        print(f"changed-literals: {exc}", file=sys.stderr)
        return 1

    payload = json.dumps([dataclasses.asdict(f) for f in findings], indent=2)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(payload)
    else:
        print(payload)

    added = sum(1 for f in findings if f.change == "added")
    removed = sum(1 for f in findings if f.change == "removed")
    high_confidence = sum(1 for f in findings if f.confidence >= 0.7)
    print(
        f"Found {len(findings)} candidate UI strings (confidence >= 0.3): "
        f"{added} added, {removed} removed.",
        file=sys.stderr,
    )
    print(f"High confidence (>= 0.7): {high_confidence}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
