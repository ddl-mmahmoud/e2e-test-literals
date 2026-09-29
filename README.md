# e2e-test-literals

Maps `internal-e2e-tests-service`'s `@testrail(####)`-tagged cucu scenarios to the UI
string-literal / table-column values their execution depends on, and lets you look up
which cases a batch of *changed* product literals (e.g. from
[`changed-literals`](../changed-literals)) would affect.

Extracted from `internal-e2e-tests-service`'s `tests/helpers/cucu_literal_deps/` so it
can index a checkout without living inside it -- see `src/e2e_test_literals/repo_checkout.py`
for how it gets at the target repo's `tests/` tree.

## Indexing

```
uv sync

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
(what's visible directly in `.feature` files). Output defaults to `./cucu_literal_deps.sqlite`
in the current directory; override with `--out`.

Private-repo auth: set `GITHUB_LITERALS_PAT` to a GitHub PAT (same env var
[`changed-literals`](../changed-literals) uses) -- it's passed as an `http.extraheader`
override, never written to `.git/config`.

## Looking up affected cases

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

## Tests

```
uv run pytest
```

One test (`test_resolve_templated_literals_real_repo_add_launcher_to_source_project`) runs
Pass B against a real `internal-e2e-tests-service` checkout rather than a fabricated
fixture, since it's asserting behavior against a specific real templatized step. It's
skipped unless `E2E_TEST_LITERALS_REPO_ROOT` is set to a local checkout path.
