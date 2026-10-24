const $ = (id) => document.getElementById(id);
const api = (path, opts) => fetch(path, opts).then(async (r) => {
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
});

let docs = [];
let poll = null;
let flashId = null;

document.querySelectorAll("nav button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("nav button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $(btn.dataset.panel).classList.add("active");
    if (btn.dataset.panel === "extract") loadFacts();
    if (btn.dataset.panel === "eval") loadEvals();
  });
});

async function refreshHealth() {
  try {
    const h = await api("/api/health");
    $("health").textContent = h.llm ? "Backend ready (LLM on)" : "Backend ready";
  } catch {
    $("health").textContent = "Backend unreachable";
  }
}

async function refreshDocs() {
  docs = await api("/api/documents");
  renderStats();
  renderDocTable();
  fillDocSelect();
  fillAskDocs();
  const busy = docs.some((d) => ["queued", "processing"].includes(d.status) || ["ingesting", "extracting", "claimed", "retry_wait"].includes(d.stage));
  if (busy && !poll) poll = setInterval(refreshDocs, 1200);
  if (!busy && poll) { clearInterval(poll); poll = null; }
}

function renderStats() {
  const n = (s) => docs.filter((d) => d.status === s).length;
  $("stats").innerHTML = [
    ["Documents", docs.length],
    ["Completed", n("completed")],
    ["Queued / processing", n("queued") + n("processing")],
    ["Failed", n("failed")],
  ].map(([k, v]) => `<div class="card"><h3>${k}</h3><div class="n">${v}</div></div>`).join("");
}

function renderDocTable() {
  if (!docs.length) {
    $("doc-table").innerHTML = "<p class='muted'>No documents yet. Upload one file, or upload all 6 at once.</p>";
    return;
  }
  $("doc-table").innerHTML = `<table><thead><tr>
    <th>File</th><th>Progress</th><th>Status</th><th>Type</th><th>Organisation</th><th>Facts</th><th></th>
  </tr></thead><tbody>${docs.map((d) => `<tr class="${d.id === flashId ? "flash" : ""}" data-doc="${d.id}">
    <td>${esc(d.filename)}${d.error ? `<div class="muted bad">${esc(d.error).slice(0, 120)}</div>` : ""}</td>
    <td>
      <div class="progress ${esc(d.status)}"><span style="width:${Number(d.progress || 0)}%"></span></div>
      <div class="muted">${esc(d.stage_label || d.stage || "")}${d.attempts ? ` · try ${d.attempts}/${d.max_attempts || 3}` : ""}</div>
    </td>
    <td><span class="badge ${d.status}">${d.status}</span></td>
    <td>${esc(d.doc_type || "—")}</td>
    <td>${esc(d.organization || "—")}</td>
    <td>${d.n_facts}</td>
    <td class="row-actions">
      <button class="ghost" data-inspect="${d.id}">Inspect</button>
      <button class="ghost" data-retry="${d.id}">${d.status === "failed" ? "Retry this file" : "Reprocess"}</button>
    </td>
  </tr>`).join("")}</tbody></table>`;
  $("doc-table").querySelectorAll("[data-retry]").forEach((b) => {
    b.onclick = async () => {
      await api(`/api/documents/${b.dataset.retry}/retry`, { method: "POST" });
      flashId = b.dataset.retry;
      refreshDocs();
    };
  });
  $("doc-table").querySelectorAll("[data-inspect]").forEach((b) => {
    b.onclick = () => openFacts(b.dataset.inspect);
  });
}

function openFacts(id) {
  document.querySelectorAll("nav button").forEach((x) => x.classList.remove("active"));
  document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
  document.querySelector('nav button[data-panel="extract"]').classList.add("active");
  $("extract").classList.add("active");
  $("doc-select").value = id;
  loadFacts();
}

function fillDocSelect() {
  const sel = $("doc-select");
  const current = sel.value;
  const completed = docs.filter((d) => d.status === "completed");
  sel.innerHTML = completed.map((d) => `<option value="${d.id}">${esc(d.filename)}</option>`).join("");
  if (current && [...sel.options].some((o) => o.value === current)) sel.value = current;
}

function fillAskDocs() {
  const box = $("ask-docs");
  if (!box) return;
  const completed = docs.filter((d) => d.status === "completed");
  const prev = new Set([...box.querySelectorAll("input:checked")].map((i) => i.value));
  const keep = prev.size ? prev : new Set(completed.map((d) => d.id));
  box.innerHTML = completed.map((d) =>
    `<label><input type="checkbox" value="${esc(d.id)}" ${keep.has(d.id) ? "checked" : ""}/> ${esc(d.filename)}</label>`
  ).join("");
}

async function loadFacts() {
  await refreshDocs();
  const id = $("doc-select").value;
  if (!id) { $("fact-table").innerHTML = "<p class='muted'>Process a document first.</p>"; $("fact-meta").innerHTML = ""; return; }
  const doc = await api(`/api/documents/${id}`);
  $("fact-meta").innerHTML = [
    ["Organisation", doc.organization || "—"],
    ["Type", doc.doc_type || "—"],
    ["Facts", doc.n_facts],
    ["Chunks", doc.n_chunks],
  ].map(([k, v]) => `<div class="card"><h3>${k}</h3><div class="n" style="font-size:18px">${esc(String(v))}</div></div>`).join("");
  renderFacts(doc.facts || []);
}

function renderFacts(facts) {
  const q = ($("fact-filter").value || "").toLowerCase();
  const rows = facts.filter((f) => JSON.stringify(f).toLowerCase().includes(q));
  $("fact-table").innerHTML = `<table><thead><tr>
    <th>Name</th><th>Value</th><th>Role</th><th>Provenance</th><th>Period</th><th>Source</th>
  </tr></thead><tbody>${rows.map((f) => `<tr>
    <td>${esc(f.name)}</td>
    <td>${f.value_num != null ? f.value_num : esc(f.value_text || "—")} ${esc(f.unit || "")}</td>
    <td>${esc(f.role)}</td>
    <td><span class="badge ${f.provenance}">${esc(f.provenance)}</span></td>
    <td>${esc([f.period_start, f.period_end].filter(Boolean).join(" → ") || "—")}</td>
    <td>${esc(f.locator)}<div class="fact-quote">${esc(f.quote || "")}</div></td>
  </tr>`).join("")}</tbody></table>`;
}

$("doc-select").addEventListener("change", loadFacts);
$("fact-filter").addEventListener("input", () => loadFacts());

function renderBatch(res) {
  const box = $("batch-box");
  const view = $("batch-view");
  const msg = $("upload-msg");
  if (!res) return;
  const items = res.items || [];
  if (msg) {
    msg.textContent = `Queued ${res.queued || 0}, already present ${res.duplicates || 0}, rejected ${res.rejected || 0}. Each file processes separately.`;
  }
  if (!box || !view) return;
  if (!items.length) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  const label = {
    queued: "Queued",
    requeued: "Queued again after failure",
    duplicate: "Already processed (skipped)",
    already_queued: "Already in queue",
    rejected: "Rejected",
  };
  view.innerHTML = `<table><thead><tr><th>File</th><th>This batch</th><th>Note</th></tr></thead><tbody>
    ${items.map((i) => `<tr>
      <td>${esc(i.filename)}</td>
      <td><span class="badge ${esc(i.outcome)}">${esc(label[i.outcome] || i.outcome)}</span></td>
      <td class="muted">${esc(i.error || (i.document && i.document.stage_label) || "")}</td>
    </tr>`).join("")}
  </tbody></table>`;
}

$("seed").onclick = async () => {
  $("seed").disabled = true;
  const msg = $("upload-msg");
  if (msg) msg.textContent = "Queuing all 6 files…";
  try {
    const res = await api("/api/seed", { method: "POST" });
    flashId = res.documents && res.documents[0] && res.documents[0].id;
    renderBatch(res);
    await refreshDocs();
  } catch (e) {
    if (msg) msg.textContent = e.message;
    alert(e.message);
  } finally {
    $("seed").disabled = false;
  }
};

async function uploadFiles(files) {
  if (!files.length) return;
  const msg = $("upload-msg");
  if (msg) msg.textContent = files.length === 1 ? "Uploading 1 file…" : `Uploading ${files.length} files…`;
  try {
    const body = new FormData();
    [...files].forEach((f) => body.append("files", f));
    const res = await api("/api/documents/upload", { method: "POST", body });
    flashId = res.documents && res.documents[0] && res.documents[0].id;
    renderBatch(res);
    await refreshDocs();
  } catch (e) {
    if (msg) msg.textContent = "Upload failed: " + e.message;
    alert("Upload failed: " + e.message);
  }
  $("file").value = "";
}
$("file").addEventListener("change", (e) => uploadFiles(e.target.files));
;["dragover", "dragenter"].forEach((ev) => $("drop").addEventListener(ev, (e) => { e.preventDefault(); }));
$("drop").addEventListener("drop", (e) => { e.preventDefault(); uploadFiles(e.dataTransfer.files); });

$("ask-btn").onclick = async () => {
  const question = $("question").value.trim();
  if (!question) return;
  $("ask-btn").disabled = true;
  $("answer").innerHTML = "<div class='box'>Working…</div>";
  try {
    const selected = [...document.querySelectorAll("#ask-docs input:checked")].map((i) => i.value);
    const data = await api("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question,
        mode: $("mode").value,
        fy: $("ask-fy").value || null,
        document_ids: selected.length ? selected : null,
      }),
    });
    $("answer").innerHTML = renderAnswer(data);
  } catch (e) {
    $("answer").innerHTML = `<div class="box bad">${esc(e.message)}</div>`;
  } finally {
    $("ask-btn").disabled = false;
  }
};

function renderAnswer(data) {
  const ev = (data.evidence || []).map((e) => `<div class="evidence"><strong>${esc(e.filename)}</strong> · ${esc(e.locator)} · ${esc(e.provenance || e.kind || "")}<div>${esc(e.quote || "")}</div></div>`).join("");
  const calc = (data.calculations || []).map((c) => `<li>${esc(c.expression)} = <strong>${esc(c.result)}</strong> <span class="muted">${esc(c.note || "")}</span></li>`).join("");
  const gaps = (data.gaps || []).map((g) => `<li>${esc(g)}</li>`).join("");
  const inc = (data.inconsistencies || []).map((g) => `<li>${esc(g)}</li>`).join("");
  return `<div class="box">
    <h3>Answer</h3>
    <div class="answer">${esc(data.answer || "")}</div>
    ${calc ? `<h3 style="margin-top:16px">Calculations</h3><ul>${calc}</ul>` : ""}
    ${gaps ? `<h3>Gaps</h3><ul>${gaps}</ul>` : ""}
    ${inc ? `<h3>Inconsistencies</h3><ul>${inc}</ul>` : ""}
    <h3 style="margin-top:16px">Evidence</h3>${ev || "<p class='muted'>None</p>"}
  </div>`;
}

$("fy-btn").onclick = loadFy;
async function loadFy() {
  const label = $("fy-select").value;
  $("fy-view").innerHTML = "<div class='box'>Computing coverage…</div>";
  try {
    const data = await api(`/api/fy/${encodeURIComponent(label)}`);
    const notes = (data.notes || []).map((n) => `<li>${esc(n)}</li>`).join("");
    const rows = (data.included || []).map((i) => `<tr>
      <td>${esc(i.filename)}</td><td>${esc(i.organization || "—")}</td>
      <td>${esc(i.name)} / ${esc(i.role)}</td>
      <td>${i.value_num != null ? i.value_num : "—"} ${esc(i.unit || "")}</td>
      <td>${esc(i.period_start || "")} → ${esc(i.period_end || "")}</td>
      <td><span class="badge ${i.provenance}">${esc(i.provenance)}</span> · ${esc(i.coverage.status)}</td>
      <td>${esc(i.locator)}</td>
    </tr>`).join("");
    const totals = Object.entries(data.org_totals || {}).map(([org, vals]) =>
      `<div class="card"><h3>${esc(org)}</h3><div>${Object.entries(vals).map(([k, v]) => `${esc(k)}: <strong>${Number(v).toLocaleString()}</strong>`).join("<br>")}</div></div>`
    ).join("");
    $("fy-view").innerHTML = `
      <div class="cards">${totals || `<div class="card"><h3>In-period totals</h3><div>None</div></div>`}</div>
      <div class="box"><h3>Coverage notes</h3><ul>${notes}</ul>
      <p class="muted">Window ${esc(data.start)} to ${esc(data.end)}. ${data.outside_count || 0} dated facts fall outside this FY.</p></div>
      <div class="box" style="margin-top:12px; overflow:auto;"><h3>Included records</h3>
      <table><thead><tr><th>Document</th><th>Org</th><th>Fact</th><th>Value</th><th>Period</th><th>Coverage</th><th>Source</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="7">No in-period records</td></tr>`}</tbody></table></div>`;
  } catch (e) {
    $("fy-view").innerHTML = `<div class="box bad">${esc(e.message)}</div>`;
  }
}

async function runEval(mode) {
  const btn = mode === "baseline" ? $("eval-base") : $("eval-imp");
  btn.disabled = true;
  $("eval-view").innerHTML = `<div class="box">Running ${mode} evaluation…</div>`;
  try {
    await api("/api/eval/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode }) });
    await loadEvals();
  } catch (e) {
    $("eval-view").innerHTML = `<div class="box bad">${esc(e.message)}</div>`;
  } finally { btn.disabled = false; }
}
$("eval-base").onclick = () => runEval("baseline");
$("eval-imp").onclick = () => runEval("improved");
$("eval-refresh").onclick = async () => {
  $("eval-refresh").disabled = true;
  $("eval-view").innerHTML = "<div class='box'>Loading saved results…</div>";
  try {
    await loadEvals();
  } catch (e) {
    $("eval-view").innerHTML = `<div class="box bad">${esc(e.message)}</div>`;
  } finally {
    $("eval-refresh").disabled = false;
  }
};

async function loadEvals() {
  const data = await api("/api/eval/results");
  const cmp = data.comparison;
  const runs = data.runs || [];
  if (!runs.length && !(cmp && cmp.baseline && cmp.improved)) {
    $("eval-view").innerHTML = "<p class='muted'>No eval runs yet. Process documents, then run baseline and improved.</p>";
    return;
  }
  let html = "";
  if (cmp && cmp.baseline && cmp.improved) {
    html += `<div class="cards">
      <div class="card"><h3>Baseline pass rate</h3><div class="n">${pct(cmp.baseline.pass_rate)}</div><div class="muted">${cmp.baseline.passed}/${cmp.baseline.n_cases} · avg ${cmp.baseline.average_score}</div></div>
      <div class="card"><h3>Improved pass rate</h3><div class="n">${pct(cmp.improved.pass_rate)}</div><div class="muted">${cmp.improved.passed}/${cmp.improved.n_cases} · avg ${cmp.improved.average_score}</div></div>
    </div>
    <div class="box"><h3>Case-by-case</h3>
    <table><thead><tr><th>Case</th><th>Baseline</th><th>Improved</th><th>Δ</th></tr></thead><tbody>
    ${(cmp.cases || []).map((c) => `<tr>
      <td>${esc(c.case_id)}<div class="muted">${esc(c.question || "")}</div></td>
      <td>${c.baseline_passed ? "<span class='ok'>pass</span>" : "<span class='bad'>fail</span>"} ${c.baseline_score}</td>
      <td>${c.improved_passed ? "<span class='ok'>pass</span>" : "<span class='bad'>fail</span>"} ${c.improved_score}</td>
      <td class="${(c.delta || 0) >= 0 ? "delta-pos" : "delta-neg"}">${(c.delta || 0) > 0 ? "+" : ""}${c.delta ?? 0}</td>
    </tr>`).join("")}
    </tbody></table>
    <p class="muted">Improved: ${(cmp.improved_cases || []).join(", ") || "—"}. Regressed: ${(cmp.regressed_cases || []).join(", ") || "none"}.</p>
    </div>`;
  }
  runs.slice(0, 2).forEach((run) => {
    html += `<div class="box" style="margin-top:12px"><h3>${esc(run.label)} · ${esc(run.mode)}</h3>
      <table><thead><tr><th>Case</th><th>Score</th><th>Checks</th><th>Answer excerpt</th></tr></thead><tbody>
      ${(run.results || []).map((r) => `<tr>
        <td>${esc(r.case_id)}</td>
        <td>${r.passed ? "<span class='ok'>pass</span>" : "<span class='bad'>fail</span>"} ${r.score}</td>
        <td>${(r.checks || []).map((c) => `${c.passed ? "✓" : "✗"} ${esc(c.name)}`).join("<br>")}</td>
        <td class="muted">${esc((r.answer || "").slice(0, 220))}</td>
      </tr>`).join("")}
      </tbody></table></div>`;
  });
  $("eval-view").innerHTML = html;
}

function pct(x) { return `${Math.round((x || 0) * 100)}%`; }
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

const SAMPLE_QUESTIONS = [
  "What electricity consumption does each bill report, and for which period?",
  "Does the water consumption match the meter readings?",
  "What total waste quantity is recorded?",
  "Do the workbook's monthly values and annual summaries agree?",
  "Does the quality policy specify a deadline for resolving customer complaints?",
  "For Indian financial year 2024-25, what electricity consumption is evidenced and how complete is the coverage?",
];
const sampleBox = $("ask-samples");
if (sampleBox) {
  sampleBox.innerHTML = SAMPLE_QUESTIONS.map((q) => `<button class="ghost" type="button">${esc(q)}</button>`).join("");
  sampleBox.querySelectorAll("button").forEach((b) => {
    b.onclick = () => { $("question").value = b.textContent; };
  });
}

refreshHealth();
refreshDocs();
