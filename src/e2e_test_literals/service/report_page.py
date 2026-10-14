"""The impact report page: one self-contained HTML document (served for
`GET /changed-literals-impact/jobs/<id>/result?report=1`) with the report model
(`report.build_report`) embedded as JSON. Plain string template, like `service_ui/page.py`: no
build step and nothing to ship besides this module.

Vanilla JavaScript, and no request of its own except one: toggling entries recomputes the
`/test-e2e` lines by POSTing the toggled cases to `changed-literals-impact/invocations` (a
path relative to the page's own URL, so it works at any mount prefix), since that logic lives
in `invocations.py` and is deliberately not duplicated here. The rest -- rendering, the
toggle state (kept in localStorage, as the entries *deselected*, so entries new to a rerun
default to on), the prompt text -- needs no server. The code between the `logic:start` and
`logic:end` markers touches no DOM so tests can run it under node.
"""

from __future__ import annotations

import json

from .report import build_report

LOGIC_START = "/* logic:start */"
LOGIC_END = "/* logic:end */"


def _embed(model: dict) -> str:
    """JSON safe to put inside a <script> element."""
    return (
        json.dumps(model)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def render_report(result: dict) -> str:
    return _TEMPLATE.replace("__DATA__", _embed(build_report(result)))


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Impact report</title>
<style>
  :root { color-scheme: light dark; --dim: #8889; --line: #8884; --line-strong: #888c; --mark: #f7d94c66; }
  body { font-family: system-ui, sans-serif; margin: 0; padding: 0 1rem 3rem; }
  h1 { font-size: 1.3rem; margin: 1rem 0 .25rem; }
  h2 { font-size: 1rem; margin: 0 0 .5rem; }
  a { color: inherit; }
  code, pre, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .85rem; }
  .meta { font-size: .85rem; color: var(--dim); margin-bottom: 1rem; }
  .layout { display: grid; grid-template-columns: minmax(0, 3fr) minmax(0, 2fr); gap: 1.5rem; align-items: start; }
  @media (max-width: 1000px) { .layout { grid-template-columns: 1fr; } }
  .side { position: sticky; top: .5rem; display: flex; flex-direction: column; gap: 1rem; max-height: calc(100vh - 1rem); }
  @media (max-width: 1000px) { .side { position: static; max-height: none; } }
  .pane { border: 1px solid var(--line); border-radius: .4rem; padding: .6rem .8rem; min-height: 0; display: flex; flex-direction: column; }
  .pane pre { margin: 0; white-space: pre-wrap; word-break: break-word; overflow: auto; max-height: 40vh; }
  .pane .head { display: flex; justify-content: space-between; align-items: center; gap: .5rem; }
  .toolbar { display: flex; gap: .5rem; align-items: center; flex-wrap: wrap; margin-bottom: 1rem; font-size: .9rem; }
  button { padding: .25rem .7rem; cursor: pointer; }
  .change { border: 1px solid var(--line); border-radius: .4rem; padding: .6rem .8rem; margin-bottom: 1rem; }
  .change-head { display: flex; gap: .5rem; align-items: flex-start; }
  .change-head .where { display: block; }
  .changed-text { display: block; font-weight: 600; margin-top: .1rem; }
  .arrow { color: var(--dim); font-weight: 400; }
  .group { margin: 1.4rem 0 0 1.2rem; border-top: 2px dashed var(--line-strong); padding-top: .8rem; }
  .group-head { display: flex; gap: .5rem; align-items: center; }
  .fold { all: unset; cursor: pointer; width: 1rem; text-align: center; color: var(--dim); flex: none; }
  .fold::before { content: "\25B8"; }
  .fold[aria-expanded="true"]::before { content: "\25BE"; }
  .fold:focus-visible { outline: 2px solid currentColor; border-radius: .15rem; }
  .fold-count { display: none; font-size: .8rem; color: var(--dim); }
  .folded > .change-head .fold-count, .folded > .group-head .fold-count { display: inline; }
  .folded > .children { display: none; }
  .entry { display: flex; gap: .5rem; align-items: flex-start; margin: .5rem 0 0 1.4rem; }
  .entry .lines > div { overflow-wrap: anywhere; }
  .tags { font-size: .8rem; color: var(--dim); }
  .tags a, .tags span { margin-right: .4rem; }
  .step { padding-left: 1rem; }
  mark { background: var(--mark); color: inherit; border-radius: .15rem; padding: 0 .1rem; }
  .dim { color: var(--dim); }
  .warn { color: #b9770e; font-size: .85rem; margin: .4rem 0 0; }
  .error { color: #c0392b; font-size: .85rem; margin: .4rem 0 0; }
  details { margin-top: 1.5rem; }
  details li { margin: .3rem 0; }
</style>
</head>
<body>
<h1>Impact report</h1>
<div class="meta" id="meta"></div>
<div class="layout">
  <main>
    <div class="toolbar">
      <button type="button" id="select-all">Select all</button>
      <button type="button" id="select-none">Select none</button>
      <span id="counts" class="dim"></span>
    </div>
    <div id="changes"></div>
    <div id="unmatched"></div>
  </main>
  <aside class="side">
    <section class="pane">
      <div class="head"><h2>Invocations</h2><button type="button" id="copy-invocations">Copy</button></div>
      <pre id="invocations"></pre>
      <div id="invocations-notes"></div>
    </section>
    <section class="pane">
      <div class="head"><h2>Prompt for a coding agent</h2><button type="button" id="copy-prompt">Copy</button></div>
      <pre id="prompt"></pre>
    </section>
  </aside>
</div>
<script type="application/json" id="report-data">__DATA__</script>
<script>
"use strict";
/* logic:start */
function entriesOf(model) {
  const out = [];
  for (const change of model.changes) for (const group of change.groups) for (const entry of group.entries) out.push(entry);
  return out;
}

// "all" | "some" | "none" -- `off` is the set of deselected entry ids.
function selectionState(entries, off) {
  let on = 0;
  for (const entry of entries) if (!off.has(entry.id)) on++;
  return on === 0 ? "none" : on === entries.length ? "all" : "some";
}

function setSelected(entries, off, selected) {
  for (const entry of entries) {
    if (selected) off.delete(entry.id); else off.add(entry.id);
  }
}

function selectedEntries(model, off) {
  return entriesOf(model).filter((entry) => !off.has(entry.id));
}

function promptText(model, off) {
  const lines = [];
  for (const entry of selectedEntries(model, off)) {
    if (entry.prompt_line !== null && !lines.includes(entry.prompt_line)) lines.push(entry.prompt_line);
  }
  return lines.length ? [model.preamble, ...lines].join("\n") : "";
}

// The cases whose `/test-e2e` lines are wanted: those of selected entries that clear the
// result's own min_invocation_confidence, so the pane agrees with the server's initial answer.
function invocationCaseIds(model, off) {
  const ids = new Set();
  for (const entry of selectedEntries(model, off)) {
    if (entry.confidence >= model.recompute.min_invocation_confidence) ids.add(entry.case_id);
  }
  return [...ids].sort((a, b) => a - b);
}

function recomputeBody(model, caseIds) {
  const r = model.recompute;
  return {
    test_sha: r.test_sha,
    case_ids: caseIds,
    invocation_max_length: r.invocation_max_length,
    extra_cases_allowed: r.extra_cases_allowed,
    missing_cases_allowed: r.missing_cases_allowed,
  };
}

// Deselected ids from what localStorage held, ignoring anything not in this report.
function parseDeselected(raw, model) {
  const valid = new Set(entriesOf(model).map((entry) => entry.id));
  try {
    const ids = JSON.parse(raw);
    if (Array.isArray(ids)) return new Set(ids.filter((id) => valid.has(id)));
  } catch (e) { /* unset or malformed: start with everything on */ }
  return new Set();
}
/* logic:end */

const model = JSON.parse(document.getElementById("report-data").textContent);
const off = parseDeselected(readStorage(), model);
const checkboxes = []; // {box, entries}, refreshed on every change

function readStorage() {
  try { return localStorage.getItem(model.storage_key); } catch (e) { return null; }
}
function writeStorage() {
  try { localStorage.setItem(model.storage_key, JSON.stringify([...off])); } catch (e) { /* private mode etc. */ }
}

function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (key === "class") node.className = value; else node[key] = value;
  }
  for (const child of children) node.append(child);
  return node;
}

function link(url, ...children) {
  if (!url) return el("span", {}, ...children);
  return el("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, ...children);
}

function selector(entries) {
  const box = el("input", { type: "checkbox" });
  box.addEventListener("change", () => {
    setSelected(entries, off, box.checked);
    onSelectionChanged();
  });
  checkboxes.push({ box, entries });
  return box;
}

// A fold toggle for `host`: while it has the `folded` class its `.children` are hidden and the
// head's `.fold-count` ("3 feature files") is shown. The head's checkbox keeps summarising the
// children's selection either way.
function foldToggle(host, folded) {
  const button = el("button", { type: "button", class: "fold", title: "Fold or unfold" });
  const sync = () => button.setAttribute("aria-expanded", String(!host.classList.contains("folded")));
  host.classList.toggle("folded", folded);
  button.addEventListener("click", () => { host.classList.toggle("folded"); sync(); });
  sync();
  return button;
}

function foldCount(n, noun) {
  return el("span", { class: "fold-count" }, "(" + n + " " + noun + (n === 1 ? "" : "s") + ")");
}

function renderEntry(entry) {
  const tags = el("div", { class: "tags" });
  for (const tag of entry.tags) tags.append(link(tag.url || entry.step_url, "@" + tag.text));
  const step = el("div", { class: "step mono" });
  step.append(entry.step.before);
  if (entry.step.literal) step.append(el("mark", {}, entry.step.literal));
  step.append(entry.step.after);
  const lines = el(
    "div",
    { class: "lines" },
    tags,
    el("div", {}, link(entry.case_url, "C" + entry.case_id + " " + entry.scenario_name), " ", link(entry.step_url, el("span", { class: "dim" }, "L" + entry.line))),
    link(entry.step_url, step)
  );
  return el("div", { class: "entry" }, selector([entry]), lines);
}

function renderGroup(group) {
  const host = el("div", { class: "group" });
  const head = el(
    "div",
    { class: "group-head" },
    foldToggle(host, true),
    selector(group.entries),
    link(group.feature_url, el("span", { class: "mono" }, group.file)),
    foldCount(group.entries.length, "entry")
  );
  host.append(head, el("div", { class: "children" }, ...group.entries.map(renderEntry)));
  return host;
}

function renderChange(change) {
  const all = change.groups.flatMap((group) => group.entries);
  const text = el("span", { class: "changed-text" }, link(change.diff_url, '"' + change.text + '"'));
  if (change.replacement !== null) text.append(el("span", { class: "arrow" }, " → "), '"' + change.replacement + '"');
  else text.append(el("span", { class: "arrow" }, " → (no replacement detected)"));
  const where = el("span", { class: "where mono" }, link(change.diff_url, change.file + " L" + change.line + " C" + change.column));
  const host = el("section", { class: "change" });
  const head = el(
    "div",
    { class: "change-head" },
    foldToggle(host, false),
    selector(all),
    el("div", {}, where, text, foldCount(change.groups.length, "feature file"))
  );
  host.append(head, el("div", { class: "children" }, ...change.groups.map(renderGroup)));
  return host;
}

function renderUnmatched() {
  const host = document.getElementById("unmatched");
  if (!model.unmatched.length) return;
  const list = el("ul");
  for (const item of model.unmatched) {
    const label = '"' + item.text + '"' + (item.replacement !== null ? ' → "' + item.replacement + '"' : "");
    list.append(el("li", {}, link(item.diff_url, el("span", { class: "mono" }, item.file + " L" + item.line + " C" + item.column)), " ", label));
  }
  host.append(el("details", {}, el("summary", {}, "Removed strings no test step depends on (" + model.unmatched.length + ")"), list));
}

function renderMeta() {
  const m = model.meta;
  const meta = document.getElementById("meta");
  meta.append(link(m.literals_url, m.literals_repo), " ");
  meta.append(m.pr_number !== null ? "PR #" + m.pr_number : m.base_ref.slice(0, 12) + "…" + m.updated_ref.slice(0, 12));
  meta.append(" · tests ", link(m.test_url, m.test_repo), " @ " + m.test_sha.slice(0, 12));
}

// --- the two panes -----------------------------------------------------------------------
let invocationTimer = null;
let invocationRequest = 0;
const initialCaseIds = invocationCaseIds(model, new Set()).join(",");

function showInvocations(stanza, failure) {
  const notes = document.getElementById("invocations-notes");
  notes.replaceChildren();
  document.getElementById("invocations").textContent = stanza ? stanza.commands.join("\n") : "";
  if (failure) notes.append(el("p", { class: "error" }, failure));
  if (!stanza) return;
  for (const text of stanza.warnings) notes.append(el("p", { class: "warn" }, text));
  for (const text of stanza.uncovered) notes.append(el("p", { class: "warn" }, "uncovered: " + text));
}

function updateInvocations() {
  const caseIds = invocationCaseIds(model, off);
  clearTimeout(invocationTimer);
  const ticket = ++invocationRequest;
  if (caseIds.length === 0) { showInvocations({ commands: [], warnings: [], uncovered: [] }); return; }
  if (caseIds.join(",") === initialCaseIds) { showInvocations(model.invocations); return; }
  invocationTimer = setTimeout(async () => {
    try {
      const response = await fetch("../../invocations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(recomputeBody(model, caseIds)),
      });
      if (!response.ok) throw new Error((await response.text()) || response.statusText);
      const stanza = await response.json();
      if (ticket === invocationRequest) showInvocations(stanza);
    } catch (error) {
      if (ticket === invocationRequest) showInvocations(null, "Could not recompute invocations: " + error.message);
    }
  }, 300);
}

function refresh() {
  for (const { box, entries } of checkboxes) {
    const state = selectionState(entries, off);
    box.checked = state === "all";
    box.indeterminate = state === "some";
  }
  const all = entriesOf(model);
  const chosen = selectedEntries(model, off);
  document.getElementById("counts").textContent =
    chosen.length + " of " + all.length + " entries selected · " + new Set(chosen.map((e) => e.case_id)).size + " cases";
  document.getElementById("prompt").textContent = promptText(model, off) || "(nothing selected)";
}

function onSelectionChanged() {
  writeStorage();
  refresh();
  updateInvocations();
}

async function copyText(text, button) {
  try {
    await navigator.clipboard.writeText(text);
    button.textContent = "Copied";
  } catch (e) {
    button.textContent = "Select and copy manually";
  }
  setTimeout(() => { button.textContent = "Copy"; }, 1500);
}

renderMeta();
document.getElementById("changes").append(...model.changes.map(renderChange));
renderUnmatched();
document.getElementById("select-all").addEventListener("click", () => { setSelected(entriesOf(model), off, true); onSelectionChanged(); });
document.getElementById("select-none").addEventListener("click", () => { setSelected(entriesOf(model), off, false); onSelectionChanged(); });
document.getElementById("copy-invocations").addEventListener("click", (event) => copyText(document.getElementById("invocations").textContent, event.target));
document.getElementById("copy-prompt").addEventListener("click", (event) => copyText(promptText(model, off), event.target));
if (!model.changes.length) document.getElementById("changes").append(el("p", { class: "dim" }, "No removed product string matches a test step."));
refresh();
updateInvocations();
</script>
</body>
</html>
"""
