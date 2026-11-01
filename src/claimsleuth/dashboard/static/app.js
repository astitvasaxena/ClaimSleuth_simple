"use strict";
/* ClaimSleuth front end. Everything shown comes from the backend (PDF extraction, the investigation run, the
   deterministic adjudicator, the MCP server's log). Claim and PDF text is untrusted and is always escaped. */

const $ = id => document.getElementById(id);
const S = { draft: null, run: null, view: null, poll: null, docTab: 0, agentTab: 0, status: null };

// ------------------------------------------------------------------ helpers
function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  let body = null;
  try { body = await r.json(); } catch (_) { /* not JSON */ }
  if (!r.ok) throw new Error((body && body.error) || `Request failed (HTTP ${r.status})`);
  return body;
}
const post = (path, data) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
const DECISION = { APPROVE: ["Approve", "v-approve", "ok"], REJECT: ["Reject", "v-reject", "bad"],
  ESCALATE: ["Escalate to a human", "v-escalate", "warn"], REQUEST_EVIDENCE: ["Request evidence", "v-evidence", "info"] };
const decisionText = d => (DECISION[d] || [d || "—"])[0];
const decisionPill = d => d ? `<span class="pill ${(DECISION[d] || [0, 0, "neutral"])[2]}">${esc(d)}</span>` : '<span class="pill neutral">none</span>';
const DECIDED_BY = {
  DETERMINISTIC_RULES: "the deterministic policy rules",
  GUARDRAIL_ESCALATION: "the security guardrail (instructions found in untrusted text)",
  CONFLICT_ESCALATION: "conflict escalation (verified agent concerns the rules did not resolve)",
  INSUFFICIENT_REVIEW: "insufficient review (no completed agent review to corroborate)",
  BASELINE_UNAVAILABLE: "the baseline being unavailable", INTEGRITY_ESCALATION: "an integrity check" };
const REVIEW = { APPROVED: "Approved", REJECTED: "Rejected", INFO_REQUESTED: "More information requested" };
const LABELS = { purchase_date: "Purchase date", serial_number: "Serial number", customer_name: "Customer name",
  policy_id: "Policy number", inspection_date: "Inspection date", diagnosis: "Technician diagnosis",
  repair_cost: "Repair cost", prior_claim: "Earlier claim" };
const DOC_NAMES = { invoice: "Invoice", repair_report: "Repair report", other_text: "Other text" };
const time = iso => { const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleTimeString([], { hour12: false }); };
// Amounts are Indian Rupees with Indian digit grouping (₹1,25,000.00); a record in another currency shows its code.
const money = (v, cur = "INR") => v === null || v === undefined ? "—"
  : `${cur === "INR" ? "₹" : `${cur} `}${Number(v).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const threshold = () => S.status ? money(S.status.human_approval_threshold, S.status.currency) : "the sign-off threshold";
const list = (items, empty) => items && items.length ? `<ul class="plain">${items.map(i => `<li>${esc(i)}</li>`).join("")}</ul>`
  : `<p class="muted small">${esc(empty)}</p>`;
const mark = ok => ok === true ? '<span class="ok">✓</span>' : ok === false ? '<span class="bad">✗</span>' : '<span class="na">–</span>';
function setStatus(text, cls = "") { const p = $("status-pill"); p.textContent = text; p.className = `status-pill ${cls}`; }
function message(html, cls = "") { $("message").innerHTML = html ? `<div class="notice ${cls}">${html}</div>` : ""; }

// ------------------------------------------------------------------ navigation (#investigation?run=<id> reopens a report)
function route() {
  const query = location.hash.replace(/^#/, "").split("?")[1];
  const run = new URLSearchParams(query || "").get("run");
  if (run && (!S.view || S.view.run_id !== run)) openRun(run);
}
window.addEventListener("hashchange", route);

async function loadStatus() {
  try {
    S.status = await api("/api/status");
    const st = S.status;
    const crew = st.services.crewai !== "RUNNING" ? "CrewAI service not running"
      : st.crew_mode === "ollama-local" ? `ollama-local (${st.crew_model})` : st.crew_mode;
    $("mode-pill").textContent = `Mode: ${crew}`;
    $("mode-pill").title = "Evidence and Investigator Agents: test-double. Forensic Agent: " + crew;
    if (st.services.mcp !== "RUNNING" || st.services.crewai !== "RUNNING") {
      message("The investigation services are not running. Start ClaimSleuth with <code>python -m claimsleuth.dashboard</code>.", "warn");
    } else if (st.warnings.length) message(esc(st.warnings.join(" ")), "warn");
    if (S.view) { renderDetails(); renderReview(); }   // the report may have rendered before the status arrived
  } catch (e) { $("mode-pill").textContent = "Mode: unknown"; }
}

// ------------------------------------------------------------------ sample claims
async function loadSamples() {
  const { samples } = await api("/api/samples");
  $("samples").innerHTML = samples.map(s => `<div class="scenario-card" data-file="${esc(s.filename)}">
      <span class="badge">${esc(s.label)}</span>
      <div class="scenario-title">${esc(s.claim_id)}</div>
      <div class="scenario-desc">${esc(s.description)}</div>
      <div class="scenario-actions"><button class="btn-secondary use-sample">Use this claim</button>
        <a class="btn-link" href="/api/samples/${encodeURIComponent(s.filename)}" download>Download PDF</a></div></div>`).join("");
  document.querySelectorAll(".use-sample").forEach(b => b.addEventListener("click", async () => {
    const card = b.closest(".scenario-card");
    const name = card.dataset.file;
    document.querySelectorAll(".scenario-card").forEach(c => c.classList.toggle("active", c === card));
    const blob = await (await fetch(`/api/samples/${encodeURIComponent(name)}`)).blob();
    uploadFile(new File([blob], name, { type: "application/pdf" }));      // the same upload path as a user's file
  }));
}

// ------------------------------------------------------------------ upload and preview
const dz = $("dropzone");
["dragenter", "dragover"].forEach(e => dz.addEventListener(e, ev => { ev.preventDefault(); dz.classList.add("drag"); }));
["dragleave", "drop"].forEach(e => dz.addEventListener(e, ev => { ev.preventDefault(); dz.classList.remove("drag"); }));
dz.addEventListener("drop", ev => { const f = ev.dataTransfer.files[0]; if (f) uploadFile(f); });
$("file-input").addEventListener("change", ev => { const f = ev.target.files[0]; if (f) uploadFile(f); ev.target.value = ""; });

async function uploadFile(file) {
  resetReport();
  S.draft = null;
  $("dz-file").textContent = `${file.name} · ${(file.size / 1024).toFixed(1)} KB · reading…`;
  $("run-btn").disabled = true;
  renderTimeline(null);
  try {
    S.draft = await api("/api/upload", { method: "POST", body: file,
      headers: { "Content-Type": file.type || "application/octet-stream", "X-Filename": encodeURIComponent(file.name) } });
    $("dz-file").textContent = `${S.draft.filename} · ${S.draft.pages} page(s)`;
    renderDraft();
  } catch (e) {
    $("dz-file").textContent = file.name;
    renderChips(null);
    $("preview").innerHTML = `<div class="notice bad">${esc(e.message)}</div>`;
  }
}
function field(d, name) { const f = d.fields.find(x => x.field === name); return f ? f.value : null; }
function renderChips(claim) {
  const chip = (k, v) => `<div class="meta-chip"><span class="meta-k">${k}</span><span class="meta-v">${esc(v || "—")}</span></div>`;
  $("claim-chips").innerHTML = chip("Claim ID", claim && claim.claim_id) + chip("Policy", claim && claim.policy_id) +
    chip("Claimant", claim && claim.claimant_name) + chip("Incident date", claim && claim.incident_date);
}
function renderDraft() {
  const d = S.draft;
  renderChips(d.claim || { claim_id: field(d, "claim_id"), policy_id: field(d, "policy_id"),
    claimant_name: field(d, "claimant_name"), incident_date: field(d, "incident_date") });
  const docs = d.documents.map(x => DOC_NAMES[x.doc_id] || x.doc_id);
  $("preview").innerHTML = `<div><b>${esc(field(d, "device_model") || "Product not found")}</b> · serial ${esc(field(d, "serial_number") || "not stated")}</div>
    <div class="muted small">Documents found: ${esc(docs.join(", ") || "none")}</div>
    ${d.missing.length ? `<div class="small" style="color:var(--amber)">Missing: ${esc(d.missing.join("; "))}</div>` : ""}
    ${d.errors.length ? `<div class="notice bad small">This PDF cannot be investigated:${list(d.errors)}</div>` : ""}`;
  $("run-btn").disabled = !d.can_submit;
  S.docTab = 0;
  // Security findings are shown in the investigation report, not in the upload preview.
  renderDocs(d.documents.map(x => ({ id: x.doc_id, lines: x.lines.map(l => ({ n: l.n, text: l.text, page: l.page, line: l.pdf_line })) })));
  renderTimeline(null);
}

// ------------------------------------------------------------------ source documents
function renderDocs(docs, flagged = []) {
  S.docs = docs;
  $("doc-tabs").innerHTML = docs.map((d, i) => `<button class="doc-tab ${i === S.docTab ? "active" : ""}" data-i="${i}">${esc(DOC_NAMES[d.id] || d.id)}</button>`).join("");
  document.querySelectorAll(".doc-tab").forEach(b => b.addEventListener("click", () => { S.docTab = +b.dataset.i; renderDocs(S.docs, flagged); }));
  const doc = docs[S.docTab];
  $("doc-preview").innerHTML = doc ? doc.lines.map(l => {
    const bad = flagged.some(f => f.startsWith(`doc:${doc.id}#L${l.n} `));
    return `<div class="doc-line ${bad ? "flag" : ""}"><span class="n">${l.n}</span><span class="t">${esc(l.text)}</span><span class="p">${l.page ? `p.${l.page}` : ""}</span></div>`;
  }).join("") : '<span class="muted">No documents.</span>';
}

// ------------------------------------------------------------------ run and progress
$("run-btn").addEventListener("click", async () => {
  if (!S.draft) return;
  $("run-btn").disabled = true;
  message("");
  try {
    const r = await post("/api/runs", { upload_id: S.draft.upload_id });
    S.run = r.run_id;
    setStatus("INVESTIGATING", "running");
    renderTimeline([{ label: "Document extraction", state: "done" }, ...["Evidence Agent", "Investigator Agent", "Forensic Agent", "Final adjudication"].map(l => ({ label: l, state: "waiting" }))]);
    clearInterval(S.poll);
    S.poll = setInterval(pollJob, 1000);
  } catch (e) { message(esc(e.message), "bad"); $("run-btn").disabled = !S.draft.can_submit; }
});
async function pollJob() {
  let job;
  try { job = await api(`/api/jobs/${encodeURIComponent(S.run)}`); } catch (e) { return; }
  renderTimeline(job.stages);
  if (job.state === "running") return;
  clearInterval(S.poll);
  if (job.state === "failed") { setStatus("FAILED", "failed"); message(`The investigation could not finish: ${esc(job.error)}`, "bad"); $("run-btn").disabled = false; return; }
  history.replaceState(null, "", `#investigation?run=${encodeURIComponent(S.run)}`);
  await openRun(S.run);
  $("run-btn").disabled = false;
}
function renderTimeline(stages) {
  if (!stages) {
    const ready = S.draft && S.draft.can_submit;
    stages = ["Document extraction", "Evidence Agent", "Investigator Agent", "Forensic Agent", "Final adjudication"]
      .map((l, i) => ({ label: l, state: i === 0 && ready ? "done" : "waiting" }));
  }
  const ICON = { done: "✓", failed: "✗", running: '<span class="spin"></span>', waiting: "○" };
  const TEXT = { done: "completed", failed: "failed", running: "running…", waiting: "waiting" };
  $("timeline").innerHTML = stages.map((s, i) => `<li class="${s.state}"><span class="step-icon">${ICON[s.state] || "○"}</span>
    <span>${i + 1}. ${esc(s.label)}</span><span class="step-state">${TEXT[s.state] || s.state}</span></li>`).join("");
}

// ------------------------------------------------------------------ report
function resetReport() {
  S.view = null;
  $("results-banner").hidden = true;
  $("report").innerHTML = "";
  $("evidence").innerHTML = '<p class="muted small">No evidence yet. Run an investigation.</p>';
  $("hitl").innerHTML = "";
  $("audit").innerHTML = '<p class="muted small">No investigation yet.</p>';
  $("details-body").innerHTML = '<p class="muted small">Run an investigation to see the agents\' separate reports.</p>';
  setStatus("READY");
  if (location.hash.includes("run=")) history.replaceState(null, "", "#investigation");
}
async function openRun(runId) {
  try { S.view = await api(`/api/runs/${encodeURIComponent(runId)}`); }
  catch (e) { message(`Could not load the report: ${esc(e.message)}`, "bad"); return; }
  S.run = runId;
  renderReport();
}
const check = (v, name) => (v.final.rule_checks || []).find(c => c.name === name);
function mcpResults(v, tool) { return v.mcp.calls.filter(c => c.tool === tool && c.status === "ok" && c.returned).map(c => c.returned); }

function renderReport() {
  const v = S.view, f = v.final, b = v.baseline, c = v.claim || {};
  setStatus(f ? "COMPLETED" : "FAILED", f ? "done" : "failed");
  renderChips(c);
  renderTimeline([{ label: "Document extraction", state: "done" },
    ...v.agents.map(a => ({ label: a.title, state: a.status === "COMPLETED" ? "done" : "failed" })),
    { label: "Final adjudication", state: f ? "done" : "failed" }]);
  if (!S.draft || S.draft.claim?.claim_id !== v.claim_id) {
    $("dz-file").textContent = v.origin && v.origin.filename ? v.origin.filename : v.claim_id;
    $("preview").innerHTML = `<div><b>${esc(c.device_model)}</b> · serial ${esc(c.serial_number || "not stated on the claim form")}</div>`;
  }
  if (!f) { message("This run has no final report.", "bad"); return; }

  // results banner (prototype: Status, Coverage, Settlement Payout, Human Approval)
  const cov = check(v, "coverage_window");
  const latest = v.human_reviews.length ? v.human_reviews[v.human_reviews.length - 1] : null;
  const card = (label, value, cls, note) => `<div class="result-card"><div class="result-card-label">${label}</div>
    <div class="result-card-value ${cls || ""}">${value}</div><div class="result-card-note">${note}</div></div>`;
  $("results-grid").innerHTML =
    card("Automated decision", esc(decisionText(f.final_decision)), (DECISION[f.final_decision] || [])[1], `${esc(f.final_decision)} · decided by ${esc(DECIDED_BY[f.decided_by] || f.decided_by)}`) +
    card("Coverage", cov ? { PASS: "Within warranty", FAIL: "Outside warranty", NOT_EVALUABLE: "Not evaluable" }[cov.outcome] : "—", "", esc(cov ? cov.detail : "")) +
    card("Settlement payout", f.payout ? money(f.payout.payout, f.payout.currency) : "None", f.payout ? "v-approve" : "", f.payout ? "computed by the deterministic rules" : "no payout without an approval") +
    card("Human approval", f.requires_human_approval ? "Required" : "Not required", "",
      latest ? `Human review: ${esc(REVIEW[latest.action])} by ${esc(latest.reviewer)}` : f.requires_human_approval ? "awaiting a reviewer" : "decided automatically");
  $("results-banner").hidden = false;

  // left column: claim summary, findings, explanation, settlement
  const policy = mcpResults(v, "warranty_policy_search").flatMap(r => r.matches || [])[0];
  const records = {};
  mcpResults(v, "claim_history_lookup").forEach(r => (r.records || []).forEach(x => { records[x.claim_id] = x; }));
  const hist = check(v, "claim_history");
  const CONFLICT = ["serial_consistency", "serial_format", "claimant_consistency", "policy_consistency", "document_consistency", "inspection_after_incident", "incident_after_purchase"];
  const map = (v.origin && v.origin.line_map) || {};
  const issues = f.rule_checks.filter(r => CONFLICT.includes(r.name) && r.outcome === "FAIL").map(r => r.detail);
  const abuse = (b.guardrail_events || []).filter(g => g.kind === "abusive_language");
  const policyChecks = ["policy_known", "required_documents", "coverage_window", "exclusions"].map(n => check(v, n)).filter(Boolean);
  $("report").innerHTML = `
    <div class="block"><div class="block-title">Claim summary</div><dl class="kv">
      <dt>Claim ID</dt><dd>${esc(c.claim_id)}</dd><dt>Claimant</dt><dd>${esc(c.claimant_name)} (${esc(c.claimant_id)})</dd>
      <dt>Product</dt><dd>${esc(c.device_model)}</dd><dt>Serial number</dt><dd>${esc(c.serial_number || "not stated on the claim form")}</dd>
      <dt>Incident</dt><dd>${esc(c.incident_date)} – ${esc(c.reported_issue)}</dd>
      ${c.claimant_statement ? `<dt>Statement</dt><dd>${esc(c.claimant_statement)}</dd>` : ""}</dl></div>
    ${integrityBlock(v.integrity)}
    <div class="block"><div class="block-title">Warranty policy</div>
      ${policy ? `<p class="small">${esc(policy.name)} (${esc(policy.policy_id)}): ${esc(policy.terms.duration_months)} months, deductible ${money(policy.terms.deductible)}, limit ${money(policy.terms.max_coverage_limit)}. Exclusions: ${esc(policy.terms.exclusions.join("; "))}.</p>` : '<p class="muted small">Policy not retrieved.</p>'}
      <ul class="check-list">${policyChecks.map(r => `<li>${mark(r.outcome === "PASS" ? true : r.outcome === "FAIL" ? false : null)}<span>${esc(r.detail)}</span></li>`).join("")}</ul></div>
    <div class="block"><div class="block-title">Claim history</div>
      ${Object.values(records).length ? `<ul class="plain">${Object.values(records).map(r => `<li>${esc(r.claim_id)} – ${esc(r.device_model)}, claimant ${esc(r.claimant_id)}, ${esc(r.claim_date)}, ${esc(r.outcome)}</li>`).join("")}</ul>`
        : '<p class="small">No earlier claims found for this device or claimant.</p>'}
      ${hist ? `<p class="small">${mark(hist.outcome === "PASS" ? true : hist.outcome === "FAIL" ? false : null)} ${esc(hist.detail)}</p>` : ""}
      <p class="muted small">Records come from the claim-history database through the claim_history_lookup tool.</p></div>
    <div class="block"><div class="block-title">Suspicious inconsistencies</div>${list(issues, "None found.")}
      ${abuse.length ? `<p class="muted small">Abusive language was noted in the log; it does not affect the decision.</p>` : ""}</div>
    <div class="block"><div class="block-title">Missing documents or evidence</div>${list(b.missing_information, "Nothing missing.")}</div>
    <div class="block"><div class="block-title">Why this decision</div>${list(f.rationale, "")}
      <p class="muted small">The three agents only recommend. The decision comes from the deterministic adjudicator, which re-checks the policy rules and the agents' evidence; it never takes a vote.</p></div>
    ${f.payout ? `<div class="block"><div class="block-title">Settlement breakdown</div><div class="math-grid">
      <div class="math-cell"><div class="math-label">Repair cost</div><div class="math-val">${money(f.payout.repair_cost, f.payout.currency)}</div></div>
      <div class="math-cell"><div class="math-label">Deductible</div><div class="math-val">-${money(f.payout.deductible, f.payout.currency)}</div></div>
      <div class="math-cell"><div class="math-label">Depreciation</div><div class="math-val">-${money(f.payout.depreciation_amount, f.payout.currency)}</div></div>
      <div class="math-cell"><div class="math-label">Approved payout</div><div class="math-val approved">${money(f.payout.payout, f.payout.currency)}</div></div></div>
      <p class="muted small" style="margin-top:6px">${esc(f.payout.explanation)}</p></div>` : ""}`;

  // right column: documents, evidence, human approval, audit
  S.docTab = 0;
  renderDocs(v.documents.map(d => ({ id: d.doc_id, lines: d.lines.map((t, i) => ({ n: i + 1, text: t, page: (map[d.doc_id] || [])[i]?.[0] })) })),
    v.integrity.findings.flatMap(x => x.doc_id ? x.lines.map(n => `doc:${x.doc_id}#L${n} `) : []));
  $("evidence").innerHTML = f.evidence_references.length ? f.evidence_references.map(r => {
    const page = r.line && map[r.doc_id] && map[r.doc_id][r.line - 1] ? ` · PDF page ${map[r.doc_id][r.line - 1][0]}` : "";
    const src = r.doc_id === "claim_history" ? "claim-history database" : `${DOC_NAMES[r.doc_id] || r.doc_id}, line ${r.line}${page}`;
    return `<div class="evidence-item"><span class="k">${esc(LABELS[r.field] || r.field)}</span><span>${esc(r.value)}<div class="src">${esc(src)}</div></span></div>`;
  }).join("") : '<p class="muted small">No evidence could be extracted.</p>';
  renderReview();
  $("audit").innerHTML = v.audit_trail.map(a => `<div class="audit-entry ${a.kind}"><span class="t">${time(a.ts)}</span>
    <span>${a.kind === "human" ? `<b>Human review: ${esc(REVIEW[a.action] || a.action)}</b> by ${esc(a.reviewer)} <span class="d">(automated decision ${esc(a.automated_decision)} unchanged)</span>` : esc(a.text)}
    ${a.detail && a.kind === "human" ? `<div class="d">“${esc(a.detail)}”</div>` : ""}</span></div>`).join("");
  renderDetails();
}

// Security & Document Integrity: only what the backend recorded for this run (detection events, PDF positions,
// the agents' own findings, the MCP server's history-lookup log).
const INTEGRITY = { "Clean": "integ-clean", "Suspicious": "integ-suspicious", "Prompt Injection Detected": "integ-detected" };
function integrityBlock(sec) {
  const range = ns => ns.length > 1 ? `lines ${ns[0]}–${ns[ns.length - 1]}` : `line ${ns[0]}`;
  const where = x => {
    const pages = [...new Set(x.pdf.map(p => p[0]))];
    const pdf = x.pdf.length ? `PDF page ${pages.join(", ")}, ${range(x.pdf.map(p => p[1]))}` : "";
    const doc = x.lines.length ? `extracted ${range(x.lines)}` : "";
    return [pdf, doc].filter(Boolean).join(" · ") || "—";
  };
  const findings = sec.findings.map(x => `<div class="finding">
      <div class="finding-type">${esc(x.type)}</div>
      <dl class="kv"><dt>Source</dt><dd>${esc(x.source)}</dd><dt>Location</dt><dd>${esc(where(x))}</dd>
        <dt>Detected instruction</dt><dd class="quote">“${esc(x.text)}”</dd>
        <dt>Detection</dt><dd>${esc(x.detection)}${x.reported_by.length ? `<br>Also reported in their own findings by: ${esc(x.reported_by.join(", "))}` : "<br>No agent reported it in its own findings."}</dd>
        <dt>System response</dt><dd>${list(x.response, "")}</dd></dl></div>`).join("");
  const note = sec.findings.length ? "" : sec.status === "Clean"
    ? '<p class="small">No instructions addressed to the system were found in the claim form or the documents.</p>'
    : `<p class="small">${sec.agent_only_flags.length ? `Flagged by ${esc(sec.agent_only_flags.join(", "))} but not by the deterministic screen.` : ""}
       ${sec.history_scope.refused.length ? `${sec.history_scope.refused.length} out-of-scope history request(s) refused by the MCP server: ${esc(sec.history_scope.refused[0].error)}` : ""}</p>`;
  return `<div class="block integrity ${INTEGRITY[sec.status] || ""}"><div class="block-title">Security &amp; Document Integrity</div>
    <div class="integrity-status ${INTEGRITY[sec.status] || ""}">${esc(sec.status)}</div>${note}${findings}
    <p class="muted small">${esc(sec.history_line)}</p></div>`;
}

function renderReview() {
  const v = S.view, f = v.final;
  const latest = v.human_reviews.length ? v.human_reviews[v.human_reviews.length - 1] : null;
  if (!f.requires_human_approval) {
    $("hitl").innerHTML = `<div class="hitl-title">Human Approval</div><p class="small muted">Not required: the automated decision is
      ${esc(f.final_decision)}${f.payout ? ` with a payout below the ${threshold()} sign-off threshold` : ""}.</p>`;
    return;
  }
  $("hitl").innerHTML = `<div class="hitl-title">Human Approval</div>
    <p class="small muted">Required for payouts of ${threshold()} or more, rejections and escalations. Your action is recorded separately; the automated decision is never changed.</p>
    <div class="decision-pair"><div>Automated decision<b>${esc(f.final_decision)}</b></div>
      <div>Human review<b>${latest ? `${esc(REVIEW[latest.action])} – ${esc(latest.reviewer)}` : "Pending"}</b></div></div>
    <div class="hitl-fields"><input id="reviewer" placeholder="Reviewer name" maxlength="80" value="${esc(localStorage.getItem("cs-reviewer") || "")}">
      <textarea id="review-notes" rows="2" placeholder="Reviewer notes" maxlength="2000"></textarea></div>
    <div class="hitl-actions"><button class="btn-action btn-approve" data-a="APPROVED">✔ Approve &amp; Close</button>
      <button class="btn-action btn-reject" data-a="REJECTED">✖ Reject Claim</button>
      <button class="btn-action btn-info" data-a="INFO_REQUESTED">✉ Request More Information</button></div>
    <div id="review-msg" class="small" style="margin-top:6px"></div>`;
  document.querySelectorAll(".hitl-actions button").forEach(btn => btn.addEventListener("click", async () => {
    const reviewer = $("reviewer").value.trim();
    if (!reviewer) { $("review-msg").innerHTML = '<span class="bad">Enter the reviewer\'s name first.</span>'; return; }
    try { localStorage.setItem("cs-reviewer", reviewer); } catch (_) { /* optional */ }
    try {
      await post(`/api/runs/${encodeURIComponent(S.run)}/review`, { action: btn.dataset.a, reviewer, notes: $("review-notes").value });
      await openRun(S.run);
    } catch (e) { $("review-msg").innerHTML = `<span class="bad">${esc(e.message)}</span>`; }
  }));
}

// ------------------------------------------------------------------ investigation details (collapsed)
function agentReport(a) {
  const o = a.output || {};
  const model = a.model === "test-double" ? '<span class="tag td">test-double · not LLM reasoning</span>'
    : a.model ? `<span class="tag live">${esc(a.model)}${a.genuine_inference ? " · local model inference" : ""}</span>` : "";
  const mcp = a.tool_calls.filter(t => t.protocol === "mcp");
  const cites = f => (f.citations || []).map(c => `${DOC_NAMES[c.doc_id] || c.doc_id} line ${c.line} ${c.verified ? "✓" : "✗ not in document"}`).join("; ");
  const checks = (title, cs) => cs && cs.length ? `<p class="small"><b>${title}</b></p><ul class="check-list">${cs.map(x =>
    `<li>${mark(x.result === "CONSISTENT" ? true : x.result === "INCONSISTENT" ? false : null)}<span>${esc(x.check)}: ${esc(x.detail)}</span></li>`).join("")}</ul>` : "";
  return `<div class="agent-report"><h3>${esc(a.title)}</h3>
    <p class="small muted">${esc(a.framework)} · ${esc(a.responsibility)}</p>
    <p class="small"><span class="pill ${a.status === "COMPLETED" ? "ok" : "bad"}">${esc(a.status)}</span> ${model}
      <span class="muted">${a.latency_ms ? `${(a.latency_ms / 1000).toFixed(2)} s` : ""}${mcp.length ? ` · MCP tools: ${esc([...new Set(mcp.map(t => t.tool))].join(", "))}` : ""}</span></p>
    ${a.errors.length ? `<div class="notice bad small">${list(a.errors)}</div>` : ""}
    ${a.output ? `<p class="small"><b>Recommendation:</b> ${decisionPill(a.recommendation)} <span class="muted">(recommendation only)</span></p>
      <p class="small">${esc(o.reasoning_summary)}</p>
      <p class="small"><b>Findings</b></p>${(o.findings || []).length ? `<ul class="plain">${o.findings.map(f => `<li><b>${esc(f.severity)}</b> ${esc(f.description)}${cites(f) ? ` <span class="muted">(${esc(cites(f))})</span>` : ""}</li>`).join("")}</ul>` : '<p class="muted small">No findings.</p>'}
      ${a.key === "investigator_agent" ? `<p class="small"><b>Policy eligibility:</b> ${esc(o.policy_eligibility)} – ${esc(o.policy_eligibility_reason)}</p>` : ""}
      ${checks("Serial-number checks", o.serial_number_checks)}${checks("Chronology checks", o.chronology_checks)}
      <p class="small"><b>Missing information:</b> ${esc((o.missing_information || []).join("; ") || "none")}</p>` : ""}</div>`;
}
function renderDetails() {
  const v = S.view, forensic = v.agents[2];
  const live = S.status && S.status.latest_live_attempt;
  const crewMode = forensic.llm_mode === "live" ? `${forensic.model} (local model, ollama-local)` : forensic.llm_mode || "unknown";
  $("details-body").innerHTML = `<p class="small"><b>Execution mode of this investigation.</b> Evidence and Investigator Agents (Google ADK):
      test-double – a labelled offline stand-in, not LLM reasoning. Forensic Agent (CrewAI, reached over A2A): ${esc(crewMode)}.
      ${live ? `<span class="muted">The latest genuine local-model attempt (${esc(live.run_id)}, ${esc(live.crewai_model)}) ended ${esc(live.crewai_status)}${live.crewai_status === "FAILED" ? " – Ollama's 4,096-token context window truncated the prompt" : ""}.</span>` : ""}</p>
    <div class="agent-tabs">${v.agents.map((a, i) => `<button class="doc-tab ${i === S.agentTab ? "active" : ""}" data-i="${i}">${esc(a.title)} · ${esc(a.framework)}</button>`).join("")}</div>
    ${agentReport(v.agents[S.agentTab])}`;
  document.querySelectorAll(".agent-tabs button").forEach(b => b.addEventListener("click", () => { S.agentTab = +b.dataset.i; renderDetails(); }));
}

// ------------------------------------------------------------------ start
renderChips(null);
renderTimeline(null);
loadStatus();
loadSamples();
route();
