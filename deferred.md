# Deferred

Things worth doing that we are deliberately not doing yet.

## Tag substitution for recommended invocations

Source: `INVOCATIONS-PLAN.md` Q4. The first pass of `recommended_invocations` emits plain
`testrail(N)` tags only. Almost certainly needed later.

- What: replace `testrail(N)` tags with shared scenario tags to shorten invocations, as
  `~/_s/map_test_trigger_coverage.py` does with `--extra-cases-allowed` / `--missing-cases-allowed`.
- Where the logic is: `_case_tag_universe`, `_group_frontier`, `_component_frontier`,
  `_plan_tag_selection`, `_shortfall_message` (~250 lines, Pareto frontier + branch and bound).
- Constraints to carry over: only UI-only test types are eligible (the API `--tags=` plugin only
  understands numeric IDs), and extras (cases the shared tags also run) must be reported.
- Likely shape: optional request params `extra_cases_allowed` / `missing_cases_allowed` on
  `POST /changed-literals-impact`, plus an `extra_cases` note in the result.
