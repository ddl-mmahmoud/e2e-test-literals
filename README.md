# e2e-test-literals

A service that answers one question: **"if these product UI strings changed, which
end-to-end test cases are affected?"**

It maps `internal-e2e-tests-service`'s `@testrail(####)`-tagged cucu scenarios to the UI
string-literal / table-column values their execution depends on, and compares that index
against the UI strings *removed* between two commits of a product repo. A removed string
that a test still looks for is a test that is about to break.

This one Domino App/repo hosts the whole pipeline: the HTTP service described first, the
indexer behind it, the diff-extraction component (`changed_literals`) it calls, and the
command-line tools for running those pieces by hand.

## Using the service

The main entry point is `POST /changed-literals-impact`. Give it the e2e test repo, the
product repo, and two refs of the product repo; it tells you which removed UI strings
appear in which test cases.

Long-running work is exposed as a job: submit, poll, fetch.

```
# 1. Submit. Returns 202 with a job_id, status_url and result_url.
curl -X POST "$BASE/changed-literals-impact" \
  -H "Content-Type: application/json" \
  -d '{
        "test_repo": "https://github.com/cerebrotech/internal-e2e-tests-service",
        "test_ref": "main",
        "literals_repo": "https://github.com/cerebrotech/domino",
        "base_ref": "18f72106469b17c5338f076ac588a95dd1ba6374",
        "updated_ref": "f58ba93730d887fafb610f6f3397d25de36a072a",
        "min_removal_confidence": 0.9
      }'

# 2. Poll until status is "done" (or "error", with an "error" message).
curl "$BASE/changed-literals-impact/jobs/$JOB_ID"

# 3. Fetch the result. 409 while the job is still pending/running.
curl "$BASE/changed-literals-impact/jobs/$JOB_ID/result"
```

`$BASE` is the app's URL (on Domino, `https://<domino-host>/apps/e2e-test-literals`;
behind Domino's auth, add an `Authorization: Bearer <PAT>` header). `check_impact.sh` is
this exact flow as a runnable script -- edit its placeholders and run it.

### Request fields

| Field | Default | Meaning |
|---|---|---|
| `test_repo` | required | Git URL of the e2e test repo to index. |
| `test_ref` | `main` | Branch/tag/SHA of the test repo. |
| `literals_repo` | required | Git URL of the product repo whose UI strings changed. |
| `base_ref` | required | Pre-change ref of `literals_repo`. |
| `updated_ref` | required | Post-change ref of `literals_repo`. |
| `min_removal_confidence` | `0.85` | Only removed strings that `changed_literals` scores at or above this are reported (how likely it is to really be user-facing UI text). |
| `min_literal_match_confidence` | `0.9` | Minimum match score between a removed string and a test literal for it to count as a hit. |
| `match_method` | `substring_and_partial_ratio` | How a test literal is compared to a removed string; see below. |
| `min_invocation_confidence` | `0.0` | Only cases matched at or above this get into `recommended_invocations` (a match's confidence is the lower of the removal's `confidence` and its `match_confidence`). The default recommends every matched case. |
| `invocation_max_length` | `300` | Split each recommended `/test-e2e` line at tag boundaries so it is at most this many characters; `0` disables splitting. |
| `extra_cases_allowed` | omitted | Tag substitution (see below): most cases, summed over all lines, that shared tags may run beyond the matched ones. |
| `missing_cases_allowed` | omitted | Tag substitution: most matched cases, summed over all lines, that may keep an explicit `testrail(N)` tag because no shared tag selects them. |

`match_method` is one of:

- `substring` -- the test literal must appear in the removed string (case-insensitive).
  Every match has confidence 1.0.
- `partial_ratio` -- rapidfuzz fuzzy score alone, no containment required. Catches
  paraphrases and typos, but is noisier.
- `substring_and_partial_ratio` (default) -- containment is required, and the fuzzy score
  only grades the confidence of matches that already passed it.

Test literals shorter than 3 characters never match under any method.

### Result

```
{
  "test_repo": ..., "test_ref": ..., "test_sha": ...,   // the resolved test revision
  "literals_repo": ..., "base_ref": ..., "updated_ref": ...,
  "min_removal_confidence": ..., "min_literal_match_confidence": ..., "match_method": ...,
  "min_invocation_confidence": ..., "invocation_max_length": ...,
  "extra_cases_allowed": ..., "missing_cases_allowed": ...,   // null when omitted
  "total": 12,
  "findings": [
    {
      "file": "...", "line": 42, "column": 9,           // where the string was removed
      "text": "Create Project", "context": "...",
      "confidence": 0.95, "change": "removed",
      "matched_test_literals": [
        {
          "value": "Create Project", "match_confidence": 1.0,
          "case_id": 17, "scenario_name": "...", "tags": ["@testrail(1234)"],
          "source_file": "...", "source_line": 8, "step_text": "..."
        }
      ]
    }
  ],
  "recommended_invocations": {
    "commands": ["/test-e2e nexus --on_prem --flavor=qe-aws-eks --tags=testrail(1234),testrail(5678)"],
    "structured": [
      {"test_type": "nexus", "tags": ["testrail(1234)", "testrail(5678)"],
       "flags": ["on_prem"], "args": {"flavor": "qe-aws-eks"}}
    ],
    "uncovered": [],   // cases with no recommendable invocation, with the reason
    "warnings": []     // e.g. this revision has no routing data
  }
}
```

Every removed finding above `min_removal_confidence` is returned, including ones that
matched nothing: an empty `matched_test_literals` means "checked, no test depends on it".
Filter on a non-empty list to get the affected cases.

`recommended_invocations` is the smallest set of `/test-e2e` lines that runs every
matched case (above `min_invocation_confidence`), in two forms: the command strings, and
the same lines as dicts. `flags` are the boolean flags that are on (bare names, no `--`),
`args` the `--key=value` arguments they imply. It never recommends `--vcluster` or the
core-workflow-only cases, and prefers plain `cucu` over nexus/global-config types. Only
UI scenarios drive it.

**Tag substitution.** By default every line lists plain `testrail(N)` tags. Give
`extra_cases_allowed` and/or `missing_cases_allowed` (non-negative integers; whichever is
omitted is unbounded, but if both are omitted nothing is substituted) and shared scenario
tags such as `smoke` replace the `testrail(N)` tags wherever that stays within the bounds,
minimizing extra + missing. Each structured entry then carries `extra_cases`: the case IDs
its shared tags also run beyond the matched ones. Test types that also run API tests keep
plain tags (the API `--tags=` plugin only understands numeric IDs). If no selection fits the
bounds, the plain-tag lines are returned and `warnings` says what is achievable (e.g. `with
at most 0 missing: at least 1 extra`); a `warnings` entry also says if the search hit its
node limit and the selection may not be minimal. The tags come from the per-revision db that
is already built, so tuning these two parameters needs no re-index. The logic is ported from
`map_test_trigger_coverage.py` (`--extra-cases-allowed` / `--missing-cases-allowed`); see
`service/tag_planning.py`.

### Other endpoints

- `POST /revisions` `{"repo": ..., "ref": "main"}` -- build (index) a test revision
  without running an impact check. Same job shape; `GET /revisions/jobs/<id>[/result]`
  to follow it. `GET /revisions` lists every built or in-progress revision.
- `/data/<sha>...` -- each built revision's index served by [Datasette](https://datasette.io):
  its JSON table API (`/data/<sha>/literals.json?value__contains=...`), read-only SQL
  (`/data/<sha>.json?sql=SELECT ...`), CSV export, the HTML browser, and the raw sqlite
  download (`/data/<sha>.db`).
- `GET /` -- the revision-builder web UI (list revisions, kick off a build).

## How test repos are indexed

Indexing turns a test repo at a given ref into a small per-revision sqlite database
(`<sha>.sqlite`) -- the "case index".

1. **Checkout.** The test repo is bare-cloned (cached across runs, keyed by repo URL), the
   ref resolved to a SHA, and just the `tests/` subtree (plus the few routing files, when
   the revision has them) materialized (`repo_checkout.py`). The SHA names the database, so a revision is only ever built
   once; later requests for the same SHA reuse it.
2. **Pass A -- feature files.** Every `.feature` file is parsed; each scenario carrying a
   `@testrail(####)` tag becomes a *case*, and the quoted strings and table-column values
   in its steps are recorded as *literals*, along with the step text and source location.
3. **Pass B -- templatized steps.** Custom steps defined by templates (whose
   `run_steps` bodies contain the real UI strings) are resolved by an AST scan, and the
   literals they use are attributed back to the cases that call them. Skippable with
   `--skip-templated`.
4. **Routing.** For every `@testrail(####)` UI scenario *and* API test, `routing.py`
   records which `/test-e2e` test_type/flag combinations select it, by evaluating the test
   repo's own routing code (`build/circleci/run_ui_tests.py`, `step_run_api_tests.py`) in an
   isolated subprocess. This is stored as plain data (tables `entries`,
   `invocation_candidates`, `entry_labels` -- tags/marks, and the `on_prem`/`vcluster`/
   `is_core`/... flags each candidate needs), so the impact check never needs test-repo
   source. Best-effort: a revision lacking those files still gets its literal index, with
   `meta.routing_indexed = 0` and the reason in `meta.routing_error`. Its logic comes from
   `~/_s/map_test_trigger_coverage.py`.
5. **Write.** The result is written to sqlite (tables `source_files`, `cases`, `tags`,
   `literals`, `patterns`, plus the routing tables above and `meta`) via a temp file and
   atomic rename, so a half-built revision never appears. Revisions are kept indefinitely,
   except that one built before routing data existed is rebuilt once on its next request.

Per-revision databases are served read-only through on-demand Datasette subprocesses,
which are started when first requested and stopped after sitting idle.

## How changed literals are extracted

`changed_literals` (`src/changed_literals/`) finds the candidate UI strings touched by the
diff between two refs of a product repo. It is language-agnostic at the top and
language-specific at the bottom; `CHANGED-LITERALS-DESIGN.md` has the full design.

1. Clone the repo (or reuse a bare cache) and fetch both refs.
2. `git diff --unified=0` restricted to supported file types, parsed into exact
   added/removed line ranges per file.
3. For each changed file, a **language adapter** (TS/TSX/JS/JSX, and Play/Twirl
   `*.scala.html`) parses the pre- and post-change blobs with tree-sitter and keeps only
   string literals overlapping the changed lines.
4. The adapter scores each literal for how likely it is to be user-facing text (JSX text
   and attributes like `placeholder`/`title` score high; identifiers, class names and
   paths score low). Findings below 0.3 are dropped.
5. Each finding records `file`, `line`, `column`, `text`, `context`, `confidence`, and
   `change` (`added` or `removed`).

The impact check only uses the **removed** findings: an added string can't break an
existing test, a removed or reworded one can. For each, `e2e_test_literals.service.impact`
compares its text against every literal in the test revision's index (per
`match_method`) and annotates it with the matches.

### Process layout

One Domino App runs three processes (see `app.sh`), each with its own URL namespace:

- `e2e_test_literals.service_ui` -- the only one bound to a real TCP port (the one
  Domino's ingress exposes). Serves the revision-builder UI and reverse-proxies
  everything else through to the API below.
- `e2e_test_literals.service` -- the API (`/revisions`, `/changed-literals-impact`,
  `/data/...`), bound to its own Unix domain socket, reached only by `service_ui`.
- `changed_literals` -- the diff/extraction tool's small HTTP job service (routes under
  the fixed `/changed-literals` prefix), bound to a second Unix domain socket, reached
  only by `e2e_test_literals.service.changed_literals_client`.

All inter-process calls are plain HTTP over Unix domain sockets; no sibling process talks
over the real network. `changed-literals` used to be a separately deployed Domino App (see
`CHANGED-LITERALS-IMPACT-PLAN.md`) and was folded into this one.

## Command line

Everything the service does can also be run directly. First `uv sync`.

### Indexing a test repo

```
# Against an existing local checkout:
uv run e2e-test-literals --repo-root /path/to/internal-e2e-tests-service

# Or clone/index a ref directly (bare-clones internal-e2e-tests-service under the hood,
# then materializes just the tests/ subtree it needs -- see repo_checkout.py):
uv run e2e-test-literals --repo git@github.com:cerebrotech/internal-e2e-tests-service.git --ref main

# Reuse a bare clone across runs instead of re-cloning every time:
uv run e2e-test-literals --repo <url> --repo-cache ~/.cache/e2e-test-literals/internal-e2e-tests-service.git
```

`--repo-root` and `--repo` are mutually exclusive. `--skip-templated` skips the AST scan
of templatized custom steps' `run_steps` bodies (Pass B) and emits Pass-A-only literals
(what's visible directly in `.feature` files). Output defaults to
`./cucu_literal_deps.sqlite` in the current directory; override with `--out`.

Private-repo auth: set `GITHUB_LITERALS_PAT` to a GitHub PAT -- it's passed as an
`http.extraheader` override, never written to `.git/config`.

### Looking up affected cases

```
uv run e2e-test-literals-lookup changed_literals.txt --format json
# or pipe newline-delimited literals in on stdin
```

Matches each provided literal against the case-index db's recorded literals (substring
match by default -- the provided values are the changing product strings, the recorded
ones are typically just the part a test actually checks; `--reverse` flips the direction,
`--exact` disables substring matching). See `--help` for output formats
(`text`/`tsv`/`csv`/`json`/`ndjson`) and `--group-by-step`.

`lookup.py` has no dependency on the target repo at all -- it only reads the sqlite db,
so it works standalone once a db has been generated.

### Extracting changed literals

```
uv run changed-literals --repo <url-or-path> --base <ref> --updated <ref> [--languages tsx,twirl] [--repo-cache DIR] [--out findings.json]
```

Prints (or writes) the findings as JSON, with a summary on stderr.

### Running the service locally

```
./app.sh        # all three processes; UI on http://127.0.0.1:8888
```

Then point `check_impact.sh`'s `BASE` at `http://127.0.0.1:8888`. Configuration is via
`E2E_TEST_LITERALS_SERVICE_*` and `CHANGED_LITERALS_*` environment variables; see
`src/e2e_test_literals/service/config.py` and `src/changed_literals/app.py`.

### Tests

```
uv run pytest
```

One test (`test_resolve_templated_literals_real_repo_add_launcher_to_source_project`) runs
Pass B against a real `internal-e2e-tests-service` checkout rather than a fabricated
fixture, since it's asserting behavior against a specific real templatized step. It's
skipped unless `E2E_TEST_LITERALS_REPO_ROOT` is set to a local checkout path.
