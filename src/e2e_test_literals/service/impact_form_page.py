"""The impact check's input form: one self-contained HTML document (served for
`GET /changed-literals-impact-report`) with a field for each parameter of `POST
/changed-literals-impact`. Submitting it starts that job and sends the browser to the job's
`/result?report=1` page (see `report_page.py`), which until the job is done is the usual 409
JSON saying to poll, and afterwards the report -- so refreshing it is the whole progress UI.

Plain string template like `report_page.py` and `service_ui/page.py`. The two paths the page
requests (`changed-literals-impact`, `changed-literals-impact/jobs/<id>/result`) are relative
to the page's own URL, so the same bytes work at any mount prefix.
"""

from __future__ import annotations

from html import escape

from . import impact

DEFAULT_TEST_REPO = "https://github.com/cerebrotech/internal-e2e-tests-service"
DEFAULT_LITERALS_REPO = "https://github.com/cerebrotech/domino"


def render_form() -> str:
    defaults = {
        "TEST_REPO": DEFAULT_TEST_REPO,
        "LITERALS_REPO": DEFAULT_LITERALS_REPO,
        "MIN_REMOVAL_CONFIDENCE": impact.DEFAULT_MIN_REMOVAL_CONFIDENCE,
        "MIN_LITERAL_MATCH_CONFIDENCE": impact.DEFAULT_MIN_LITERAL_MATCH_CONFIDENCE,
        "MIN_INVOCATION_CONFIDENCE": impact.DEFAULT_MIN_INVOCATION_CONFIDENCE,
        "INVOCATION_MAX_LENGTH": impact.DEFAULT_INVOCATION_MAX_LENGTH,
    }
    page = _TEMPLATE
    for name, value in defaults.items():
        page = page.replace(f"__{name}__", escape(str(value), quote=True))
    return page


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Changed literals impact</title>
<style>
  :root { color-scheme: light dark; --dim: #8889; --line: #8884; }
  [hidden] { display: none !important; }
  body { font-family: system-ui, sans-serif; max-width: 760px; margin: 2rem auto; padding: 0 1rem 3rem; }
  h1 { font-size: 1.4rem; margin-bottom: .25rem; }
  h2 { font-size: 1.05rem; margin: 0 0 .6rem; }
  p.lead { color: var(--dim); margin-top: 0; }
  fieldset { border: 1px solid var(--line); border-radius: .4rem; margin: 0 0 1.2rem; padding: .8rem 1rem 1rem; }
  legend { font-weight: 600; padding: 0 .4rem; }
  .field { display: flex; flex-direction: column; gap: .2rem; margin-bottom: .8rem; }
  .field:last-child { margin-bottom: 0; }
  .field label, .field .label { font-size: .9rem; font-weight: 600; }
  .field small { color: var(--dim); }
  .row { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: .8rem; }
  input[type=text], input[type=number] { padding: .4rem; font: inherit; width: 100%; box-sizing: border-box; }
  .modes { display: flex; gap: 1.2rem; margin-bottom: .8rem; }
  .modes label { display: flex; gap: .4rem; align-items: center; }
  button { padding: .5rem 1.2rem; font: inherit; cursor: pointer; }
  #status { margin-top: .8rem; min-height: 1.2em; font-size: .9rem; }
  #status.error { color: #c0392b; }
</style>
</head>
<body>
<h1>Changed literals impact</h1>
<p class="lead">Diff a product repo between two refs and find the e2e test cases that depend on a removed or reworded string. Submitting starts the check and opens its report; the report page shows a "poll" message until the job is done, so refresh it.</p>

<form id="impact-form">
  <fieldset>
    <legend>Repositories</legend>
    <div class="row">
      <div class="field">
        <label for="test_repo">Test repo</label>
        <input type="text" id="test_repo" value="__TEST_REPO__" required>
        <small>Git URL or local path of the e2e tests.</small>
      </div>
      <div class="field">
        <label for="test_ref">Test ref</label>
        <input type="text" id="test_ref" value="main">
      </div>
    </div>
  </fieldset>

  <fieldset>
    <legend>Change to analyze</legend>
    <div class="modes">
      <label><input type="radio" name="mode" value="pr_url" checked> PR URL</label>
      <label><input type="radio" name="mode" value="pr"> PR number</label>
      <label><input type="radio" name="mode" value="refs"> Two refs</label>
    </div>
    <div id="mode-pr-url" class="field">
      <label for="pr_url">PR URL</label>
      <input type="text" id="pr_url" placeholder="https://github.com/cerebrotech/frontend-web-ui-service/pull/2853">
      <small>The pull request to check; its repo is the one whose strings changed. Diffs the PR's base against its branch.</small>
    </div>
    <div id="mode-repo" class="field" hidden>
      <label for="literals_repo">Literals repo</label>
      <input type="text" id="literals_repo" value="__LITERALS_REPO__">
      <small>The product repo whose strings changed.</small>
    </div>
    <div id="mode-pr" class="field" hidden>
      <label for="pr_number">PR number</label>
      <input type="number" id="pr_number" min="1" step="1">
      <small>Diffs the PR's base against its branch.</small>
    </div>
    <div id="mode-refs" class="row" hidden>
      <div class="field">
        <label for="base_ref">Base ref</label>
        <input type="text" id="base_ref">
      </div>
      <div class="field">
        <label for="updated_ref">Updated ref</label>
        <input type="text" id="updated_ref">
      </div>
    </div>
  </fieldset>

  <fieldset>
    <legend>Thresholds</legend>
    <div class="row">
      <div class="field">
        <label for="min_removal_confidence">Min removal confidence</label>
        <input type="number" id="min_removal_confidence" min="0" max="1" step="any" value="__MIN_REMOVAL_CONFIDENCE__">
        <small>How sure a finding must be that its string was removed.</small>
      </div>
      <div class="field">
        <label for="min_literal_match_confidence">Min literal match confidence</label>
        <input type="number" id="min_literal_match_confidence" min="0" max="1" step="any" value="__MIN_LITERAL_MATCH_CONFIDENCE__">
        <small>How well a test literal must match a removed string.</small>
      </div>
      <div class="field">
        <label for="min_invocation_confidence">Min invocation confidence</label>
        <input type="number" id="min_invocation_confidence" min="0" max="1" step="any" value="__MIN_INVOCATION_CONFIDENCE__">
        <small>0 recommends every matched case.</small>
      </div>
    </div>
  </fieldset>

  <fieldset>
    <legend>Invocations</legend>
    <div class="row">
      <div class="field">
        <label for="invocation_max_length">Max line length</label>
        <input type="number" id="invocation_max_length" min="0" step="1" value="__INVOCATION_MAX_LENGTH__">
        <small>0 means no limit.</small>
      </div>
      <div class="field">
        <label for="extra_cases_allowed">Extra cases allowed</label>
        <input type="number" id="extra_cases_allowed" min="0" step="1">
        <small>Blank: never substitute shared scenario tags.</small>
      </div>
      <div class="field">
        <label for="missing_cases_allowed">Missing cases allowed</label>
        <input type="number" id="missing_cases_allowed" min="0" step="1">
        <small>Blank: never substitute shared scenario tags.</small>
      </div>
    </div>
  </fieldset>

  <button type="submit">Run impact check</button>
  <div id="status" role="status"></div>
</form>

<script>
"use strict";
const form = document.getElementById("impact-form");
const statusEl = document.getElementById("status");
const $ = (id) => document.getElementById(id);

function setStatus(text, isError) {
  statusEl.textContent = text;
  statusEl.className = isError ? "error" : "";
}

function currentMode() {
  return form.elements.mode.value;
}

function syncMode() {
  const mode = currentMode();
  const prUrl = mode === "pr_url";
  const pr = mode === "pr";
  const refs = mode === "refs";
  $("mode-pr-url").hidden = !prUrl;
  $("mode-repo").hidden = prUrl;
  $("mode-pr").hidden = !pr;
  $("mode-refs").hidden = !refs;
  $("pr_url").required = prUrl;
  $("literals_repo").required = !prUrl;
  $("pr_number").required = pr;
  $("base_ref").required = refs;
  $("updated_ref").required = refs;
}

// A blank optional field is left out of the body, so the API's own default applies.
function numberOrNull(id) {
  const raw = $(id).value.trim();
  return raw === "" ? null : Number(raw);
}

function buildPayload() {
  const payload = {
    test_repo: $("test_repo").value.trim(),
    test_ref: $("test_ref").value.trim() || "main",
    extra_cases_allowed: numberOrNull("extra_cases_allowed"),
    missing_cases_allowed: numberOrNull("missing_cases_allowed"),
  };
  const mode = currentMode();
  if (mode === "pr_url") {
    // The API splits a PR URL into the repo and PR number itself.
    payload.pr_url = $("pr_url").value.trim();
  } else {
    payload.literals_repo = $("literals_repo").value.trim();
    if (mode === "pr") {
      payload.pr_number = numberOrNull("pr_number");
    } else {
      payload.base_ref = $("base_ref").value.trim();
      payload.updated_ref = $("updated_ref").value.trim();
    }
  }
  for (const id of ["min_removal_confidence", "min_literal_match_confidence", "min_invocation_confidence", "invocation_max_length"]) {
    const value = numberOrNull(id);
    if (value !== null) payload[id] = value;
  }
  return payload;
}

// FastAPI's 422 `detail` is a list of {loc, msg}; anything else is a string.
function describeError(body, resp) {
  const detail = body && body.detail;
  if (Array.isArray(detail)) {
    return detail.map((d) => `${(d.loc || []).filter((p) => p !== "body").join(".")}: ${d.msg}`).join("; ");
  }
  return detail || `${resp.status} ${resp.statusText}`;
}

form.addEventListener("change", (e) => { if (e.target.name === "mode") syncMode(); });

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  setStatus("Starting…");
  try {
    const resp = await fetch("changed-literals-impact", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildPayload()),
    });
    const body = await resp.json().catch(() => null);
    if (!resp.ok) throw new Error(describeError(body, resp));
    window.location.assign(`changed-literals-impact/jobs/${encodeURIComponent(body.job_id)}/result?report=1`);
  } catch (err) {
    setStatus(`Failed to start the check: ${err.message}`, true);
    button.disabled = false;
  }
});

syncMode();
</script>
</body>
</html>
"""
