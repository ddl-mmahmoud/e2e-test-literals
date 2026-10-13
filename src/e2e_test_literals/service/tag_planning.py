"""Choose shared scenario tags to stand in for explicit `testrail(N)` tags in a `/test-e2e`
line, within a budget of extra and missing cases.

Ported from `~/_s/map_test_trigger_coverage.py` (`_pareto_frontier` .. `_plan_tag_selection`;
a standalone copy of `internal-e2e-tests-service`'s `build/circleci/map_test_trigger_coverage.py`).
Unlike the script it raises `TagSelectionError` instead of `click.ClickException`, and works
on integer case IDs.

Vocabulary: a *group* is the case IDs one invocation has to run plus its *tag universe*
(shared tag -> every case ID that tag selects under that invocation's test_type and flags).
Picking a tag may run cases outside the group (*extra*), and a group case no picked tag
selects keeps its explicit `testrail(N)` tag (*missing*). The planner returns the pick
that, summed over all groups, fits both allowances with the fewest extra + missing.
"""

from __future__ import annotations

import math
import sys


class TagSelectionError(ValueError):
    """No tag selection fits the allowances; the message says what is achievable."""


# Per connected component, the exact search gives up after this many nodes and keeps the
# best frontier found so far.
_SEARCH_NODE_LIMIT = 200_000


def _pareto_frontier(points):
    """Drop (extra, missing, witness) points that another point matches or beats on both
    counts. Returns the rest by ascending missing, so extra strictly descends."""
    frontier = []
    for point in sorted(points, key=lambda p: (p[1], p[0])):
        if not frontier or point[0] < frontier[-1][0]:
            frontier.append(point)
    return frontier


def _combine_frontiers(first, second):
    """Frontier of independent choices, one point from each: counts add, witnesses concatenate."""
    return _pareto_frontier((e1 + e2, m1 + m2, w1 + w2) for e1, m1, w1 in first for e2, m2, w2 in second)


def _component_frontier(tags):
    """Exact (extra, missing) Pareto frontier for `tags` (tag -> (target ids, extra ids)),
    by branch and bound: take the target with the fewest candidate tags and branch on which
    tag covers it, or on leaving it uncovered ("missing", i.e. an explicit `testrail(N)`).
    Tags passed over in a branch are forbidden below it, so no tag set is visited twice.

    Returns (frontier, exhausted); `exhausted` means `_SEARCH_NODE_LIMIT` cut the search
    short and the frontier may be missing points."""
    targets = frozenset().union(*(hit for hit, _ in tags.values()))
    size = len(targets)
    tags_of = {t: sorted(tag for tag, (hit, _) in tags.items() if t in hit) for t in targets}
    # reach[m]: fewest extras among solutions found so far with at most m missing.
    reach = [math.inf] * size + [0]
    found = {size: (0, ())}  # missing -> (extra, tags); seeded with "all explicit IDs"
    nodes = 0
    exhausted = False
    # Recursion is one frame per target covered or given up on. Only ever raised, never
    # lowered, but note it is process-wide.
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 4 * size + 100))

    def search(chosen, covered, extras, forbidden):
        nonlocal nodes, exhausted
        nodes += 1
        if nodes > _SEARCH_NODE_LIMIT:
            exhausted = True
            return
        allowed = {t: [a for a in tags_of[t] if a not in forbidden] for t in targets - covered}
        forced_missing = sum(1 for options in allowed.values() if not options)
        if reach[forced_missing] <= len(extras):
            return
        open_targets = {t: options for t, options in allowed.items() if options}
        if not open_targets:
            found[forced_missing] = (len(extras), tuple(chosen))
            for m in range(forced_missing, size + 1):
                reach[m] = min(reach[m], len(extras))
            return
        target = min(open_targets, key=lambda t: (len(open_targets[t]), t))
        ranked = sorted(
            open_targets[target],
            key=lambda a: (len(tags[a][1] - extras), -len(tags[a][0] - covered), a),
        )
        for i, tag in enumerate(ranked):
            hit, extra_ids = tags[tag]
            search([*chosen, tag], covered | hit, extras | extra_ids, forbidden | set(ranked[:i]))
        search(chosen, covered, extras, forbidden | set(ranked))

    search([], frozenset(), frozenset(), frozenset())
    return _pareto_frontier((extra, missing, chosen) for missing, (extra, chosen) in found.items()), exhausted


def _group_frontier(target_case_ids, tag_universe):
    """Exact-as-possible (extra, missing) Pareto frontier of tag selections for one
    invocation. Witnesses are tuples of the shared scenario tags to use; the missing targets
    get explicit `testrail(N)` tags, and the extras are what the chosen tags also select.

    Tags that select no non-target case are free and always taken. The rest are split into
    components that share no target or extra case, solved separately, and recombined.

    Returns (frontier, exhausted), as `_component_frontier`."""
    targets = set(target_case_ids)
    candidates = {}
    for tag, ids in tag_universe.items():
        hit = frozenset(ids & targets)
        if hit:
            candidates[tag] = (hit, frozenset(ids - targets))

    free = {tag: hit for tag, (hit, extras) in candidates.items() if not extras}
    free_tags = []
    covered = set()
    while free:
        tag = max(sorted(free), key=lambda t: len(free[t] - covered))
        if not free[tag] - covered:
            break
        free_tags.append(tag)
        covered |= free.pop(tag)
    remaining = targets - covered

    distinct = {}  # (hit, extras) -> tag; tags with identical effect are interchangeable
    for tag in sorted(candidates):
        hit, extras = candidates[tag]
        hit &= remaining
        if extras and hit:
            distinct.setdefault((hit, extras), tag)
    shared = {tag: key for key, tag in distinct.items()}

    parent = {tag: tag for tag in shared}

    def find(tag):
        while parent[tag] != tag:
            parent[tag] = parent[parent[tag]]
            tag = parent[tag]
        return tag

    owner = {}
    for tag, (hit, extras) in shared.items():
        for element in [("target", c) for c in hit] + [("extra", c) for c in extras]:
            if element in owner:
                parent[find(tag)] = find(owner[element])
            else:
                owner[element] = tag
    components: dict[str, dict] = {}
    for tag, key in shared.items():
        components.setdefault(find(tag), {})[tag] = key

    taggable = set().union(*(hit for hit, _ in shared.values()))
    frontier = [(0, len(remaining - taggable), ())]
    exhausted = False
    for component in components.values():
        component_frontier, component_exhausted = _component_frontier(component)
        exhausted |= component_exhausted
        frontier = _combine_frontiers(frontier, component_frontier)
    return [(extra, missing, (*free_tags, *chosen)) for extra, missing, chosen in frontier], exhausted


def _shortfall_message(frontier, extra_allowed, missing_allowed, exhausted):
    best_extra, best_missing, _ = min(frontier, key=lambda p: (p[0] + p[1], p[0]))
    lines = [
        "no tag selection fits "
        f"extra_cases_allowed={'unbounded' if extra_allowed == math.inf else extra_allowed} and "
        f"missing_cases_allowed={'unbounded' if missing_allowed == math.inf else missing_allowed}; "
        f"fewest extra + missing overall: {best_extra} extra, {best_missing} missing",
    ]
    if missing_allowed != math.inf:
        within = [p for p in frontier if p[1] <= missing_allowed]
        if within:
            lines.append(f"with at most {missing_allowed} missing: at least {min(p[0] for p in within)} extra")
        else:
            lines.append(f"at least {frontier[-1][1]} missing even with unlimited extra cases")
    if extra_allowed != math.inf:
        within = [p for p in frontier if p[0] <= extra_allowed]
        lines.append(f"with at most {extra_allowed} extra: at least {min(p[1] for p in within)} missing")
    if exhausted:
        lines.append("(search limit reached; these minima may not be exact)")
    return "; ".join(lines)


def plan_tag_selection(groups, extra_allowed, missing_allowed):
    """Pick shared tags for every (case_ids, tag_universe) in `groups` so that the extra and
    missing counts, summed over all groups, are within the allowances (`math.inf` = no
    bound); of the selections that fit, take the one minimizing extra + missing. Raises
    `TagSelectionError` with the achievable minima if none fits.

    Returns (per-group tag tuples, exhausted)."""
    frontier = [(0, 0, ())]
    exhausted = False
    for index, (case_ids, tag_universe) in enumerate(groups):
        group_frontier, group_exhausted = _group_frontier(case_ids, tag_universe)
        exhausted |= group_exhausted
        frontier = _combine_frontiers(frontier, [(e, m, ((index, w),)) for e, m, w in group_frontier])
    fitting = [p for p in frontier if p[0] <= extra_allowed and p[1] <= missing_allowed]
    if not fitting:
        raise TagSelectionError(_shortfall_message(frontier, extra_allowed, missing_allowed, exhausted))
    _, _, witness = min(fitting, key=lambda p: (p[0] + p[1], p[0]))
    return [tags for _, tags in sorted(witness)], exhausted
