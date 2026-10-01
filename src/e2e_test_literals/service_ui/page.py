"""The UI's single HTML page, kept as a plain string constant (rather than a static
file on disk) so it ships correctly with the package with no build-system data-file
configuration to get right. It's pure markup/CSS/JS with no server-side templating:
every fetch below uses a path relative to the page's own URL (`api/revisions`, not
`/api/revisions`), so the same bytes work whether this app is mounted at `/` or behind
a `DOMINO_RUN_HOST_PATH` prefix -- see app.py, which serves this at both.
"""

from __future__ import annotations

PAGE_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>e2e-test-literals revisions</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; max-width: 960px; margin: 2rem auto; padding: 0 1rem; }
  h1 { font-size: 1.4rem; }
  h2 { font-size: 1.1rem; margin-top: 2rem; }
  form { display: flex; gap: .5rem; flex-wrap: wrap; align-items: center; }
  input[type=text] { flex: 1 1 260px; padding: .4rem; }
  button { padding: .4rem .9rem; }
  table { width: 100%; border-collapse: collapse; margin-top: 1rem; }
  th, td { text-align: left; padding: .4rem .5rem; border-bottom: 1px solid #8884; font-size: .9rem; }
  .status { margin-top: .5rem; font-size: .9rem; min-height: 1.2em; }
  .status.error { color: #c0392b; }
  .status.ok { color: #1e8449; }
  .badge { font-size: .75rem; padding: .1rem .5rem; border-radius: .6rem; background: #8882; }
  .badge.running { background: #1e8449; color: white; }
</style>
</head>
<body>
<h1>e2e-test-literals revisions</h1>

<form id="build-form">
  <input type="text" id="repo" placeholder="repo (git URL or local path)" value="https://github.com/cerebrotech/internal-e2e-tests-service" required>
  <input type="text" id="ref" placeholder="ref" value="main">
  <button type="submit">Build</button>
</form>
<div id="build-status" class="status"></div>

<h2>Already built</h2>
<button id="refresh" type="button">Refresh</button>
<table>
  <thead><tr><th>SHA</th><th>Built</th><th>Status</th><th>Datasette</th></tr></thead>
  <tbody id="revisions-body"><tr><td colspan="4">Loading…</td></tr></tbody>
</table>

<script>
const form = document.getElementById("build-form");
const statusEl = document.getElementById("build-status");
const tbody = document.getElementById("revisions-body");

function setStatus(text, cls) {
  statusEl.textContent = text;
  statusEl.className = "status" + (cls ? " " + cls : "");
}

async function loadRevisions() {
  tbody.innerHTML = "<tr><td colspan=4>Loading…</td></tr>";
  try {
    const resp = await fetch("api/revisions");
    if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
    const { revisions } = await resp.json();
    if (revisions.length === 0) {
      tbody.innerHTML = "<tr><td colspan=4>No revisions built yet.</td></tr>";
      return;
    }
    tbody.innerHTML = "";
    for (const r of revisions) {
      const tr = document.createElement("tr");
      const shaShort = r.sha.slice(0, 12);
      const built = new Date(r.built_at).toLocaleString();
      const badgeClass = r.running ? "badge running" : "badge";
      const badgeText = r.running ? "running" : "idle";

      const shaCell = document.createElement("td");
      const code = document.createElement("code");
      code.title = r.sha;
      code.textContent = shaShort;
      shaCell.appendChild(code);

      const builtCell = document.createElement("td");
      builtCell.textContent = built;

      const statusCell = document.createElement("td");
      const badge = document.createElement("span");
      badge.className = badgeClass;
      badge.textContent = badgeText;
      statusCell.appendChild(badge);

      const linkCell = document.createElement("td");
      const link = document.createElement("a");
      link.href = r.datasette_url;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = "open";
      linkCell.appendChild(link);

      tr.append(shaCell, builtCell, statusCell, linkCell);
      tbody.appendChild(tr);
    }
  } catch (err) {
    tbody.innerHTML = "";
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 4;
    td.textContent = `Failed to load: ${err}`;
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
}

async function pollJob(statusUrl) {
  for (;;) {
    const resp = await fetch(statusUrl);
    if (!resp.ok) {
      setStatus(`Error checking job status: ${resp.status}`, "error");
      return;
    }
    const body = await resp.json();
    if (body.status === "pending" || body.status === "running") {
      setStatus(`Building… (${body.status})`);
      await new Promise((r) => setTimeout(r, 1500));
      continue;
    }
    if (body.status === "error") {
      setStatus(`Build failed: ${body.error}`, "error");
      return;
    }
    const resultResp = await fetch(body.result_url);
    const result = await resultResp.json();
    setStatus(`Built ${result.sha.slice(0, 12)} — `, "ok");
    const link = document.createElement("a");
    link.href = result.datasette_url;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = "open in Datasette";
    statusEl.appendChild(link);
    await loadRevisions();
    return;
  }
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const repo = document.getElementById("repo").value.trim();
  const ref = document.getElementById("ref").value.trim() || "main";
  if (!repo) return;
  const button = form.querySelector("button");
  button.disabled = true;
  setStatus("Submitting…");
  try {
    const resp = await fetch("api/revisions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo, ref }),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.detail || `${resp.status} ${resp.statusText}`);
    await pollJob(body.status_url);
  } catch (err) {
    setStatus(`Failed to start build: ${err}`, "error");
  } finally {
    button.disabled = false;
  }
});

document.getElementById("refresh").addEventListener("click", loadRevisions);
loadRevisions();
</script>
</body>
</html>
"""
