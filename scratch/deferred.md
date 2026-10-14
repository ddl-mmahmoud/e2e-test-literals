# Deferred

Things worth doing that we are deliberately not doing yet.

## Impact report UI (see `IMPACT-REPORT-PLAN.md`)

- Long-form LLM prompt design: per-step context, the surrounding steps of the scenario, richer
  instructions for the coding agent. v1 uses the simple one-bullet-per-step prompt.
- Saving the toggled set server-side (v1 keeps it in the browser's `localStorage` only).
- Authentication beyond whatever Domino already provides in front of the app.
- Working out the updated value of a *substring-matched* literal itself (`text`/`element`
  kinds); v1 reports the full old -> new product string and leaves the rest to the agent.
- Column-level highlighting of the literal in feature-file links (needs a `source_column` in
  the index, hence a re-index); v1 links to the line and highlights the text in the page.
- A "view report" link in the revision UI at `/`; v1 is reached by hand with `?report=1`.

