"use strict";
/* ClaimSleuth front end. Everything shown comes from the backend (PDF extraction, the investigation run, the
   deterministic adjudicator, the MCP server's log). Claim and PDF text is untrusted and is always escaped. */

const $ = id => document.getElementById(id);
const S = { draft: null, run: null, view: null, bb: null, trace: null, poll: null, docTab: 0, status: null, open: {}, runKey: null, reliability: null };

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
const RUN_STATUS = { COMPLETED: ["done", "ok", "full investigation succeeded"], INCOMPLETE: ["warn", "warn", "a required agent review failed"],
  "SUPPORTING RECORDS UNAVAILABLE": ["warn", "warn", "both agent reviews finished; their tool calls could not be confirmed"],
  BLOCKED: ["failed", "bad", "the agent reviews could not begin"], ESCALATED: ["warn", "warn", "all reviews finished; human review required"] };
const AGENT_NAMES = { evidence_agent: "Evidence Agent", investigator_agent: "Investigator Agent", forensic_agent: "Forensic Agent",
  evidence: "Evidence", investigation: "Investigator", forensics: "Forensic" };
const statusCls = s => s === "COMPLETED" ? "ok" : s === "BLOCKED" || s === "TIMED_OUT" ? "warn" : "bad";
const retryPill = a => { const r = a.retry || a.result; return r ? ` <span class="pill ${r === "RECOVERED AFTER RETRY" ? "ok" : "bad"}">${esc(r)}</span>` : ""; };
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
// An expandable section stays as the reader left it when its contents are rendered again.
function diag(key, title, body, byDefault = false) {
  return `<details class="diag" data-key="${esc(key)}" ${S.open[key] ?? byDefault ? "open" : ""}><summary>${title}</summary><div class="diag-body">${body}</div></details>`;
}
document.addEventListener("toggle", e => { if (e.target.dataset && e.target.dataset.key) S.open[e.target.dataset.key] = e.target.open; }, true);
const stats = items => `<dl class="stats">${items.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join("")}</dl>`;
const counted = (title, n) => `${title} <span class="muted">(${n})</span>`;
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
    const isLive = st.execution_mode === "LIVE";
    let label = isLive ? `LIVE · Gemini ${st.gemini_model}`
      : st.crew_mode === "ollama-local" ? `EXPERIMENTAL · ollama-local (${st.crew_model})` : "OFFLINE · test doubles";
    if (st.services.crewai !== "RUNNING") label += " · CrewAI service not running";
    $("mode-pill").textContent = `Mode: ${label}`;
    $("mode-pill").classList.toggle("live", isLive);
    $("mode-pill").title = isLive
      ? `All three agents use genuine Gemini inference (${st.gemini_model}, provider ${st.provider}). API key configured: ${st.gemini_key_configured ? "yes" : "NO"}. The key is never shown.`
      : `Evidence and Investigator Agents: offline test double. Forensic Agent: ${st.crew_mode || "unknown"}. Test doubles are labelled stand-ins, not LLM reasoning.`;
    if (st.crew_mode_mismatch) {
      message(`LIVE mode, but the CrewAI service is running in mode <b>${esc(st.crew_mode)}</b>, not Gemini. LIVE runs are refused until it is restarted in Gemini mode.`, "warn");
    } else if (st.services.mcp !== "RUNNING" || st.services.crewai !== "RUNNING") {
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
// One key for one click, kept until the server answers: a retry of the same click cannot start a second, paid run.
const newKey = () => (window.crypto && crypto.randomUUID ? crypto.randomUUID() : `k-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`);
async function startRun(body) {
  S.runKey = S.runKey || newKey();
  const started = await api("/api/runs", { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": S.runKey }, body: JSON.stringify(body) });
  S.runKey = null;
  return started;
}
$("run-btn").addEventListener("click", async () => {
  if (!S.draft) return;
  $("run-btn").disabled = true;
  message("");
  try {
    const r = await startRun({ upload_id: S.draft.upload_id });
    S.run = r.run_id;
    setStatus("INVESTIGATING", "running");
    renderTimeline([{ label: "Document extraction", state: "done" }, ...["Evidence Agent", "Investigator Agent", "Forensic Agent", "Final adjudication"].map(l => ({ label: l, state: "waiting" }))]);
    clearInterval(S.poll);
    S.poll = setInterval(pollJob, 1000);
  } catch (e) { message(esc(e.message), "bad"); $("run-btn").disabled = !S.draft.can_submit; }
});
async function pollJob() {
  let job;
  try { job = await api(`/api/jobs/${encodeURIComponent(S.run)}`); }
  catch (e) {                                  // the server may be restarting; only an answer that it does not know the run ends this
    if (e.message !== "unknown job") return;
    clearInterval(S.poll);
    setStatus("UNKNOWN", "failed");
    message("This run is not known to the application.", "bad");
    $("run-btn").disabled = false;
    return;
  }
  if (job.stages.length) renderTimeline(job.stages);
  if (job.state === "running") return;
  clearInterval(S.poll);
  loadReliability();
  if (job.state === "failed") { setStatus("FAILED", "failed"); message(`The investigation could not finish: ${esc(job.error)}`, "bad"); $("run-btn").disabled = false; return; }
  if (job.state !== "completed") {             // nothing to show: no report exists, and none is invented
    setStatus("INTERRUPTED", "failed");
    message(`<b>INVESTIGATION INTERRUPTED</b> – run ${esc(S.run)} stopped at stage <b>${esc(STAGE_LABEL[job.stage] || job.stage)}</b> and has no report. It was not approved. Open Unfinished Runs in Advanced Diagnostics to start a new investigation.`, "bad");
    $("run-btn").disabled = false;
    return;
  }
  history.replaceState(null, "", `#investigation?run=${encodeURIComponent(S.run)}`);
  await openRun(S.run);
  $("run-btn").disabled = false;
  $("results-banner").scrollIntoView();
}
function renderTimeline(stages) {
  if (!stages) {
    const ready = S.draft && S.draft.can_submit;
    stages = ["Document extraction", "Evidence Agent", "Investigator Agent", "Forensic Agent", "Final adjudication"]
      .map((l, i) => ({ label: l, state: i === 0 && ready ? "done" : "waiting" }));
  }
  const ICON = { done: "✓", failed: "✗", blocked: "⊘", running: '<span class="spin"></span>', waiting: "○" };
  const TEXT = { done: "completed", failed: "failed", blocked: "blocked", running: "running…", waiting: "waiting" };
  $("timeline").innerHTML = stages.map((s, i) => `<li class="${s.state}"><span class="step-icon">${ICON[s.state] || "○"}</span>
    <span>${i + 1}. ${esc(s.label)}</span><span class="step-state">${TEXT[s.state] || s.state}</span></li>`).join("");
}

// ------------------------------------------------------------------ report
function resetReport() {
  S.view = S.bb = S.trace = S.runKey = null;
  $("results-banner").hidden = true;
  $("alerts").innerHTML = "";
  $("advanced-flags").innerHTML = "";
  $("report").innerHTML = '<p class="muted small">Run an investigation to see its findings.</p>';
  $("evidence").innerHTML = '<p class="muted small">No evidence yet. Run an investigation.</p>';
  $("hitl").innerHTML = "";
  $("audit").innerHTML = '<p class="muted small">No investigation yet.</p>';
  $("audit-count").textContent = "";
  $("agents-body").innerHTML = '<p class="muted small">Run an investigation to see the three agent reports.</p>';
  $("overview-body").innerHTML = '<p class="muted small">Run an investigation to see how it executed.</p>';
  $("coordination-body").innerHTML = '<p class="muted small">Run an investigation to see its blackboard.</p>';
  $("details-body").innerHTML = '<p class="muted small">Run an investigation to see its execution record.</p>';
  $("bb-body").innerHTML = '<p class="muted small">Run an investigation to see its blackboard.</p>';
  $("trace-body").innerHTML = '<p class="muted small">Run an investigation to see its execution trace.</p>';
  setStatus("READY");
  if (location.hash.includes("run=")) history.replaceState(null, "", "#investigation");
}
async function openRun(runId) {
  try { S.view = await api(`/api/runs/${encodeURIComponent(runId)}`); }
  catch (e) { message(`Could not load the report: ${esc(e.message)}`, "bad"); return; }
  S.run = runId;
  renderReport();
  loadBlackboard(runId);
  loadTrace(runId);
  loadSecurity();
  loadReliability();
}
const check = (v, name) => (v.final.rule_checks || []).find(c => c.name === name);
const REVIEW_EFFECT = { REPEATED: "repeated, no effect", REFUSED: "refused" };
const STAGE_LABEL = { started: "started", agents_running: "agents running", report_published: "report published", sealed: "blackboard sealed", decided_not_stored: "decided, report not stored" };
// What stands: the final action, else the latest request for information. A repeated or refused action changes nothing.
const standingReview = v => v.final_review || [...v.human_reviews].reverse().find(r => !REVIEW_EFFECT[r.effect]) || null;
function mcpResults(v, tool) { return v.mcp.calls.filter(c => c.tool === tool && c.status === "ok" && c.returned).map(c => c.returned); }

function renderReport() {
  const v = S.view, f = v.final, b = v.baseline, c = v.claim || {};
  const badge = v.status_badge || v.run_status;
  setStatus(badge, (RUN_STATUS[badge] || [])[0]);
  renderChips(c);
  renderTimeline([{ label: "Document extraction", state: "done" },
    ...v.agents.map(a => ({ label: a.title, state: a.status === "COMPLETED" ? "done" : a.status === "BLOCKED" ? "blocked" : "failed" })),
    { label: "Final adjudication", state: f ? "done" : "failed" }]);
  if (!S.draft || S.draft.claim?.claim_id !== v.claim_id) {
    $("dz-file").textContent = v.origin && v.origin.filename ? v.origin.filename : v.claim_id;
    $("preview").innerHTML = `<div><b>${esc(c.device_model)}</b> · serial ${esc(c.serial_number || "not stated on the claim form")}</div>`;
  }
  renderDetails();
  renderAgents();
  renderOverview();
  renderAlerts();
  if (!f) { message("This run has no final report.", "bad"); return; }
  const rv = v.reviewer || {};
  if (S.banner) message("");                   // the previous run's banner, not a service warning
  S.banner = !!rv.title;
  if (rv.title) {
    message(`<b>${esc(rv.title)}</b> – ${esc(rv.headline)} ${esc(rv.detail)}
      ${rv.records_only ? "" : (f.incomplete_reasons || []).map(esc).join(" · ")}${v.outcome.explanation ? `<br>${esc(v.outcome.explanation)}` : ""}`, "warn");
  }

  // results banner (prototype: Status, Coverage, Settlement Payout, Human Approval)
  const cov = check(v, "coverage_window");
  const latest = standingReview(v);
  const card = (label, value, cls, note) => `<div class="result-card"><div class="result-card-label">${label}</div>
    <div class="result-card-value ${cls || ""}">${value}</div><div class="result-card-note">${note}</div></div>`;
  $("results-grid").innerHTML =
    card("Automated decision", esc(decisionText(f.final_decision)), (DECISION[f.final_decision] || [])[1], `${esc(f.final_decision)} · decided by ${esc(DECIDED_BY[f.decided_by] || f.decided_by)}${rv.banner ? ` · <b>${esc(rv.banner)}</b>` : ""}`) +
    card("Coverage", cov ? { PASS: "Within warranty", FAIL: "Outside warranty", NOT_EVALUABLE: "Not evaluable" }[cov.outcome] : "—", "", esc(cov ? cov.detail : "")) +
    card("Settlement payout", f.payout ? money(f.payout.payout, f.payout.currency) : "None", f.payout ? "v-approve" : "", f.payout ? `computed by the deterministic rules${f.requires_human_approval && (!latest || latest.action === "INFO_REQUESTED") ? " · on hold until a person approves" : ""}` : "no payout without an approval") +
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
    <div class="block"><div class="block-title">Why this decision</div>${list(f.rationale, "")}
      <p class="muted small">The three agents only recommend. The decision comes from the deterministic adjudicator, which re-checks the policy rules and the agents' evidence; it never takes a vote.</p></div>
    ${f.payout ? `<div class="block"><div class="block-title">Settlement breakdown</div><div class="math-grid">
      <div class="math-cell"><div class="math-label">Repair cost</div><div class="math-val">${money(f.payout.repair_cost, f.payout.currency)}</div></div>
      <div class="math-cell"><div class="math-label">Deductible</div><div class="math-val">-${money(f.payout.deductible, f.payout.currency)}</div></div>
      <div class="math-cell"><div class="math-label">Depreciation</div><div class="math-val">-${money(f.payout.depreciation_amount, f.payout.currency)}</div></div>
      <div class="math-cell"><div class="math-label">Approved payout</div><div class="math-val approved">${money(f.payout.payout, f.payout.currency)}</div></div></div>
      <p class="muted small" style="margin-top:6px">${esc(f.payout.explanation)}</p></div>` : ""}
    ${concernsBlock(rv)}
    ${integrityBlock(v.integrity)}
    ${evidenceIntegrityBlock(v.evidence_integrity)}
    <div class="block"><div class="block-title">Suspicious inconsistencies</div>${list(issues, "None found.")}
      ${abuse.length ? `<p class="muted small">Abusive language was noted in the log; it does not affect the decision.</p>` : ""}</div>
    <div class="block"><div class="block-title">Missing documents or evidence</div>${list(b.missing_information, "Nothing missing.")}</div>
    <div class="block"><div class="block-title">Claim summary</div><dl class="kv">
      <dt>Claim ID</dt><dd>${esc(c.claim_id)}</dd><dt>Claimant</dt><dd>${esc(c.claimant_name)} (${esc(c.claimant_id)})</dd>
      <dt>Product</dt><dd>${esc(c.device_model)}</dd><dt>Serial number</dt><dd>${esc(c.serial_number || "not stated on the claim form")}</dd>
      <dt>Incident</dt><dd>${esc(c.incident_date)} – ${esc(c.reported_issue)}</dd>
      ${c.claimant_statement ? `<dt>Statement</dt><dd>${esc(c.claimant_statement)}</dd>` : ""}</dl></div>
    <div class="block"><div class="block-title">Warranty policy</div>
      ${policy ? `<p class="small">${esc(policy.name)} (${esc(policy.policy_id)}): ${esc(policy.terms.duration_months)} months, deductible ${money(policy.terms.deductible)}, limit ${money(policy.terms.max_coverage_limit)}. Exclusions: ${esc(policy.terms.exclusions.join("; "))}.</p>` : '<p class="muted small">Policy not retrieved.</p>'}
      <ul class="check-list">${policyChecks.map(r => `<li>${mark(r.outcome === "PASS" ? true : r.outcome === "FAIL" ? false : null)}<span>${esc(r.detail)}</span></li>`).join("")}</ul></div>
    <div class="block"><div class="block-title">Claim history</div>
      ${Object.values(records).length ? `<ul class="plain">${Object.values(records).map(r => `<li>${esc(r.claim_id)} – ${esc(r.device_model)}, claimant ${esc(r.claimant_id)}, ${esc(r.claim_date)}, ${esc(r.outcome)}</li>`).join("")}</ul>`
        : '<p class="small">No earlier claims found for this device or claimant.</p>'}
      ${hist ? `<p class="small">${mark(hist.outcome === "PASS" ? true : hist.outcome === "FAIL" ? false : null)} ${esc(hist.detail)}</p>` : ""}
      <p class="muted small">Records come from the claim-history database through the claim_history_lookup tool.</p></div>`;

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
    <span>${a.kind === "human" ? `<b>Human review: ${esc(REVIEW[a.action] || a.action)}</b> by ${esc(a.reviewer)}${REVIEW_EFFECT[a.effect] ? ` <span class="pill warn">${REVIEW_EFFECT[a.effect]}</span>` : ""} <span class="d">(automated decision ${esc(a.automated_decision)} unchanged)</span>` : esc(a.text)}
    ${a.detail && a.kind === "human" ? `<div class="d">“${esc(a.detail)}”</div>` : ""}</span></div>`).join("");
  $("audit-count").textContent = `(${v.audit_trail.length})`;
}

// What must never wait behind an expandable section: security findings, failures and blackboard problems of this run.
function bbProblems(bb) {
  if (!bb || !bb.enabled) return [];
  const auth = bb.authentication || {}, late = bb.late || [], refused = bb.security_events || [];
  return (bb.monitoring_failures || []).map(f => `[${f.where}] ${f.error} (${f.environment} pid ${f.pid})`)
    .concat(bb.findings || []).concat(bb.error ? [bb.error] : [])
    .concat(Object.keys(auth).some(k => k !== "valid") ? [`authentication problems: ${JSON.stringify(auth)}`] : [])
    .concat(bb.decision_matches_report === false ? ["the recorded decision differs from the report"] : [])
    .concat(bb.registered && !bb.decision && S.view && S.view.final ? ["the decision could not be recorded on the blackboard"] : [])
    .concat(late.length ? [`${late.length} late entries after the adjudicator's inputs were fixed`] : [])
    .concat(refused.length ? [`${refused.length} refused read(s) of shared context`] : []);
}
function renderAlerts() {
  const v = S.view;
  if (!v) return;
  const failures = (v.outcome && v.outcome.failures) || [], bb = bbProblems(S.bb), collection = (S.trace && S.trace.collection_errors) || [];
  const alerts = [];
  if (v.integrity.status !== "Clean") alerts.push([v.integrity.status === "Suspicious" ? "warn" : "bad", `<b>Security finding:</b> ${esc(v.integrity.status)}. See Security &amp; Document Integrity in the findings below.`]);
  if (v.evidence_integrity.length) alerts.push(["bad", `<b>Evidence integrity:</b> ${v.evidence_integrity.length} refused read(s) of shared context in this run.`]);
  if (failures.length) alerts.push(["bad", `<b>Provider or agent failure:</b> ${failures.length} recorded. See Execution Monitoring in Advanced Diagnostics.`]);
  (v.provider_retries || []).filter(r => r.outcome === "recovered").forEach(r => alerts.push(["", `<b>${esc(r.result)}:</b> a request by the ${esc(AGENT_NAMES[r.agent] || r.agent)} failed with HTTP ${esc(r.http_status)} (<code>${esc(r.error_code)}</code>) and its one automatic retry succeeded. See Execution Monitoring in Advanced Diagnostics.`]));
  if (v.mcp && v.mcp.status === "FAILED") alerts.push(["warn", "<b>MCP records unavailable:</b> the agents' tool calls could not be confirmed, so a human must review this run."]);
  if ((v.reviewer || {}).records_missing) alerts.push(["warn", "<b>MCP records missing:</b> the server's log holds none of the tool calls the agents recorded, although the server reported no fault."]);
  const ledger = v.execution.request_accounting;
  if (ledger && !ledger.match) alerts.push(["warn", `<b>Request accounting:</b> the ledger holds ${ledger.ledger} request(s) for this run, the agents recorded ${ledger.recorded}.`]);
  if (v.reviews_unreadable) alerts.push(["warn", `<b>Human review record:</b> ${v.reviews_unreadable} line(s) could not be read. They are not guessed at.`]);
  bb.forEach(p => alerts.push(["bad", `<b>Blackboard:</b> ${esc(p)}`]));
  if (collection.length) alerts.push(["warn", `<b>Monitoring:</b> ${collection.length} collection error(s). The investigation is not affected.`]);
  $("alerts").innerHTML = alerts.map(([cls, html]) => `<div class="notice ${cls}">${html}</div>`).join("");
  const problems = failures.length + bb.length + collection.length;
  $("advanced-flags").innerHTML = problems ? `<span class="pill bad">${problems} recorded problem${problems === 1 ? "" : "s"}</span>` : "";
}

// Agent concerns for the reviewer. Verified and unverified stay apart: an unverified concern was not used in the decision,
// and the reviewer still needs to read what the agent saw.
function concernCard(c, verified) {
  return `<div class="concern ${verified ? "verified" : "unverified"}">
      <div class="concern-head"><span class="pill ${verified ? "ok" : "warn"}">${verified ? "Verified" : "Unverified"}</span>
        <b>${esc(c.agent)}</b> <span class="pill neutral">${esc(c.severity)}</span> <span class="muted">${esc(c.category)}</span></div>
      <p class="small concern-text">${esc(c.description)}</p>
      ${c.why_unverified ? `<p class="small"><b>Why it could not be verified:</b> ${esc(c.why_unverified)}</p>` : ""}
      <p class="small"><b>Effect on the decision:</b> ${esc(c.effect)}</p></div>`;
}
function concernsBlock(rv) {
  const unverified = rv.unverified_concerns || [], verified = rv.verified_concerns || [], other = rv.other_unverified || [];
  if (!unverified.length && !verified.length && !other.length) return "";
  return `<div class="block"><div class="block-title">Agent concerns for the reviewer</div>
    ${unverified.map(c => concernCard(c, false)).join("")}${verified.map(c => concernCard(c, true)).join("")}
    ${other.length ? `<p class="small"><b>Other items that could not be verified</b></p><ul class="plain">${other.map(o => `<li>${esc(o.agent)}: ${esc(o.text)}</li>`).join("")}</ul>` : ""}</div>`;
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

// Evidence Integrity of one run: the shared-context reads the backend refused in this run. The events are as recorded,
// so a controlled test's own run is labelled as one.
function evidenceIntegrityBlock(events) {
  if (!events.length) return "";
  const kind = events.some(e => e.test_mode) ? "Controlled adversarial test" : "Real claim investigation";
  return `<div class="block integrity integ-detected"><div class="block-title">Evidence Integrity</div>
    <div class="integrity-status integ-detected">Shared context refused <span class="pill warn">${esc(kind)}</span></div>
    ${traceTable(["Attack type", "Refused reader", "Entry", "Payload", "Signature", "Consumption", "Escalation"],
      events.map(e => [`<code>${esc(e.attack_type)}</code>`, esc(AGENT_NAMES[e.consumer] || e.consumer || "controller"), esc(e.affected_entry_reference || "—"),
        esc(e.verification_result.payload_integrity), esc(e.verification_result.signature), e.downstream_blocked ? "blocked" : "not blocked", esc(e.escalation_status)]))}</div>`;
}

function renderReview() {
  const v = S.view, f = v.final;
  const latest = standingReview(v);
  if (!f.requires_human_approval) {
    $("hitl").innerHTML = `<div class="hitl-title">Human Approval</div><p class="small muted">Not required: the automated decision is
      ${esc(f.final_decision)}${f.payout ? ` with a payout below the ${threshold()} sign-off threshold` : ""}.</p>`;
    return;
  }
  $("hitl").innerHTML = `<div class="hitl-title">Human Approval</div>
    <p class="small muted">Required for payouts of ${threshold()} or more, rejections and escalations. Your action is recorded separately; the automated decision is never changed.</p>
    ${(v.reviewer || {}).human_reasons && v.reviewer.human_reasons.length ? `<p class="small"><b>Why this run needs you</b></p>${list(v.reviewer.human_reasons, "")}` : ""}
    <div class="decision-pair"><div>Automated decision<b>${esc(f.final_decision)}</b></div>
      <div>Human review<b>${latest ? `${esc(REVIEW[latest.action])} – ${esc(latest.reviewer)}` : "Pending"}</b></div></div>
    <div class="hitl-fields"><input id="reviewer" placeholder="Reviewer name" maxlength="80" value="${esc(localStorage.getItem("cs-reviewer") || "")}">
      <textarea id="review-notes" rows="2" placeholder="Reviewer notes" maxlength="2000"></textarea></div>
    <div class="hitl-actions"><button class="btn-action btn-approve" data-a="APPROVED">Approve &amp; Close</button>
      <button class="btn-action btn-reject" data-a="REJECTED">Reject Claim</button>
      <button class="btn-action btn-info" data-a="INFO_REQUESTED">Request More Information</button></div>
    <div id="review-msg" class="small" style="margin-top:6px"></div>`;
  document.querySelectorAll(".hitl-actions button").forEach(btn => btn.addEventListener("click", async () => {
    const reviewer = $("reviewer").value.trim();
    if (!reviewer) { $("review-msg").innerHTML = '<span class="bad">Enter the reviewer\'s name first.</span>'; return; }
    try { localStorage.setItem("cs-reviewer", reviewer); } catch (_) { /* optional */ }
    try {
      await post(`/api/runs/${encodeURIComponent(S.run)}/review`, { action: btn.dataset.a, reviewer, notes: $("review-notes").value });
      await openRun(S.run);
    } catch (e) {
      await openRun(S.run);                    // a refused action is on the audit trail too
      if ($("review-msg")) $("review-msg").innerHTML = `<span class="bad">${esc(e.message)}</span>`;
    }
  }));
}

// ------------------------------------------------------------------ agent reports
function agentReport(a) {
  const o = a.output || {};
  const kind = a.provider === "google-gemini" ? "Gemini inference" : "local model inference";
  const model = a.model === "test-double" ? '<span class="tag td">test-double · not LLM reasoning</span>'
    : a.model ? `<span class="tag live">${esc(a.model)}${a.genuine_inference ? " · " + kind : ""}</span>` : "";
  const mcp = a.tool_calls.filter(t => t.protocol === "mcp");
  const cites = f => (f.citations || []).map(c => `${DOC_NAMES[c.doc_id] || c.doc_id} line ${c.line} ${c.verified ? "✓" : "✗ not in document"}`).join("; ");
  const checks = (title, cs) => cs && cs.length ? `<p class="small"><b>${title}</b></p><ul class="check-list">${cs.map(x =>
    `<li>${mark(x.result === "CONSISTENT" ? true : x.result === "INCONSISTENT" ? false : null)}<span>${esc(x.check)}: ${esc(x.detail)}</span></li>`).join("")}</ul>` : "";
  const findings = o.findings || [];
  const details = `${findings.length ? `<ul class="plain">${findings.map(f => `<li><b>${esc(f.severity)}</b> ${esc(f.description)}${cites(f) ? ` <span class="muted">(${esc(cites(f))})</span>` : ""}</li>`).join("")}</ul>` : '<p class="muted small">No findings.</p>'}
      ${a.key === "investigator_agent" ? `<p class="small"><b>Policy eligibility:</b> ${esc(o.policy_eligibility)} – ${esc(o.policy_eligibility_reason)}</p>` : ""}
      ${checks("Serial-number checks", o.serial_number_checks)}${checks("Chronology checks", o.chronology_checks)}
      <p class="small"><b>Missing information:</b> ${esc((o.missing_information || []).join("; ") || "none")}</p>`;
  return `<article class="agent-report"><h3>${esc(a.title)}</h3>
    <p class="small muted">${esc(a.framework)} · ${esc(a.responsibility)}</p>
    <p class="small"><span class="pill ${statusCls(a.status)}">${esc(a.status)}</span>${retryPill(a)} ${model}
      <span class="muted">${a.latency_ms ? `${(a.latency_ms / 1000).toFixed(2)} s` : ""}${mcp.length ? ` · MCP tools: ${esc([...new Set(mcp.map(t => t.tool))].join(", "))}` : ""}</span></p>
    ${a.errors.length ? `<div class="notice bad small">${list(a.errors)}</div>` : ""}
    ${a.output ? `<p class="small"><b>Recommendation:</b> ${decisionPill(a.recommendation)} <span class="muted">(recommendation only)</span></p>
      <p class="small">${esc(o.reasoning_summary)}</p>
      ${diag(`agent-${a.key}`, counted("Findings and checks", `${findings.length} finding${findings.length === 1 ? "" : "s"}`), details)}` : ""}</article>`;
}
function renderAgents() {
  $("agents-body").innerHTML = S.view.agents.map(agentReport).join("");
}
function modeTag(x) {
  const allDone = x.agents.every(a => a.status === "COMPLETED");
  return x.mode === "LIVE" ? (allDone ? '<span class="tag live">LIVE · genuine Gemini inference for all three agents</span>'
      : '<span class="tag live">LIVE · Gemini</span> <span class="pill bad">not every agent completed – see the agent errors</span>')
    : x.mode === "OFFLINE" ? '<span class="tag td">OFFLINE · labelled test doubles, not LLM reasoning</span>'
    : '<span class="tag td">MIXED · not every agent used a live model</span>';
}
function renderDetails() {
  const v = S.view;
  const live = S.status && S.status.latest_live_attempt;
  const x = v.execution;
  const ollamaNote = live && live.run_id !== v.run_id && (live.crewai_model || "").includes(":") && live.crewai_status === "FAILED"
    ? ` <span class="muted">An earlier local-model attempt (${esc(live.run_id)}, ${esc(live.crewai_model)}) ended FAILED – Ollama's 4,096-token context window truncated the prompt.</span>` : "";
  $("details-body").innerHTML = `<p class="small"><b>Execution mode of this investigation:</b> ${modeTag(x)}${x.requested_live && x.mode !== "LIVE" ? ' <span class="pill bad">LIVE was requested; see the agent errors</span>' : ""}${ollamaNote}</p>
    <table class="exec-table small"><thead><tr><th>Agent</th><th>Status</th><th>Model · provider</th><th>Model calls</th><th>MCP tools completed</th></tr></thead>
    <tbody>${x.agents.map(a => `<tr><td>${esc(a.agent)} <span class="muted">(${esc(a.framework)})</span></td>
      <td><span class="pill ${statusCls(a.status)}">${esc(a.status)}</span>${retryPill(a)}</td>
      <td>${esc(a.model || "none")}${a.provider ? ` · ${esc(a.provider)}` : ""}</td><td>${a.llm_calls ?? "–"}</td>
      <td>${esc(a.mcp_ok.join(", ") || "none")}${a.mcp_errors ? ` · ${a.mcp_errors} failed` : ""}</td></tr>`).join("")}</tbody></table>
    <p class="small muted">${x.gemini_requests_total ? `Gemini requests: ${x.gemini_requests_total} (${Object.entries(x.gemini_requests).map(([k, n]) => `${esc(k)} ${n}`).join(", ")}) · ` : ""}MCP server log: ${x.mcp_server_calls} call(s) for this run · A2A: ${x.a2a.request_sent ? "FORENSIC_REQUEST sent" : "no request sent"}, ${x.a2a.response ? esc(x.a2a.response) + " received" : "no response"} ·
      Final: ${esc(x.final_decision || "none")}${x.decided_by ? ` by ${esc(x.decided_by)}` : ""}${x.requires_human_approval ? " · human approval required" : ""}</p>`;
}

// ------------------------------------------------------------------ execution overview
// A summary of what the run and its trace recorded. The records themselves are in Advanced Diagnostics.
const taskCls = s => s === "COMPLETED" ? "ok" : ["RUNNING", "PENDING", "BLOCKED", "TIMED_OUT"].includes(s) ? "warn" : "bad";
const taskFlow = tasks => `<ol class="task-flow">${tasks.map((t, i) => `<li>${i && t.task_id === "forensics" ? '<span class="muted" title="runs in parallel with the Investigator">∥</span>' : i ? '<span class="muted">→</span>' : ""}
  <span class="pill ${taskCls(t.status)}">${esc(t.task_id)}: ${esc(t.status)}</span></li>`).join("")}</ol>`;
function renderOverview() {
  const v = S.view;
  if (!v) return;
  const x = v.execution, tr = S.trace, m = (tr && tr.available && tr.metrics) || {}, o = v.outcome || {};
  const badge = v.status_badge || v.run_status;
  const [, cls, note] = RUN_STATUS[badge] || ["", "neutral", ""];
  const doubles = x.agents.reduce((n, a) => n + (a.genuine_inference ? 0 : a.llm_calls || 0), 0);
  const retries = v.provider_retries || [], overlap = m.parallel_overlap_ms;
  $("overview-body").innerHTML = `<dl class="kv small">
      <dt>Investigation</dt><dd><span class="pill ${cls}">${esc(badge)}</span> <span class="muted">${esc(note)}</span></dd>
      <dt>Execution mode</dt><dd>${modeTag(x)}${x.requested_live && x.mode !== "LIVE" ? ' <span class="pill bad">LIVE was requested</span>' : ""}</dd>
      <dt>Agents</dt><dd>${x.agents.map(a => `<span class="pill ${statusCls(a.status)}">${esc(a.agent)}: ${esc(a.status)}</span>${retryPill(a)}`).join(" ")}</dd>
      <dt>Duration</dt><dd>${msText(m.total_ms)}</dd>
      <dt>Model requests</dt><dd>${x.gemini_requests_total} to Gemini${doubles ? ` <span class="muted">· ${doubles} test-double calls, no model involved</span>` : ""}</dd>
      <dt>MCP</dt><dd>${x.mcp_server_calls} calls in the server log${m.mcp_latency ? ` <span class="muted">· median ${msText(m.mcp_latency.median_ms)}</span>` : ""}</dd>
      <dt>A2A</dt><dd>${x.a2a.request_sent ? "FORENSIC_REQUEST sent" : "no request sent"}, ${x.a2a.response ? `${esc(x.a2a.response)} received` : "no response"}${m.a2a_latency ? ` <span class="muted">· ${msText(m.a2a_latency.median_ms)}</span>` : ""}</dd>
      ${x.request_accounting ? `<dt>Request accounting</dt><dd><span class="pill ${x.request_accounting.match ? "ok" : "warn"}">${x.request_accounting.match ? "ledger matches the agents' records" : "ledger and agents differ"}</span> <span class="muted">${x.request_accounting.ledger} in the ledger, ${x.request_accounting.recorded} recorded</span></dd>` : ""}
      <dt>Parallel execution</dt><dd>${overlap === undefined || overlap === null ? '<span class="muted">not recorded</span>' : overlap > 0
        ? `Investigator and Forensic Agents overlapped for ${msText(overlap)}` : "the Investigator and Forensic Agents did not overlap"}</dd>
      ${retries.length ? `<dt>Automatic retries</dt><dd>${retries.map(r => `${retryPill(r)} ${esc(AGENT_NAMES[r.agent] || r.agent)} after HTTP ${esc(r.http_status)}`).join("; ")}</dd>` : ""}
      <dt>Manager tasks</dt><dd>${m.tasks ? taskFlow(m.tasks) : '<span class="muted">not recorded</span>'}</dd></dl>
    ${tr && !tr.available ? `<p class="small muted">${esc(tr.reason)}</p>` : ""}`;
}

// ------------------------------------------------------------------ reliability (stored runs of this installation)
// Counted by the backend. Runs from scripts and demonstrations are listed, marked, and left out of the totals.
async function loadReliability() {
  try { S.reliability = await api("/api/reliability"); } catch (_) { return; }
  renderReliability();
}
function renderReliability() {
  const { totals: t, runs } = S.reliability;
  const part = (n, label, cls) => n ? `<span class="${cls || ""}">${n} ${label}</span>` : "";
  const plural = n => n === 1 ? "" : "s";
  const parts = [part(t.completed, "completed"), part(t.escalated, "escalated"), part(t.records_unavailable, "supporting records unavailable"), part(t.incomplete, "incomplete"), part(t.blocked, "blocked"),
    part(t.interrupted, "interrupted", "bad"), part(t.failed, "failed", "bad"), part(t.damaged, "damaged", "bad"), part(t.recovered, "recovered"),
    part(t.provider_failures, `unrecovered provider failure${plural(t.provider_failures)}`), part(t.provider_recovered, "recovered after retry"),
    part(t.failures_not_classified, `older failed run${plural(t.failures_not_classified)} not classified`), part(t.duplicates_prevented, "duplicate requests prevented"),
    part(t.accounting_warnings, "accounting warnings", "bad")].filter(Boolean);
  const scope = t.stored ? ` (${t.stored}${t.offline_tests ? `, including ${t.offline_tests} offline test run${plural(t.offline_tests)}` : ""})` : "";
  $("reliability-line").innerHTML = parts.length ? `<b>Stored investigations${scope}:</b> ${parts.join(" · ")}` : "";
  $("unfinished-count").textContent = runs.length ? `(${runs.length})` : "";
  $("unfinished-body").innerHTML = traceTable(["Run", "Claim", "State", "Last stage", "Started", "Origin", ""], runs.map(r => [esc(r.run_id), esc(r.claim_id || "—"),
    `<span class="pill ${r.state === "RECOVERED" ? "ok" : "bad"}">${esc(r.state)}</span>${r.recovered_by ? ` <span class="muted">by ${esc(r.recovered_by)}</span>` : ""}`,
    esc(STAGE_LABEL[r.stage] || r.stage), r.submitted_at ? esc(new Date(r.submitted_at).toLocaleString()) : "—", r.actual ? "dashboard" : "script or demonstration",
    r.state !== "RECOVERED" && r.claim_id ? `<button class="btn-secondary recover-run" data-run="${esc(r.run_id)}" data-claim="${esc(r.claim_id)}">Start new investigation</button>` : ""]))
    + '<p class="small muted">Nothing here is resumed or rerun on its own, and no report of an unfinished run is reused. A new investigation has its own run id and its own decision, and in LIVE mode it uses Gemini requests.</p>';
  document.querySelectorAll(".recover-run").forEach(b => b.addEventListener("click", async () => {
    resetReport();
    try {
      const r = await startRun({ claim_id: b.dataset.claim, recovers: b.dataset.run });
      S.run = r.run_id;
      setStatus("INVESTIGATING", "running");
      $("run-btn").disabled = true;
      clearInterval(S.poll);
      S.poll = setInterval(pollJob, 1000);
    } catch (e) { message(esc(e.message), "bad"); }
  }));
}

// ------------------------------------------------------------------ blackboard & agent coordination
// Summary of the run's blackboard. Publication comes from the trace, consumption from the signed receipts on the board.
function renderCoordination() {
  const bb = S.bb;
  if (!bb) return;
  if (!bb.enabled) { $("coordination-body").innerHTML = '<p class="small muted">The blackboard is disabled for this dashboard: the agents shared their reports directly.</p>'; return; }
  const problems = bbProblems(bb);
  const warnings = problems.length ? `<div class="notice bad small">${list(problems, "")}</div>` : "";
  if (!bb.registered) { $("coordination-body").innerHTML = `<p class="small">Run <b>${esc(bb.run_id)}</b> is not on the blackboard.</p>${warnings}`; return; }
  const auth = bb.authentication || {}, d = bb.decision;
  const authOk = Object.keys(auth).every(k => k === "valid");
  const published = {};
  ((S.trace && S.trace.events) || []).filter(e => e.event_type === "blackboard.publish").forEach(e => { published[e.metadata.producer] = e; });
  const stateCls = st => st === "COMPLETED" ? "ok" : st === "RUNNING" || st === "PENDING" ? "warn" : "bad";
  const rows = bb.agents.map(a => {
    const p = published[a.agent], readers = (bb.context || []).filter(c => c.source_section === `report:${a.agent}`);
    return [esc(AGENT_NAMES[a.agent] || a.agent), `<span class="pill ${stateCls(a.state)}">${esc(a.state)}</span>`,
      p ? `entry ${esc(p.metadata.run_seq)}` : '<span class="muted">not recorded</span>',
      readers.length ? readers.map(c => `<span class="${c.auth === "valid" ? "ok" : "bad"}">${c.auth === "valid" ? "✓" : "✗"}</span> ${esc(AGENT_NAMES[c.consumer] || c.consumer)}`).join(", ") : '<span class="muted">none</span>'];
  });
  $("coordination-body").innerHTML = `<dl class="kv small">
      <dt>Database</dt><dd><span class="pill ok">run registered</span> <span class="muted">${esc(String(bb.database).split(/[\\/]/).pop())}</span></dd>
      <dt>Entries</dt><dd>${esc(bb.entries)}</dd>
      <dt>Seal</dt><dd>${bb.sealed_at === null || bb.sealed_at === undefined ? '<span class="pill warn">not sealed</span>' : `<span class="pill ok">sealed</span> <span class="muted">at entry ${esc(bb.sealed_at)}</span>`}</dd>
      <dt>Authentication</dt><dd>${authOk ? '<span class="pill ok">all entries authenticated</span>' : '<span class="pill bad">authentication problems</span>'}</dd>
      <dt>Recorded decision</dt><dd>${d ? `${decisionPill(d.final_decision)} ${bb.decision_matches_report === true ? '<span class="pill ok">matches the report</span>' : bb.decision_matches_report === false ? '<span class="pill bad">differs from the report</span>' : ""}` : '<span class="muted">not recorded</span>'}</dd>
      ${(bb.tasks || []).length ? `<dt>Manager tasks</dt><dd>${taskFlow(bb.tasks)}</dd>` : ""}</dl>
    ${warnings}
    <p class="small"><b>Report publication and verified consumption</b></p>
    ${traceTable(["Report", "Agent state", "Published", "Read with a signed receipt by"], rows)}`;
}

// ------------------------------------------------------------------ blackboard monitoring (read-only)
async function loadBlackboard(runId) {
  let bb;
  try { bb = await api(`/api/runs/${encodeURIComponent(runId)}/blackboard`); }
  catch (e) { $("bb-body").innerHTML = `<p class="small bad">Blackboard view unavailable: ${esc(e.message)}</p>`; return; }
  if (S.run === runId) {
    S.bb = bb;
    renderBlackboard(bb);
    renderCoordination();
    renderAlerts();
  }
}
function renderBlackboard(bb) {
  if (!bb.enabled) {
    $("bb-body").innerHTML = '<p class="small muted">The blackboard is disabled for this dashboard (--no-blackboard or CLAIMSLEUTH_BLACKBOARD_DB=off).</p>';
    return;
  }
  const failures = (bb.monitoring_failures || []).map(f => `[${f.where}] ${f.error} (${f.environment} pid ${f.pid})`)
    .concat(bb.findings || []).concat(bb.error ? [bb.error] : []);
  const failureBlock = `<p class="small"><b>Monitoring failures and findings</b> ${failures.length ? `<span class="pill bad">${failures.length}</span>` : '<span class="pill ok">none</span>'}</p>${failures.length ? list(failures, "") : ""}`;
  if (!bb.registered) {
    $("bb-body").innerHTML = `<p class="small">Run <b>${esc(bb.run_id)}</b> is not on the blackboard (${esc(bb.database)}).</p>${failureBlock}`;
    return;
  }
  const auth = bb.authentication || {};
  const authOk = Object.keys(auth).every(k => k === "valid");
  const d = bb.decision;
  const stateCls = s => s === "COMPLETED" ? "ok" : s === "RUNNING" || s === "PENDING" ? "warn" : "bad";
  $("bb-body").innerHTML = `
    <dl class="kv small"><dt>Run</dt><dd>${esc(bb.run_id)}</dd><dt>Claim</dt><dd>${esc(bb.claim_id)}</dd>
      <dt>Database</dt><dd>${esc(bb.database)} · ${bb.entries} entries · sealed at entry ${esc(bb.sealed_at ?? "—")}</dd>
      <dt>Writers</dt><dd>${authOk ? '<span class="pill ok">all entries authenticated</span>' : `<span class="pill bad">authentication problems: ${esc(JSON.stringify(auth))}</span>`}</dd>
      <dt>Recorded decision</dt><dd>${d ? `${decisionPill(d.final_decision)} by ${esc(d.decided_by)} ${bb.decision_matches_report === true ? '<span class="pill ok">matches the report</span>' : bb.decision_matches_report === false ? '<span class="pill bad">differs from the report</span>' : ""}` : '<span class="muted">not recorded</span>'}</dd></dl>
    <p class="small"><b>Agents</b> ${bb.agents.map(a => `<span class="pill ${stateCls(a.state)}">${esc(a.agent)}: ${esc(a.state)}${a.late ? " (late)" : ""}</span>`).join(" ")}</p>
    ${(bb.tasks || []).length ? `<p class="small"><b>Manager tasks</b> ${bb.tasks.map(t => `<span class="pill ${stateCls(t.status)}">${esc(t.task_id)}: ${esc(t.status)}</span>`).join(" ")}</p>` : ""}
    ${bb.late.length ? `<p class="small bad">${bb.late.length} late entr${bb.late.length === 1 ? "y" : "ies"} after the adjudicator's inputs were fixed — kept for traceability, not used.</p>` : ""}
    ${failureBlock}
    <p class="small"><b>Recent events</b></p>
    <table class="bb-table"><thead><tr><th>#</th><th>Time</th><th>Kind</th><th>Section</th><th>Writer</th><th>Process</th><th>Event</th></tr></thead><tbody>
    ${bb.events.map(e => `<tr class="${e.late ? "late" : ""}"><td>${e.run_seq}</td><td>${time(e.ts)}</td><td>${esc(e.kind)}</td><td>${esc(e.section)}</td>
      <td>${esc(e.author)}${e.auth === "valid" ? "" : ` <span class="bad">(${esc(e.auth)})</span>`}</td><td>${esc(e.process)}</td>
      <td>${esc(e.what)}${e.late ? ' <span class="bad">late</span>' : ""}</td></tr>`).join("")}</tbody></table>`;
}

// ------------------------------------------------------------------ execution monitoring (recorded trace, read-only)
async function loadTrace(runId) {
  let tr;
  try { tr = await api(`/api/runs/${encodeURIComponent(runId)}/trace`); }
  catch (e) { $("trace-body").innerHTML = `<p class="small bad">Execution trace unavailable: ${esc(e.message)}</p>`; return; }
  if (S.run === runId) {
    S.trace = tr;
    renderTrace(tr);
    renderOverview();
    renderCoordination();
    renderAlerts();
  }
}
const msText = v => v === null || v === undefined ? '<span class="muted">not recorded</span>' : v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${Math.round(v)} ms`;
const tokText = v => v === null || v === undefined ? '<span class="muted">not reported</span>' : esc(v);
const evCls = s => s === "ok" ? "ok" : s === "info" ? "" : s === "blocked" || s === "timeout" ? "warn" : "bad";
function traceTable(head, rows) {
  if (!rows.length) return '<p class="small muted">none recorded</p>';
  return `<table class="bb-table"><thead><tr>${head.map(h => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}
function outcomeBlock(o) {
  const names = l => l.length ? esc(l.map(n => AGENT_NAMES[n] || n).join(", ")) : '<span class="muted">none</span>';
  const yes = v => v === true ? "yes" : v === false ? "no" : '<span class="muted">not known</span>';
  const failures = traceTable(["Time", "Agent", "Task", "Provider · model", "Error code", "HTTP status", "Category", "Retryable", "Exception", "What happened"],
    o.failures.map(f => [time(f.occurred_at), esc(AGENT_NAMES[f.agent] || f.agent || "not attributed"), esc(f.task_id || ""), esc([f.provider, f.model].filter(Boolean).join(" · ") || "not recorded"),
      `<code>${esc(f.error_code)}</code>`, f.http_status === null ? '<span class="muted">not available</span>' : esc(f.provider_status ? `${f.http_status} ${f.provider_status}` : f.http_status),
      esc(f.category), yes(f.retryable), esc(f.exception_type), esc(f.summary)]));
  const RETRY = { recovered: "the second request succeeded", failed: "the second request failed too", refused: "the local budget did not allow a second request; none was sent" };
  const tried = (S.view && S.view.provider_retries) || o.retries || [];
  const retries = tried.length ? `<p class="small"><b>Automatic retries</b></p>${traceTable(["Time", "Agent", "Original error", "HTTP status", "Waited", "Result", "What happened"],
    tried.map(r => [time(r.occurred_at), esc(AGENT_NAMES[r.agent] || r.agent), `<code>${esc(r.error_code)}</code>`, esc(r.http_status), `${esc(r.delay_s)} s`, retryPill(r) || esc(r.outcome), esc(RETRY[r.outcome] || r.outcome)]))}` : "";
  const badge = (S.view && S.view.status_badge) || o.run_status;
  const [, cls, note] = RUN_STATUS[badge] || ["", "neutral", ""];
  return `<dl class="kv small"><dt>Run status</dt><dd><span class="pill ${cls}">${esc(badge)}</span> <span class="muted">${esc(note)}</span></dd>
      <dt>Human approval</dt><dd>${o.requires_human_approval === null ? '<span class="muted">not recorded</span>' : o.requires_human_approval ? "required" : "not required"}</dd>
      <dt>Reports preserved</dt><dd>${names(o.preserved_reports)}</dd><dt>Used in adjudication</dt><dd>${names(o.adjudicated_reports)}</dd>
      <dt>Failed tasks</dt><dd>${names(o.failed_tasks)}</dd><dt>Downstream tasks blocked</dt><dd>${names(o.blocked_tasks)}</dd>
      <dt>Completed after retry</dt><dd>${names(tried.filter(r => r.outcome === "recovered").map(r => r.agent))}</dd></dl>
    ${o.explanation ? `<div class="notice warn small">${esc(o.explanation)}</div>` : ""}
    ${retries}
    <p class="small"><b>Provider and agent failures not recovered</b> <span class="muted">(only an HTTP 503 is retried automatically: once per agent, through the request ledger)</span></p>
    ${o.failures_recorded ? failures : '<p class="small muted">Not classified: this run was stored before failures were recorded separately. See its agent errors.</p>'}`;
}
function renderTrace(tr) {
  if (!tr.available) { $("trace-body").innerHTML = `${outcomeBlock(tr.outcome)}<p class="small muted">${esc(tr.reason)}</p>`; return; }
  const m = tr.metrics || {}, ev = tr.events || [];
  const label = m.execution === "offline test double" ? '<span class="pill warn">OFFLINE — labelled test doubles, no model was called</span>'
    : m.execution === "live gemini" ? '<span class="pill ok">LIVE — Gemini</span>' : `<span class="pill">${esc(m.execution || "unknown")}</span>`;
  const meta = e => e.metadata || {};
  const at = iso => new Date(iso).getTime();
  const bars = [], starts = {};
  ev.forEach(e => {
    if (e.event_type === "agent.start") starts[e.agent_name] = at(e.timestamp);
    if (e.event_type === "agent.end" && starts[e.agent_name] !== undefined) bars.push([e.agent_name, starts[e.agent_name], at(e.timestamp), e.status]);
    if (e.event_type === "baseline.run" && e.duration_ms !== null) bars.push(["baseline", at(e.timestamp), at(e.timestamp) + e.duration_ms, e.status]);
    if (e.event_type === "a2a.response" && e.duration_ms !== null) bars.push(["A2A request → response", at(e.timestamp) - e.duration_ms, at(e.timestamp), e.status]);
  });
  const t0 = bars.length ? Math.min(...bars.map(b => b[1])) : 0;
  const span = Math.max(1, ...bars.map(b => b[2] - t0));
  const timeline = bars.length ? `<div class="trace-timeline">${bars.sort((x, y) => x[1] - y[1]).map(([name, a, b, st]) => {
    const left = (a - t0) / span * 100, width = Math.max(0.5, (b - a) / span * 100);
    return `<div class="trace-row"><span class="trace-name">${esc(name)}</span><span class="trace-lane"><span class="trace-bar ${evCls(st)}" style="left:${left}%;width:${width}%" title="${esc(time(new Date(a).toISOString()))} – ${esc(time(new Date(b).toISOString()))}"></span></span><span class="trace-dur">${msText(b - a)}</span></div>`;
  }).join("")}</div>` : '<p class="small muted">no agent timings recorded</p>';
  const taskCls = s => s === "COMPLETED" ? "ok" : s === "BLOCKED" || s === "TIMED_OUT" ? "warn" : "bad";
  const tasks = traceTable(["Task", "Agent", "Status", "Lifecycle (manager)", "Agent duration (own record)", "Model requests", "Error code"],
    (m.tasks || []).map(t => [esc(t.task_id), esc(t.agent), `<span class="pill ${taskCls(t.status)}">${esc(t.status)}</span>`, msText(t.lifecycle_ms), msText(t.execution_ms),
      t.model_calls ? `${t.model_calls} · ${msText(t.model_request_ms)}` : '<span class="muted">none</span>', esc(t.error_code || "")]));
  const toolEvents = ev.filter(e => e.event_type === "tool.call" || e.event_type === "mcp.call"), a2aEvents = ev.filter(e => e.component === "a2a");
  const boardEvents = ev.filter(e => e.component === "blackboard"), problems = ev.filter(e => ["error", "timeout", "blocked", "rejected"].includes(e.status));
  const tools = traceTable(["Time", "Recorded by", "Agent", "Tool", "Status", "Duration"],
    toolEvents.map(e => [time(e.timestamp), esc(e.component === "mcp" ? "MCP server log" : `${e.component} callback`), esc(e.agent_name || ""), esc(meta(e).tool || ""), `<span class="${evCls(e.status)}">${esc(e.status)}</span>`, msText(e.duration_ms)]));
  const a2a = traceTable(["Time", "Event", "Message", "Context ref", "Status", "Duration"],
    a2aEvents.map(e => [time(e.timestamp), esc(e.event_type), esc(meta(e).message_type || meta(e).message_id || ""), esc(e.context_ref ? `${e.context_ref.section}@${e.context_ref.run_seq}` : (meta(e).context_refs ?? "")), `<span class="${evCls(e.status)}">${esc(e.status)}</span>${e.error_code ? ` ${esc(e.error_code)}` : ""}`, msText(e.duration_ms)]));
  const prov = traceTable(["Time", "Event", "Producer", "Consumer", "Section", "Entry", "SHA-256", "Receipt / auth", "Duration"],
    boardEvents.map(e => [time(e.timestamp), esc(e.event_type.replace("blackboard.", "")), esc(meta(e).producer || ""), esc(meta(e).consumer || ""), esc(meta(e).section || ""), esc(meta(e).run_seq ?? ""), `<code>${esc(meta(e).source_sha256 || meta(e).sha256 || "")}</code>`, `<span class="${evCls(e.status)}">${esc(meta(e).receipt || meta(e).auth || e.status)}</span>`, msText(e.duration_ms)]));
  const recovered = ((S.view && S.view.provider_retries) || []).filter(r => r.outcome === "recovered");
  const errors = traceTable(["Time", "Component", "Event", "Code", "Category", "Detail"],
    problems.map(e => [time(e.timestamp), esc(e.component), esc(e.event_type), esc(e.error_code || ""), esc(e.error_category || ""),
      esc(meta(e).detail || meta(e).reason || meta(e).error || "") + retryPill(e.event_type === "model.call" && recovered.find(r => r.agent === e.agent_name) || {})]));
  const models = traceTable(["Agent", "Model", "Execution", "Calls", "Usage reported", "Prompt", "Completion", "Total", "Cached", "Median request", "Cost"],
    (m.models || []).map(u => [esc(u.agent), esc(u.model || "unknown"), esc(u.execution), `${u.calls}${u.failed_calls ? ` (${u.failed_calls} failed)` : ""}`, `${u.usage_reported_calls} of ${u.calls}`, tokText(u.prompt_tokens), tokText(u.completion_tokens), tokText(u.total_tokens), tokText(u.cached_tokens), msText(u.request_ms ? u.request_ms.median_ms : null), u.cost === null ? `<span class="muted">${esc(u.cost_note)}</span>` : `${esc(u.cost)} ${esc(u.currency)}`]));
  const allEvents = traceTable(["Time", "Component", "Event", "Agent", "Task", "Status", "Duration"],
    ev.map(e => [time(e.timestamp), esc(e.component), esc(e.event_type), esc(e.agent_name || ""), esc(e.task_id || ""), `<span class="${evCls(e.status)}">${esc(e.status)}</span>${e.error_code ? ` ${esc(e.error_code)}` : ""}`, msText(e.duration_ms)]));
  const stat = s => s ? `${s.count} · median ${msText(s.median_ms)} · max ${msText(s.max_ms)}` : '<span class="muted">not recorded</span>';
  const collection = tr.collection_errors || [];
  $("trace-body").innerHTML = `
    <p class="small">${label} <span class="muted">Assembled after the run from events recorded as they happened (not a live stream) · ${ev.length} events · correlation <code>${esc(tr.correlation_id)}</code></span></p>
    ${outcomeBlock(tr.outcome)}
    <dl class="kv small"><dt>Total</dt><dd>${msText(m.total_ms)}</dd>
      <dt>Agents</dt><dd>sum of own execution times ${msText(m.agents_sequential_sum_ms)} · elapsed ${msText(m.agents_elapsed_ms)} · Investigator ∥ Forensic overlap ${msText(m.parallel_overlap_ms)}</dd>
      <dt>MCP calls</dt><dd>${m.mcp_calls ?? 0} · ${stat(m.mcp_latency)}</dd><dt>A2A requests</dt><dd>${m.a2a_requests ?? 0} · ${stat(m.a2a_latency)}</dd>
      <dt>Blackboard</dt><dd>publish ${stat(m.blackboard_publish)} | consume ${stat(m.blackboard_consume)} | verify ${stat(m.blackboard_verify)} | seal ${stat(m.blackboard_seal)}</dd></dl>
    ${diag("trace-timeline", "Timeline", timeline)}
    ${diag("trace-tasks", counted("Task lifecycle", (m.tasks || []).length), tasks)}
    ${diag("trace-tools", counted("Tool calls: ADK and CrewAI callbacks, MCP server log", toolEvents.length), tools)}
    ${diag("trace-a2a", counted("A2A communication", a2aEvents.length), a2a)}
    ${diag("trace-provenance", counted("Blackboard provenance", boardEvents.length), prov)}
    ${diag("trace-errors", counted("Errors, timeouts and blocked tasks", problems.length), errors, problems.length > 0)}
    ${diag("trace-models", `Model usage and cost <span class="muted">(only what the framework or provider reported)</span>`, models)}
    ${diag("trace-events", counted("All recorded events", ev.length), allEvents)}
    ${collection.length ? `<p class="small bad"><b>Monitoring collection errors</b> (they never affect the investigation)</p><ul class="small">${collection.map(c => `<li>${esc(c)}</li>`).join("")}</ul>` : ""}`;
}

// ------------------------------------------------------------------ Security & Trust: Evidence Integrity
// Everything here is read from the backend's result file and from stored run traces: nothing is computed in the browser.
const yesNo = (v, yes, no) => `<span class="pill ${v ? "ok" : "bad"}">${v ? yes : no}</span>`;
async function loadEvidenceIntegrity() {
  let d;
  try { d = await api("/api/evidence-integrity"); }
  catch (e) { $("integrity-body").innerHTML = `<p class="small bad">Evidence Integrity unavailable: ${esc(e.message)}</p>`; return; }
  renderEvidenceIntegrity(d);
  clearTimeout(S.integrityPoll);
  if (d.job && d.job.state === "running") S.integrityPoll = setTimeout(loadEvidenceIntegrity, 3000);
}
function loadSecurity() { loadEvidenceIntegrity(); loadPromptInjection(); }
function runSummaryRow(label, r) {
  return [esc(label), esc(r.claim_id), esc(r.final_decision), esc(r.decided_by), esc(r.review_status), r.human_review_required ? "required" : "not required",
    r.payout === null ? "none" : money(r.payout), r.investigator_received ? "received" : "refused", r.forensic_received ? "received" : "refused"];
}
function renderEvidenceIntegrity(d) {
  const t = d.controlled_test, job = d.job, real = d.real_investigations;
  const running = job && job.state === "running";
  const failed = job && job.state === "failed" ? `<p class="small bad">The test did not finish: ${esc(job.error || "no detail")}</p>` : "";
  const button = `<button class="btn-secondary" id="integrity-run" ${running ? "disabled" : ""}>${running ? "Test running…" : "Run controlled test"}</button>`;
  let test = '<p class="small muted">No controlled test has been run yet.</p>';
  if (t) {
    const a = t.attacks[0], bad = t.tampered_run, c = t.summary;
    const refused = !bad.investigator_received && !bad.forensic_received;
    const run = `<dl class="kv small"><dt>Claim ID</dt><dd>${esc(t.claim_id)} <span class="muted">(specified as ${esc(t.claim_alias)})</span></dd>
        <dt>Attack type</dt><dd><code>${esc(a.detected.join(", "))}</code></dd>
        <dt>Original repair amount</dt><dd>${esc(t.original_repair_amount)}</dd>
        <dt>Tampered test amount</dt><dd>${esc(t.tampered_repair_amount)} <span class="muted">(disposable copy only)</span></dd>
        <dt>Signature verification</dt><dd><span class="pill neutral">${esc(a.signature.toLowerCase())}</span> <span class="muted">the HMAC signs the original hash; the changed payload no longer matches that hash</span></dd></dl>
      <p class="small"><b>Whole investigations</b> <span class="muted">(WC-2026-0411 is approved by the rules, so there the tamper changes the outcome)</span></p>
      ${traceTable(["Run", "Claim", "Decision", "Decided by", "Agent review", "Human review", "Payout", "Investigator", "Forensic"], [
        runSummaryRow("Clean", t.clean_run), runSummaryRow("Tampered", t.tampered_run), runSummaryRow("Clean", t.control_clean_run),
        runSummaryRow("Tampered", t.control_tampered_run), runSummaryRow("Tampered after read", t.control_late_run)])}`;
    const attacks = traceTable(["", "Attack", "Detected as", "Payload", "Signature", "Consumption", "Result"], t.attacks.map(x => [esc(x.attack), esc(x.name),
        x.detected.length ? x.detected.map(v => `<code>${esc(v)}</code>`).join(" ") : "—", esc(x.payload_integrity), esc(x.signature),
        x.investigator_received || x.forensic_received ? "consumed" : "refused", mark(x.passed)]));
    test = `${stats([["Attack variants tested", esc(c.total)],
        ["Handled as expected", yesNo(c.failed === 0, `${c.passed} of ${c.total}`, `${c.failed} of ${c.total} failed`)],
        ["Payload integrity", yesNo(a.payload_integrity === "MATCH", "match", "mismatch detected")],
        ["Evidence consumption", yesNo(refused, "refused", "consumed")],
        ["Human review", yesNo(bad.human_review_required, "required", "not required")]])}
      <p class="small muted">${esc(t.execution)}. Ran ${esc(new Date(t.ran_at).toLocaleString())}; ${esc(t.gemini_requests)} model requests.</p>
      ${diag("integrity-test", "Test details and whole investigations", run)}
      ${diag("integrity-attacks", counted("Attack-by-attack results", t.attacks.length), attacks)}`;
  }
  const events = real.events;
  $("integrity-body").innerHTML = `
    <div class="sec-head"><div class="block-title">Evidence Integrity</div>${button}</div>
    <p class="small"><span class="pill warn">Controlled adversarial test</span> <span class="muted">a disposable copy is altered on purpose; no real claim, production data or model request is involved</span></p>
    ${failed}${test}
    <p class="small sec-real"><span class="pill info">Real claim investigations</span> <span class="muted">${events.length} integrity event(s) in the ${esc(real.runs_checked)} most recent stored run traces</span></p>
    ${events.length ? diag("integrity-real", counted("Events from real investigations", events.length), traceTable(["Time", "Run", "Claim", "Attack type", "Refused reader", "Entry", "Payload", "Signature", "Escalation"], events.map(e => [time(e.timestamp), esc(e.run_id), esc(e.claim_id),
      `<code>${esc(e.attack_type)}</code>`, esc(AGENT_NAMES[e.consumer] || e.consumer || "controller"), esc(e.section || "—"), esc(e.payload_integrity), esc(e.signature), esc(e.escalation_status)])), true)
    : '<p class="small muted">No integrity events recorded in these runs.</p>'}`;
  $("integrity-run").addEventListener("click", async () => {
    try { await post("/api/evidence-integrity/run", {}); loadEvidenceIntegrity(); }
    catch (e) { $("integrity-body").insertAdjacentHTML("afterbegin", `<p class="small bad">${esc(e.message)}</p>`); }
  });
}

// ------------------------------------------------------------------ Security & Trust: Prompt Injection Protection
// Only what the backend stored: the latest controlled test's result file and the events of real runs, kept apart.
// "Prevented" needs an unsafe agent output that a control stopped; "not detected, no effect" is shown as what it is.
const INJECTION_OUTCOMES = {
  PREVENTED: ["ok", "Prevented"], DETECTED: ["info", "Detected"], NOT_DETECTED_NO_EFFECT: ["warn", "Not detected, no effect"],
  NOT_PREVENTED: ["bad", "Not prevented"], TEST_FAILED: ["bad", "Test failed"] };

async function loadPromptInjection() {
  let d;
  try { d = await api("/api/prompt-injection"); }
  catch (e) { $("injection-body").innerHTML = `<p class="small bad">Prompt Injection Protection unavailable: ${esc(e.message)}</p>`; return; }
  renderPromptInjection(d);
  clearTimeout(S.injectionPoll);
  if (d.job && d.job.state === "running") S.injectionPoll = setTimeout(loadPromptInjection, 3000);
}

function injectionOutcome(x) {
  const [kind, label] = INJECTION_OUTCOMES[x.outcome] || ["neutral", x.outcome];
  return `<span class="pill ${kind}">${esc(label)}</span>`;
}

function renderPromptInjection(d) {
  const t = d.controlled_test, job = d.job, real = d.real_investigations;
  const running = job && job.state === "running";
  const failed = job && job.state === "failed" ? `<p class="small bad">The test did not finish: ${esc(job.error || "no detail")}</p>` : "";
  const button = `<button class="btn-secondary" id="injection-run" ${running ? "disabled" : ""}>${running ? "Test running…" : "Run controlled test"}</button>`;
  let test = '<p class="small muted">No controlled test has been run yet.</p>';
  if (t) {
    const s = t.summary, legit = t.legitimate_content, agents = l => esc(l.map(a => AGENT_NAMES[a] || a).join(", ") || "—");
    const groups = [...new Set(t.attempts.map(x => x.category))].map(c => t.attempts.filter(x => x.category === c));
    const byCategory = traceTable(["", "Attack category", "Attempts", "Detected", "Prevented", "Not detected, no effect", "Not prevented", "Affected agents"], groups.map(g => [esc(g[0].category), esc(g[0].category_name), g.length,
      g.filter(x => x.detected).length, g.filter(x => x.prevented).length, g.filter(x => x.outcome === "NOT_DETECTED_NO_EFFECT").length, g.filter(x => x.outcome === "NOT_PREVENTED").length,
      agents([...new Set(g.flatMap(x => x.affected_agents || []))].sort())]));
    const attempts = traceTable(["", "Attack category", "Attempt", "Detected", "Stopped by", "Affected agent", "Security outcome", "Human review"], t.attempts.map(x => [esc(x.attempt), esc(x.category_name), esc(x.name),
        x.detected ? "yes" : "no", esc((x.controls || []).join(", ") || "—"), agents(x.affected_agents || []), injectionOutcome(x),
        x.human_review_required === undefined ? "—" : x.human_review_required ? "required" : "not required"]));
    test = `${stats([["Attempts tested", `${esc(s.tested)} <span class="muted">in ${esc(s.categories)} categories</span>`], ["Detected", esc(s.detected)],
        ["Prevented", esc(s.prevented)], ["Not detected, no effect", esc(s.not_detected_no_effect)],
        ["Not prevented", yesNo(s.not_prevented === 0, "0", String(s.not_prevented))], ["Test execution failed", yesNo(s.test_failed === 0, "0", String(s.test_failed))],
        ["Legitimate content", yesNo(legit.same_as_clean, `${legit.lines_added} look-alike lines changed nothing`, "the outcome changed")]])}
      <p class="small muted">Prevented means an agent's unsafe output was refused or overruled. Not detected, no effect means nothing recorded the attempt and the outcome stayed that of the clean claim. ${esc(t.execution)}. Ran ${esc(new Date(t.ran_at).toLocaleString())}; ${esc(t.gemini_requests)} model requests.</p>
      ${diag("injection-categories", counted("Results by attack category", groups.length), byCategory)}
      ${diag("injection-attempts", counted("Attempt-by-attempt results", t.attempts.length), attempts)}`;
  }
  const events = real.events;
  $("injection-body").innerHTML = `
    <div class="sec-head"><div class="block-title">Prompt Injection Protection</div>${button}</div>
    <p class="small"><span class="pill warn">Controlled adversarial test</span> <span class="muted">synthetic instructions in disposable copies of the sample PDFs; no real claim, production data or model request. It shows what the safeguards do around an obedient agent, not how a real model behaves.</span></p>
    ${failed}${test}
    <p class="small sec-real"><span class="pill info">Real claim investigations</span> <span class="muted">${events.length} prompt injection event(s) in the ${esc(real.runs_checked)} most recent stored run traces</span></p>
    ${events.length ? diag("injection-real", counted("Events from real investigations", events.length), traceTable(["Time", "Run", "Claim", "Attack type", "Agent", "Source", "Outcome", "Escalation"], events.map(e => [time(e.timestamp), esc(e.run_id), esc(e.claim_id),
      `<code>${esc(e.attack_type)}</code>`, esc(AGENT_NAMES[e.agent] || e.agent || "—"), esc(e.source || "—"), esc(e.downstream_impact || e.rejection_status), esc(e.escalation_status)])), true)
    : '<p class="small muted">No prompt injection events recorded in these runs.</p>'}`;
  $("injection-run").addEventListener("click", async () => {
    try { await post("/api/prompt-injection/run", {}); loadPromptInjection(); }
    catch (e) { $("injection-body").insertAdjacentHTML("afterbegin", `<p class="small bad">${esc(e.message)}</p>`); }
  });
}

// ------------------------------------------------------------------ start
renderChips(null);
renderTimeline(null);
loadStatus();
loadSamples();
loadSecurity();
loadReliability();
route();
