# changed-literals — design notes

## Origin and purpose

This tool extracts candidate UI-facing strings touched by a git diff, across
multiple languages, for a repo it clones itself. It replaces the *approach*
of `frontend/extract-ui-diff.ts` in the `frontend-web-ui-service` repo, which
does the same job but only for TypeScript/TSX, using `ts-morph` (a
TS-specific AST layer), and only against the checkout already on disk (it
reconstructs the diff's other side in memory from hunks, since it has no
access to the actual other commit).

This tool instead: takes a **repo URL** + **base ref** + **updated ref**,
clones the repo itself, and reads both sides of every changed file directly
via `git show <ref>:<path>` — no in-memory reconstruction needed. That let it
drop a fair amount of the original tool's complexity, and made it
straightforward to support more than one language.

The original planning conversation — including the back-and-forth on scope,
language choices, and the Twirl-specific design — is preserved as a
collaborative plan doc at
`~/deploy/frontend-web-ui-service/CHANGED-LITERALS-PLAN.md`. That's the place
to look for *why* a decision was made; this doc is the current-state map of
*how it's built*.

## Pipeline (`extractor.py::extract`)

1. `git_ops.clone` the repo into a temp dir (or, with `--repo-cache <dir>`,
   `git_ops.ensure_repo_cache` makes `<dir>` a **bare** clone the first time
   and reuses it on every later call instead — see "Repo cache" below), then
   `git_ops.fetch_refs` for both `base` and `updated` — this makes arbitrary
   SHAs/tags resolvable even when they aren't on the default branch's fetched
   history.
2. `git_ops.diff_unified0`: `git diff --unified=0 --no-renames <base> <updated> -- <pathspecs>`.
   `--unified=0` means every hunk contains *only* changed lines, so
   `diff_parser.parse_diff` can read exact added/removed line ranges
   straight off each hunk header (`@@ -oldStart,oldCount +newStart,newCount @@`)
   with no need to walk hunk bodies tracking old/new line bookkeeping.
   `--no-renames` keeps a rename as an independent delete+add so every diff
   entry resolves by a single path.
3. Per changed file: `languages.resolve_adapter(path)` picks a
   `LanguageAdapter` by extension/suffix. Fetch `git show <base>:<path>` and
   `git show <updated>:<path>` (a missing blob means new-file/deleted-file —
   that side is skipped).
4. `walker.collect_findings` parses each blob with the adapter's tree-sitter
   grammar and walks it, keeping only nodes that overlap the diff's
   added/removed line ranges for that side.
5. `adapter.classify` scores each literal; findings below
   `DEFAULT_MIN_CONFIDENCE` (0.3) are dropped.
6. All findings are sorted by confidence descending and emitted as JSON
   (`cli.py`; stdout by default, `--out <path>` to write a file instead).

## Repo cache (`--repo-cache`)

Cloning a large repo from scratch on every invocation is the pipeline's
biggest fixed cost. `--repo-cache <dir>` amortizes it: `<dir>` is populated
with a `git clone --bare` the first time it's used, and every later call
against the same `<dir>` reuses it instead of re-cloning -- the directory is
never cleaned up by this tool.

It's a **bare** clone rather than a normal working-tree checkout because
nothing downstream needs one: `diff_unified0` and `show_blob` both read
straight out of git's object store (`git diff`/`git show <ref>:<path>`), the
same as they do against the temp-dir clone used without `--repo-cache`. A
bare repo has no working tree to check out or leave dirty, which is what
makes sharing one cache dir across concurrent invocations safe -- there's no
checked-out state for one process to disturb while another is mid-diff. If a
future language adapter ever needed real files on disk instead of blobs, the
place to add that is a short-lived `git worktree add`/`git worktree remove`
per invocation against the shared bare cache, not a shared working tree.

Reusing the cache means a branch name passed as `--base`/`--updated` might
have moved upstream since it was last fetched into it, unlike the temp-dir
path where a fresh clone is always current. `fetch_refs(..., force=True)` for
the cache path re-fetches any ref that's already resolvable *and* names a
local branch (a bare repo has no worktree for git to refuse the update on,
unlike the checked-out-branch case the non-cache path's default `force=False`
still has to avoid). A ref that resolves but isn't a branch name -- a SHA, or
a revision expression like `HEAD~1` -- is left alone even under `force`: it's
either immutable already or not a valid fetch refspec to begin with.

## Module map

```
src/changed_literals/
  cli.py           argparse entry point (--repo --base --updated --out --languages --repo-cache)
  git_ops.py       subprocess wrappers: clone, ensure_repo_cache, fetch_refs, diff_unified0, show_blob
  diff_parser.py   parses -U0 diff text -> DiffFile(path, added_lines, removed_lines, is_new_file, is_deleted_file)
  finding.py       Finding dataclass (the JSON output record)
  scoring.py       linguistic_score() -- fallback heuristic when no contextual rule matches
  walker.py        collect_findings(): generic line-overlap AST walk, parameterized by a LanguageAdapter
  extractor.py     orchestrates git_ops + diff_parser + per-file adapter dispatch + walker
  languages/
    base.py        LanguageAdapter dataclass + ClassifyResult -- the extension contract
    __init__.py     extension/suffix -> LanguageAdapter registry (resolve_adapter, pathspecs_for)
    ecmascript.py   TS/TSX/JS/JSX adapter
    twirl.py        Play/Twirl (*.scala.html) adapter
tests/              one test file per module above, plus test_extractor_integration.py (full pipeline against a throwaway local git repo)
oneoffs/            exploratory scripts used to inspect tree-sitter grammar shapes before writing the adapters -- kept for reference, not part of the pipeline
```

## The LanguageAdapter contract (`languages/base.py`)

```python
LanguageAdapter(
    name: str,                                          # CLI --languages key, e.g. "tsx", "twirl"
    parser_name: str,                                    # passed to tree_sitter_language_pack.get_parser()
    literal_texts_of: Callable[[Node], list[str] | None],
    classify: Callable[[Node, str], ClassifyResult | None],
    preprocess: Callable[[bytes], bytes] | None = None,   # optional source rewrite before parsing
)
```

**`literal_texts_of(node)` return-value contract is load-bearing, read it
carefully:**
- `None` → this node is *not* a literal-shaped leaf; the walker keeps
  descending into its children. Used e.g. for a `template_string` *with*
  interpolation, so the walker still finds nested string literals inside the
  `${...}` substitution.
- a `list[str]` (possibly empty) → this node *is* a literal-shaped leaf; the
  walker stops descending into it, and classifies each string in the list
  independently. Usually a single-element list (one string node = one
  string). More than one element happens when an adapter has to split one
  underlying tree node into multiple logical fragments after the fact — see
  "Why multiple fragments" below.

All fragments returned for one node share that node's line/column in the
`Finding` output. That's a known imprecision, harmless for the tool's actual
purpose (matching against the diff's line ranges), but worth knowing if
you're staring at a `Finding.line` that looks slightly off for a
multi-fragment node.

**`classify(node, raw_text)`** returns `ClassifyResult(context, confidence)`
or `None` to drop the finding outright (e.g. blank text). `node` is the
*original* AST/DOM node (so `node.parent` context checks work the same way
regardless of which fragment of `literal_texts_of`'s list is being scored).

**`preprocess(source: bytes) -> bytes`** — an optional whole-file rewrite
applied before parsing. Must preserve byte length and line layout (i.e. only
replace bytes with same-length spans, never insert/delete), because the
diff's line numbers are computed against the *original* file and have to
still line up after preprocessing. Twirl's `mask_scala` is the only user of
this today.

## Adapter: ecmascript.py (TS/TSX/JS/JSX)

Direct port of `classify()`/`literalTextOf()` from
`frontend/extract-ui-shared.ts` in the frontend-web-ui-service repo. One
adapter instance is registered per extension (`.ts`→`typescript` grammar,
`.tsx`→`tsx` grammar, `.js`/`.jsx`→`javascript` grammar — the `javascript`
grammar parses JSX natively), but they all share the same
`literal_texts_of`/`classify` functions since the tree-sitter grammars
produce the same node shapes for everything this cares about.

Grammar node shapes relied on (verified against `tree-sitter-language-pack`
1.20.0 — see "tree-sitter-language-pack notes" below if these ever need
re-verifying against a newer version):
- string literal: `string` node, text is the concatenation of its
  `string_fragment` children (there's normally just one).
- no-substitution template literal: `template_string` node with no
  `template_substitution` child.
- JSX text: `jsx_text` node.
- JSX attribute: `jsx_attribute` node — its named children are
  `[name, value?]` with **no field names** for either in this grammar
  version (name is found by scanning for a `property_identifier`/
  `identifier` child; hyphenated names like `aria-label` come through as a
  single `property_identifier` token, so no special-casing needed there).
- call: `call_expression` has fields `function` (callee) and `arguments`
  (a wrapper node whose children are the actual argument expressions — a
  string argument's *direct* parent is `arguments`, one level below the
  `call_expression` itself).
- object property: `pair` node, fields `key` and `value`.
- variable init: `variable_declarator` node, fields `name` and `value`.

Known extension point (inherited from the original TS tool, not fixed
here): `TemplateExpression` (an interpolated template literal) only
recurses into its substitutions to find nested string literals — the
static text spans around a `${...}` aren't themselves scored.

## Adapter: twirl.py (Play/Twirl `*.scala.html`)

No `tree-sitter-twirl` grammar exists (checked: no such project on GitHub).
A Twirl file is fundamentally an HTML document with embedded Scala via
`@`-prefixed expressions, not Scala with embedded markup, so this adapter
works completely differently from the tree-sitter-native ones:

1. **`mask_scala(source)`** scans the raw bytes and blanks out (replaces
   with spaces, same length, newlines preserved) each `@`-expression
   *header* — `@import ...` (masked to end of line, Twirl-specific), a bare
   `@(params)(implicit ...)` block, an `@identifier(...)....(...)`  call
   chain, or the lone `@` of a bare `@{ expr }` block — while leaving any
   trailing `{ ... }` block body untouched, since that's live template
   content (either a `@helper(...){ block }` block-arg, or an
   `@if(...) { block }`/`@for(...) { block }` control-flow body). Multi-line
   parens/brackets are matched with a small hand-rolled balanced scanner
   (`_skip_balanced`/`_skip_string`) that understands Scala string literals
   well enough not to be confused by `(`/`)` inside them.
2. The masked text is parsed with tree-sitter's **`html`** grammar and
   walked with an HTML-flavored `classify()`, analogous to the JSX one:
   - `text` node → high confidence, after being **split into fragments**
     (see below).
   - `attribute_value` node (nested inside `quoted_attribute_value` inside
     `attribute`) → `ALLOW_ATTRS`/`DENY_ATTRS` (same idea as JSX: allow
     `placeholder`, `aria-label`, `alt`, `title`, `label`; deny `class`,
     `id`, `href`, `name`, `type`, `style`, ...).

**Why `text` nodes are split into fragments.** A masked `@`-expression
doesn't create an HTML node boundary — it just becomes blank space (or a
stray `{`/`}`) *inside* whatever `text` node it was in. If a helper call
sits in the middle of a sentence (a very common Twirl pattern —
`"...are shown. @helpLinkTemplate(...){Learn more}."` — see
`compare.scala.html` in the `domino` repo for a real example), the raw HTML
`text` node's content ends up being one sentence, a long run of blank
padding, a stray `{`, the block-arg text, a stray `}`, and often more
trailing prose — all glued together. `literal_texts_of` splits on
`_FRAGMENT_SPLIT_RE = re.compile(r"[{}]|  +")` (a lone brace, or a run of
2+ spaces — real hand-authored HTML prose essentially never has 2+
consecutive literal spaces) so each real sentence/phrase becomes its own
fragment and its own `Finding`. This was found and fixed via a real-repo
smoke test after the initial implementation — see the "Status" section at
the bottom of `CHANGED-LITERALS-PLAN.md` for the before/after.

**Known limitations, accepted as first-pass tradeoffs** (not bugs to
silently "fix later" without re-checking whether they matter in practice —
see Q6/Q8 in `CHANGED-LITERALS-PLAN.md` for the reasoning):
- A `@x match { case A => { ... } case B => { ... } }` block only has the
  `@x match {` header masked — the `case A => {` fragments themselves
  aren't `@`-expressions, so they leak through as stray low-confidence
  `html-text` nodes alongside real content. This is accepted noise, not a
  missed string: real strings inside each `case` body are still found.
  Downstream consumers filter by confidence, so this tunes out naturally.
- The masker has no way to distinguish "code position" from "template body
  position" — it just scans raw characters for `@`-shaped patterns. Real
  prose that happens to contain something `@`-shaped (an email address like
  `user@example.com`) risks being **mis-masked** (a false *negative*, not
  noise — this is a sharper edge case than the `case`/`match` one). Not yet
  observed in practice; if templates that list user emails/handles start
  showing up in real diffs and losing content, this is the place to look.
  Fixing it properly means writing a real (if partial) Twirl tokenizer that
  understands template-vs-code position, which is a materially bigger
  undertaking (Play's own Twirl compiler devotes real effort just to this).

## Extending: adding a new language

1. Write `languages/<name>.py` with `literal_texts_of(node) -> list[str] | None`
   and `classify(node, raw_text) -> ClassifyResult | None` (see the two
   existing adapters for the pattern). Add a `preprocess` function too if
   the language needs a masking-style rewrite before parsing.
2. Confirm the tree-sitter grammar name and node shapes you're relying on
   by installing nothing extra — `tree_sitter_language_pack` is already a
   dependency — and probing with a throwaway script under `oneoffs/`
   (`uv run oneoffs/<name>.py`), the same way `oneoffs/inspect_grammar_nodes.py`
   and `oneoffs/inspect_jsx_attr_names.py` were used for TS/JS/HTML. Don't
   assume node/field names from memory or from another language's grammar —
   verify them.
3. Register it in `languages/__init__.py`: one `LanguageAdapter(...)`
   instance, one entry in `_MATCHERS` (order matters — a compound suffix
   like `.scala.html` must be checked before anything that could shadow it
   with a bare extension match), and one entry in `PATHSPECS` so the git
   diff pathspec picks up the new extension.
4. Nothing else changes — `walker.py`, `extractor.py`, and `cli.py` are all
   language-agnostic.

Go was scoped out of v1 (see Q3 in `CHANGED-LITERALS-PLAN.md`) purely for
lack of a concrete example of where Go UI strings live in the Domino
codebase, not for any architectural reason — this registry is exactly what
makes it a low-cost follow-up once there's a representative file to model
heuristics on.

## tree-sitter-language-pack notes

The installed version is `tree-sitter-language-pack==1.20.0` from
`xberg-io/tree-sitter-language-pack` (the project moved/expanded from the
older, simpler `Goldziher/tree-sitter-language-pack` package this tool's
author initially expected — same PyPI name, much bigger scope now: 371+
grammars, plus non-Python bindings, a `process()` analysis API, etc. Ignore
web search results that describe the old, simpler API shape).

Verified API surface actually used here:
- `get_parser(name: str) -> tree_sitter.Parser`
- `get_language(name: str) -> tree_sitter.Language` (not currently used
  directly, but available)
- `has_language(name: str) -> bool` / `available_languages() -> list[str]`

Grammars are **downloaded lazily** on first `get_parser`/`get_language`
call and cached (`downloaded_languages()` starts empty even when
`has_language()` already returns `True` for everything) — meaning **the
first real run needs network access** to fetch grammar binaries; subsequent
runs use the cache (`cache_dir()`). This hasn't caused a problem in
practice (this environment has network access), but if `changed-literals`
is ever run somewhere network-restricted, pre-warming the cache (or
vendoring it) would be a prerequisite, not something the tool currently
handles.

Grammars confirmed available and in use: `typescript`, `tsx`, `javascript`,
`html`. `go` and `scala` are also confirmed available (`has_language`
returns `True`) but unused since Go was dropped from v1 and Scala turned
out to mean Twirl templates (HTML+embedded-Scala), not plain `.scala`
source, which the `scala` grammar wouldn't have helped with anyway.

## Testing and verification

```
cd ~/deploy/changed-literals
uv sync
uv run pytest          # 28 tests as of this writing
uv run changed-literals --repo <path-or-url> --base <ref> --updated <ref>
```

`tests/test_extractor_integration.py` builds a throwaway local git repo
with two commits and runs the full pipeline against it (`git` accepts a
local path as a clone source, so this needs no network). Beyond the test
suite, this tool was also smoke-tested against a real commit in the
`domino` repo (`~/deploy/domino`, commit `874d14abcf0`, "Change Help link
text to have the right context") touching both a `.tsx` file and Twirl
templates (`compare.scala.html`, `projectSettings.scala.html`) — 63 clean
findings, correctly split into added/removed, no glued or garbled text.
That's a good diff to re-run against if you change the Twirl adapter and
want a real-world regression check beyond the unit tests.
