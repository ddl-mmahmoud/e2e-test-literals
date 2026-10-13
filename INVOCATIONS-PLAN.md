# Recommended test invocations in the impact result — collab plan

Answer the `Q`s inline (just write under each `A:`). Say "go with your recommendations" to delegate the rest.
I haven't changed any code yet. (`invocations.md` is untouched; it has a live `.invocations.md.swp`, so I left it alone.)

## What you asked for (from `invocations.md`)

1. `changed-literals-impact` results get a stanza of recommended `/test-e2e` invocations, the way
   `~/_s/map_test_trigger_coverage.py` produces them, in two forms:
   - strings, e.g. `/test-e2e nexus-workspace --tags=testrail(1234),testrail(67895) --sso --no_cloud`
   - dicts, e.g. `{"test_type": "nexus-workspace", "tags": ["testrail(1234)", "testrail(67895)"], "flags": ["--sso", "--no_cloud"]}`
2. Don't re-clone the test repo; reuse what this service already has, so the revision is the right one.
3. Preferably get there by ingesting more into the DB; if that needs source code, do a collab plan (this file).

## What I found

**The DB alone is not enough.** `map_test_trigger_coverage.py` needs three things:

| Need | In the DB today? |
|---|---|
| UI scenario tags (feature + scenario, inherited) and whether the feature is under `cloud_staging` | Tags: yes (`tags` table, from behave `effective_tags`). Root: derivable from `source_files.path` of `cases.feature_file`. |
| The **routing logic**: `run_ui_tests._build_test_args` / `TEST_TYPE_ROUTING` and `step_run_api_tests._compute_system_test_marks` / `API_TEST_TYPES`. The script *imports and calls* these from the test repo so the mapping tracks routing changes. | No, and it is code, not data. |
| **API tests**: `@pytest.mark.testrail(N)` + marks, found by AST-scanning `tests/api/tests`. A case ID can have both UI and API entries (gpu, cloud-staging), and the script has to cover all of them. | No. The index only covers cucu `.feature` scenarios. |

**Reuse of the checkout is easy, though.** The service already keeps one bare clone per repo
(`generation.repo_cache_path`) and `repo_checkout.materialize_subtree` already extracts subpaths at a SHA via
`git archive`. Today `TESTS_SUBPATHS = ("tests/ui", "tests/common", "tests/helpers")`. The routing modules are
nearly self-contained (stdlib + `click`, already a dependency), and are ~335 + 418 lines. `tests/api/tests` is 4 MB.
So no second clone is needed: archive just those files from the same bare clone at `test_sha`.

## Options

**A. Compute at impact time from a small extra archive (no DB change).**
In `compute_impact`, after resolving `test_sha`, `git archive` `build/circleci/run_ui_tests.py`,
`build/circleci/step_run_api_tests.py`, `tests/api/tests` out of the cached bare clone into a temp dir, load the
two routing modules, port the script's mapping code into a new `service/invocations.py`, and feed it the case IDs
from the matched literals. UI tags come from the existing DB (or the archive, as the script does today).
- Pros: smallest change; always uses the exact revision's routing; existing cached DBs keep working.
- Cons: executes (imports) code from the test repo inside the service process (the indexer already does this with
  step modules, so it's not a new class of risk); some per-job `git archive` cost (small).

**B. Precompute at index time and store in the DB.**
During `resolve_and_generate`, also run the routing logic and write new tables (e.g. `invocation_candidates(case_id,
domain, test_type, on_prem, vcluster, is_core)` and API cases/marks). Impact time becomes pure SQLite + set cover.
- Pros: closest to your "ingest into the DB" preference; no source needed at impact time; also queryable via datasette.
- Cons: schema change means every cached `<sha>.sqlite` lacks the tables (needs a schema-version check + rebuild, or
  fallback); the index step now needs `build/circleci` + `tests/api` in its archive anyway, so the "source needed"
  problem moves to generation rather than disappearing.

**C. Hybrid.** Ingest raw tags (already there) + API marks into the DB, and vendor/copy the routing rules into this
repo as data. Rejected as my default: it duplicates routing logic the script deliberately imports so it can't drift.

**My recommendation: A.** It satisfies "reuse the checkout, correct revision" with the least coupling to schema, and
B can be layered on later if you want it queryable.

## Proposed implementation (assuming A)

1. `repo_checkout.py`: add `ROUTING_SUBPATHS` (`build/circleci/run_ui_tests.py`, `build/circleci/step_run_api_tests.py`,
   `tests/api/tests`, and `tests/ui/features` if I use the script's regex scan instead of the DB) and a
   `checkout_subpaths(repo_url, sha, repo_cache, subpaths)` helper (or reuse `checkout_tree(..., subpaths=...)`, which
   already takes `subpaths`).
2. New `service/invocations.py`: port from the script the pieces that matter for the service:
   `_load_module`, `_extract_ui_scenarios` (or a DB-backed equivalent), `_extract_api_tests` and helpers,
   `_ui_selecting_combos`, `_api_selecting_combos`, `build_mapping`, `_requirement_keys`, `_minimize_invocations`,
   `_trigger_*`, plus the optional tag-substitution planner (`_plan_tag_selection` and friends, ~250 lines) if you want it (Q4).
   Unlike the script it must not use module globals, `sys.exit`/`click.ClickException`, or `logging.disable`
   (process-wide in a server!), so those become parameters / plain exceptions.
3. `impact.py`: after findings are matched, collect distinct `case_id`s from all `matched_test_literals`, call
   `recommend_invocations(...)`, and add to the result:
   ```json
   "recommended_invocations": {
     "commands": ["/test-e2e cucu --tags=testrail(1234),testrail(5678)"],
     "structured": [{"test_type": "cucu", "tags": ["testrail(1234)", "testrail(5678)"], "flags": []}],
     "uncovered": ["4321: no @testrail(4321) tag ... "]
   }
   ```
   `flags` = every non-type, non-`--tags=` token of the line (`--on_prem`, `--flavor=qe-aws-eks`, ...), kept in order.
4. Request params on `POST /changed-literals-impact` (optional): `max_length`, and `extra_cases_allowed` /
   `missing_cases_allowed` if kept (Q4). Wire through `app.py`, `impact_jobs.py`, `check_impact.sh`, README.
5. Tests: unit tests for the mapping/set cover against a tiny fake repo fixture (a couple of `.feature` files, a fake
   `run_ui_tests.py`/`step_run_api_tests.py`), a command-string -> structured-dict round-trip test, and an
   `impact` test with the real function stubbed. No mock-vs-real conflation: I'll state clearly in my report that
   the live run against the real test repo is separate from the fixture tests.

## Questions

**Q1.** Option A (compute at impact time, no DB change) or B (precompute into the DB)? Recommendation: A.

A: B as discussed.

**Q2.** Which case IDs get invocations: every `case_id` in any `matched_test_literals` of the removed findings (my
assumption), or only matches above some higher confidence?

A: Great question, this is an important point.  Let's add a request parameter for tuning the minimum confidence necessary to get an invocation generated.  By default it should assume we want to invoke all found cases.

**Q3.** The API half of the script. Matched literals only ever come from UI scenarios, but a case ID can also be an API
test (shared IDs like gpu / cloud-staging), and the script covers *all* entries of an ID. Keep API handling (faithful to
the script, ~100 lines + the 4 MB archive), or UI-only for the first pass? Recommendation: keep it, since dropping it
could recommend an invocation that doesn't actually run an API half of a shared case.

A: Let's keep it, although we will never recommend API tests since the point of this service is to notice string literal changes.

**Q4.** Tag substitution (`--extra-cases-allowed` / `--missing-cases-allowed`, the Pareto/branch-and-bound planner,
~250 lines): include it now, or first pass emits plain `testrail(N)` tags only? Recommendation: plain tags for the
first pass; add the planner as a follow-up with request params.

A: Plain tags for the first pass please, but we will almost certainly need the tag substitution later.  Please start a document deferred.md which will list some items that I don't want to forget about but for now we should simply defer.  Include tag substitution in this.

**Q5.** Your example line has `--sso --no_cloud`, but the script I'm looking at only ever emits `--on_prem
--flavor=qe-aws-eks` (and never `--vcluster`). Is `~/_s/map_test_trigger_coverage.py` as I read it the current version,
or is there a newer one that also emits `--sso`/`--no_cloud`? If your example was only illustrative, I'll treat
`flags` as "whatever follows the type, excluding `--tags=`".

A: The "flags" should be a list of boolean flags.  Arguments should be converted to dict key/value pairs, i.e. `{"flavor": "qe-aws-eks"}`.  The suggestions of `--sso` and `--no_cloud` were illustrative, not literal.

**Q6.** Should `max_length` splitting (script default: off) be on by default? A CI comment line may have a length cap;
what's the actual cap?

A: Yes, please split on max lengths.  The comment line is 512 characters, but to leave room for manually inserted arguments let's split at 300

**Q7.** The two files `~/_s/map_test_trigger_coverage.py` and the new `service/invocations.py` will then be near-duplicates.
OK to leave the script alone (I won't delete or edit it), and note in the module docstring where the logic came from?

A: Yes leave the script alone please.  And yes, it's a good idea to mention in `service/invocations.py` where the code came from.
