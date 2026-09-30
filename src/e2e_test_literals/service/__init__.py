"""On-demand per-revision case-index generation, served through a pool of per-revision
`datasette serve` subprocesses -- see DATASETTE-WRAPPER-PLAN.md at the repo root for
the design this implements. `app.py` is the FastAPI entry point
(`e2e-test-literals-service` console script)."""
