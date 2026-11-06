/* ClaimSleuth - Agent Evaluation & Performance Analytics view.
   Renders stored evaluation results from /api/evaluation*. Every number shown comes from the server's computed
   metrics; N/A is drawn as N/A (never as 0). Charts are plain SVG / HTML tables (no external libraries). */
"use strict";
(() => {
  const E = { home: null, result: null, timer: null };
  const q = id => document.getElementById(id);
  const h = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const NA = '<span class="na">N/A</span>';
  const pct = v => v == null ? NA : `${(v * 100).toFixed(1)}%`;
  const num = (v, d = 0) => v == null ? NA : Number(v).toLocaleString("en-IN", { maximumFractionDigits: d });
  async function api(url, opts) {
    const r = await fetch(url, opts);
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || r.statusText);
    return body;
  }
  const msg = (html, cls = "") => { q("eval-message").innerHTML = html ? `<div class="notice ${cls}">${html}</div>` : ""; };

  // ------------------------------------------------------------ view switching (#evaluation)
  function showView() {
    const ev = location.hash.startsWith("#evaluation");
    q("view-evaluation").hidden = !ev;
    q("view-investigation").hidden = ev;
    q("nav-evaluation").classList.toggle("active", ev);
    q("nav-investigation").classList.toggle("active", !ev);
    if (ev && !E.home) load();
  }
  window.addEventListener("hashchange", showView);
  document.addEventListener("DOMContentLoaded", showView);

  // ------------------------------------------------------------ data
  async function load(selectId) {
    try {
      E.home = await api("/api/evaluation");
    } catch (e) { msg(`Could not load the evaluation data: ${h(e.message)}`, "bad"); return; }
    renderOverview(); renderDataset(); renderControls();
    const id = selectId || (E.home.evaluations[0] || {}).evaluation_id;
    if (id) await openResult(id);
    else q("eval-results").innerHTML = '<p class="muted">No evaluation has been run yet. Start an offline evaluation above.</p>';
    if (E.home.job && E.home.job.state === "running") watchJob();
  }

  async function openResult(id) {
    try {
      E.result = await api(`/api/evaluation/results/${encodeURIComponent(id)}`);
      renderResult();
    } catch (e) { msg(`Could not load evaluation ${h(id)}: ${h(e.message)}`, "bad"); }
  }

  // ------------------------------------------------------------ overview, dataset, controls
  function renderOverview() {
    q("eval-overview").innerHTML = `<div class="eval-grid3">
      <div class="eval-card"><div class="eval-k">Approaches compared</div><div>Evidence Agent · Investigator Agent · Forensic Agent · Combined system</div></div>
      <div class="eval-card"><div class="eval-k">Shared task</div><div>Claim decision (APPROVE, REJECT, REQUEST_EVIDENCE, ESCALATE): agents' advisory recommendations vs the adjudicated final decision</div></div>
      <div class="eval-card"><div class="eval-k">Agent-specific tasks</div><div>Evidence: facts, missing evidence, citations · Investigator: policy violation, claim history · Forensic: anomalies</div></div>
    </div>`;
  }

  function renderDataset() {
    const d = E.home.dataset;
    if (d.error) { q("eval-dataset").innerHTML = `<div class="notice bad">Dataset error: ${h(d.error)}</div>`; return; }
    q("eval-dataset").innerHTML = `<h2>Dataset summary</h2>
      <p class="small muted">${h(d.description)}</p>
      <table class="eval-table"><thead><tr><th>Category</th><th>Cases</th><th>Case IDs</th></tr></thead><tbody>
      ${d.categories.map(c => `<tr><td>${h(c.label)}</td><td>${c.cases}</td><td class="mono small">${h(c.case_ids.join(", "))}</td></tr>`).join("")}
      </tbody></table>
      <p class="small">Version <b>${h(d.version)}</b> · ${d.cases} synthetic cases · expected decisions:
        ${Object.entries(d.expected_decisions).map(([k, v]) => `${h(k)} ${v}`).join(", ")} ·
        ${Object.entries(d.sources).map(([k, v]) => `${v} ${h(k)}`).join(", ")}</p>`;
  }

  function renderControls() {
    const H = E.home;
    const options = H.evaluations.map(e => `<option value="${h(e.evaluation_id)}">${h(e.evaluation_id)} · ${h((e.mode || "").toUpperCase())} · ${e.cases_executed}/${e.cases_requested} cases</option>`).join("");
    q("eval-controls").innerHTML = `<h2>Run an evaluation</h2>
      <div class="eval-grid2">
        <div class="eval-card">
          <div class="eval-k">OFFLINE evaluation (default)</div>
          <p class="small">All three agents use the labelled offline test doubles: no Gemini request is made. Results describe the pipeline with deterministic stand-ins, not LLM reasoning.</p>
          <label class="small">Cases <select id="eval-off-cases"><option value="all">All 40</option><option value="10">10 (2 per category)</option><option value="5">5 (1 per category)</option></select></label>
          <button class="eval-btn" id="eval-run-offline">Run offline evaluation</button>
        </div>
        <div class="eval-card">
          <div class="eval-k">LIVE evaluation (Gemini, explicit opt-in)</div>
          <p class="small">Sends real Gemini requests through the shared request ledger (per-minute pacing; only an HTTP 503 is retried, once per agent) and stops at the first 429, unrecovered 503 or budget refusal. Limited to ${H.live_max_cases} cases.</p>
          <label class="small">Cases <input id="eval-live-cases" type="number" min="1" max="${H.live_max_cases}" value="1"></label>
          <button class="eval-btn secondary" id="eval-estimate">Show request estimate</button>
          <div id="eval-estimate-out" class="small"></div>
          <label class="small">Type <b>${h(H.live_confirmation)}</b> to confirm <input id="eval-live-confirm" autocomplete="off"></label>
          <button class="eval-btn danger" id="eval-run-live" disabled>Run live evaluation</button>
        </div>
      </div>
      <div id="eval-job" class="small"></div>
      ${options ? `<label class="small">Stored evaluations <select id="eval-history">${options}</select></label>` : ""}`;
    q("eval-run-offline").onclick = () => start({ mode: "offline", cases: q("eval-off-cases").value });
    q("eval-estimate").onclick = estimate;
    q("eval-live-confirm").oninput = () => { q("eval-run-live").disabled = q("eval-live-confirm").value !== H.live_confirmation; };
    q("eval-run-live").onclick = () => {
      const n = q("eval-live-cases").value;
      if (!confirm(`Send real Gemini requests for ${n} case(s)? This uses your API quota.`)) return;
      start({ mode: "live", cases: n, confirmation: q("eval-live-confirm").value });
    };
    if (q("eval-history")) q("eval-history").onchange = e => openResult(e.target.value);
    if (H.job) renderJob(H.job);
  }

  async function estimate() {
    try {
      const e = await api(`/api/evaluation/estimate?cases=${encodeURIComponent(q("eval-live-cases").value)}`);
      q("eval-estimate-out").innerHTML = `About <b>${e.requests_estimate}</b> Gemini requests (upper bound ${e.requests_upper_bound}) for ${e.cases} case(s) with ${h(e.model)};
        at least ${e.minimum_minutes} min at ${e.per_minute_limit}/min. Local daily budget: ${e.used_today} of ${e.daily_budget} used ·
        ${e.within_budget ? '<span class="ok-text">within budget</span>' : '<span class="bad-text">exceeds the budget - the run would be refused</span>'} ·
        API key ${e.key_configured ? "configured (not shown)" : "<b>not configured</b>"}. ${h(e.note)}`;
    } catch (err) { q("eval-estimate-out").textContent = err.message; }
  }

  async function start(body) {
    msg("");
    try {
      const r = await api("/api/evaluation/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      renderJob(r.job);
      watchJob();
    } catch (e) { msg(h(e.message), "bad"); }
  }

  function renderJob(job) {
    if (!q("eval-job")) return;
    const where = job.total ? ` · ${job.done}/${job.total} cases${job.current ? ` · ${h(job.current)}` : ""}` : "";
    q("eval-job").innerHTML = `<div class="notice ${job.state === "failed" ? "bad" : ""}">Evaluation ${h(job.mode.toUpperCase())}: <b>${h(job.state)}</b>${job.stage ? ` (${h(job.stage)})` : ""}${where}${job.error ? ` – ${h(job.error)}` : ""}</div>`;
  }

  function watchJob() {
    clearInterval(E.timer);
    E.timer = setInterval(async () => {
      try {
        const home = await api("/api/evaluation");
        if (home.job) renderJob(home.job);
        if (!home.job || home.job.state !== "running") {
          clearInterval(E.timer);
          await load(home.job && home.job.evaluation_id);
        }
      } catch (e) { clearInterval(E.timer); }
    }, 2000);
  }

  // ------------------------------------------------------------ charts
  const heatColor = v => {                       // 0 -> very light, 1 -> deep blue (single hue)
    const l = 96 - v * 52;
    return `background:hsl(214,58%,${l}%);color:${v > 0.55 ? "#fff" : "var(--text)"}`;
  };
  function heatCell(v, fmt = pct) {
    return v == null ? `<td class="heat na-cell" title="Not applicable or no data">N/A</td>` : `<td class="heat" style="${heatColor(Math.max(0, Math.min(1, v)))}">${fmt(v)}</td>`;
  }

  function heatmap1(hm) {
    return `<h3>${h(hm.title)}</h3><p class="small muted">${h(hm.note)}</p>
      <table class="eval-table heatmap"><thead><tr><th></th>${hm.columns.map(c => `<th>${h(c[1])}</th>`).join("")}</tr></thead>
      <tbody>${hm.rows.map(r => `<tr><th>${h(r.row)}</th>${hm.columns.map(c => heatCell(r.values[c[0]])).join("")}</tr>`).join("")}</tbody></table>`;
  }

  function heatmap2(hm) {
    return `<h3>${h(hm.title)}</h3>
      <table class="eval-table heatmap"><thead><tr><th>Scenario</th>${hm.columns.map(c => `<th>${h(c[1])}</th>`).join("")}</tr></thead>
      <tbody>${hm.rows.map(r => `<tr><th>${h(r.row)}</th>${hm.columns.map(c => {
        const cell = r.cells[c[0]];
        return cell.value == null ? `<td class="heat na-cell">N/A <span class="small">(n=${cell.n})</span></td>`
          : `<td class="heat" style="${heatColor(cell.value)}">${pct(cell.value)} <span class="small">(n=${cell.n})</span></td>`;
      }).join("")}</tr>`).join("")}</tbody></table>`;
  }

  function heatmap3(hm) {
    const maxLat = Math.max(1, ...hm.rows.map(r => r.values.avg_latency_ms || 0));
    const cell = (r, key) => {
      const v = r.values[key], n = r.values.executions || 0;
      if (v == null) return `<td class="heat na-cell">N/A</td>`;
      if (key === "avg_latency_ms") return `<td class="heat" style="background:hsl(210,12%,${96 - (v / maxLat) * 30}%)">${num(v)} ms</td>`;
      const share = n ? v / n : 0;
      if (!v) return `<td class="heat">0<span class="small"> / ${n}</span></td>`;      // zero: no colour
      const hue = key === "successful" ? 145 : 4;
      return `<td class="heat" style="background:hsl(${hue},55%,${97 - share * 45}%);color:${share > 0.6 ? "#fff" : "var(--text)"}">${v}<span class="small"> / ${n}</span></td>`;
    };
    return `<h3>${h(hm.title)}</h3>
      <table class="eval-table heatmap"><thead><tr><th></th>${hm.columns.map(c => `<th>${h(c[1])}</th>`).join("")}</tr></thead>
      <tbody>${hm.rows.map(r => `<tr><th>${h(r.row)}</th>${hm.columns.map(c => cell(r, c[0])).join("")}</tr>`).join("")}</tbody></table>`;
  }

  function niceTicks(lo, hi, count = 6) {
    if (hi <= lo) hi = lo + 1;
    const raw = (hi - lo) / count, mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw) || raw;
    const ticks = [];
    for (let t = Math.floor(lo / step) * step; t <= hi + step * 1e-9; t += step) ticks.push(+t.toFixed(10));
    return ticks;
  }

  function boxplot(bp) {
    const entries = Object.entries(bp.series || {});
    const ok = entries.filter(([, s]) => s.sufficient);
    let html = `<h3>${h(bp.title)} <span class="muted small">(${h(bp.unit)})</span></h3>`;
    if (bp.note) html += `<p class="small muted">${h(bp.note)}</p>`;
    if (!ok.length) {
      if (entries.length) html += `<p class="small muted">${entries.map(([n, s]) => `${h(n)}: ${h(s.note)}`).join("<br>")}</p>`;
      return html;
    }
    const vals = ok.flatMap(([, s]) => [s.whisker_low, s.whisker_high, ...s.outliers]);
    const ticks = niceTicks(Math.min(...vals), Math.max(...vals));
    const lo = ticks[0], hi = ticks[ticks.length - 1];
    const W = 820, L = 270, R = 30, rowH = 46, top = 10, plotW = W - L - R;
    const x = v => L + ((v - lo) / (hi - lo || 1)) * plotW;
    const Hh = top + entries.length * rowH + 34;
    let svg = `<svg class="boxplot" viewBox="0 0 ${W} ${Hh}" width="100%" role="img" aria-label="${h(bp.title)}">`;
    ticks.forEach(t => { svg += `<line x1="${x(t)}" x2="${x(t)}" y1="${top}" y2="${Hh - 28}" class="bp-grid"/><text x="${x(t)}" y="${Hh - 12}" class="bp-tick" text-anchor="middle">${num(t)}</text>`; });
    entries.forEach(([name, s], i) => {
      const y = top + i * rowH + rowH / 2;
      svg += `<text x="${L - 10}" y="${y + 4}" class="bp-label" text-anchor="end">${h(name)} (n=${s.n})</text>`;
      if (!s.sufficient) { svg += `<text x="${L}" y="${y + 4}" class="bp-note">${h(s.note)}</text>`; return; }
      svg += `<line x1="${x(s.whisker_low)}" x2="${x(s.whisker_high)}" y1="${y}" y2="${y}" class="bp-whisker"/>`;
      svg += `<line x1="${x(s.whisker_low)}" x2="${x(s.whisker_low)}" y1="${y - 8}" y2="${y + 8}" class="bp-whisker"/>`;
      svg += `<line x1="${x(s.whisker_high)}" x2="${x(s.whisker_high)}" y1="${y - 8}" y2="${y + 8}" class="bp-whisker"/>`;
      svg += `<rect x="${x(s.q1)}" y="${y - 13}" width="${Math.max(1, x(s.q3) - x(s.q1))}" height="26" class="bp-box"><title>Q1 ${num(s.q1)} · median ${num(s.median)} · Q3 ${num(s.q3)}</title></rect>`;
      svg += `<line x1="${x(s.median)}" x2="${x(s.median)}" y1="${y - 13}" y2="${y + 13}" class="bp-median"/>`;
      s.outliers.forEach(o => { svg += `<circle cx="${x(o)}" cy="${y}" r="3.5" class="bp-outlier"><title>${num(o)}</title></circle>`; });
    });
    svg += `</svg>`;
    const table = `<table class="eval-table small"><thead><tr><th></th><th>n</th><th>Min</th><th>Q1</th><th>Median</th><th>Q3</th><th>Max</th><th>Outliers</th></tr></thead><tbody>
      ${entries.map(([n, s]) => s.sufficient ? `<tr><th>${h(n)}</th><td>${s.n}</td><td>${num(s.min)}</td><td>${num(s.q1)}</td><td>${num(s.median)}</td><td>${num(s.q3)}</td><td>${num(s.max)}</td><td>${s.outliers.length}</td></tr>`
        : `<tr><th>${h(n)}</th><td>${s.n}</td><td colspan="6" class="muted">${h(s.note)}</td></tr>`).join("")}</tbody></table>`;
    return html + svg + table;
  }

  function confusion(cm) {
    const max = Math.max(1, ...cm.matrix.flat());
    return `<div class="cm"><h4>${h(cm.title)}</h4>
      <table class="eval-table cm-table"><thead><tr><th class="small">expected ↓ / predicted →</th>${cm.labels.map(l => `<th>${h(l)}</th>`).join("")}</tr></thead>
      <tbody>${cm.matrix.map((row, i) => `<tr><th>${h(cm.labels[i])}</th>${row.map((v, j) => {
        const shade = v / max;
        const tone = i === j ? `hsl(145,45%,${97 - shade * 45}%)` : `hsl(4,60%,${98 - shade * 40}%)`;
        return `<td style="background:${v ? tone : "transparent"}">${v}</td>`;
      }).join("")}</tr>`).join("")}</tbody></table>
      <p class="small muted">${cm.n} scored · ${cm.unscored} without a usable output (not counted)</p></div>`;
  }

  // ------------------------------------------------------------ result
  function renderResult() {
    const R = E.result, meta = R.meta, agg = R.aggregate, ap = agg.approaches;
    const live = meta.mode === "live";
    const bm = t => `${pct(t.accuracy)} / ${pct(t.precision)} / ${pct(t.recall)} / ${pct(t.f1)}`;
    const head = `<section class="eval-section">
      <h2>Evaluation ${h(meta.evaluation_id)} <span class="eval-mode ${live ? "live" : "offline"}">${live ? "LIVE · Gemini" : "OFFLINE · test doubles"}</span></h2>
      <p class="small">${h(meta.mode_label)} · dataset ${h(meta.dataset_version)} · ${h(meta.started_at)} · cases executed ${meta.cases_executed}/${meta.cases_requested}
        · Gemini requests added to the ledger: <b>${num(meta.ledger_requests_added)}</b>
        ${meta.stopped_reason ? `<br><span class="bad-text">Stopped early: ${h(meta.stopped_reason)}</span>` : ""}
        ${R.not_run && R.not_run.length ? `<br>Not run: ${h(R.not_run.join(", "))}` : ""}</p>
      ${!live ? `<div class="notice">OFFLINE results: the agents ran on the labelled offline test doubles (deterministic stand-ins), so these numbers describe the pipeline with those stand-ins, not LLM reasoning.</div>` : ""}
      <p class="small"><b>Export:</b>
        <a href="/api/evaluation/results/${encodeURIComponent(meta.evaluation_id)}/export.csv">raw results (CSV)</a> ·
        <a href="/api/evaluation/results/${encodeURIComponent(meta.evaluation_id)}/export.json">aggregated metrics (JSON)</a> ·
        <a href="/api/evaluation/results/${encodeURIComponent(meta.evaluation_id)}/export.md">evaluation report (Markdown)</a></p></section>`;

    const comparison = `<section class="eval-section"><h2>Four-approach comparison</h2>
      <p class="small muted">Each agent on its own task (accuracy / precision / recall / F1 for the binary task named), plus the shared decision task. Agent recommendations are advisory; only the combined system decides.</p>
      <table class="eval-table"><thead><tr><th>Approach</th><th>Own task</th><th>Acc / Prec / Rec / F1</th><th>False-positive rate</th><th>Other task metrics</th><th>Citation validity</th><th>Decision accuracy</th></tr></thead><tbody>
      <tr><th>${h(ap.evidence.title)}</th><td class="small">${h(ap.evidence.task)}</td><td>${bm(ap.evidence.task_metrics)}</td><td>${pct(ap.evidence.task_metrics.false_positive_rate)}</td>
        <td class="small">Fact extraction ${pct(ap.evidence.fact_extraction.accuracy)} (${ap.evidence.fact_extraction.matched}/${ap.evidence.fact_extraction.expected})</td>
        <td>${pct(ap.evidence.citation_validity.rate)}</td><td>${pct(ap.evidence.decision.accuracy)} <span class="small muted">advisory</span></td></tr>
      <tr><th>${h(ap.investigator.title)}</th><td class="small">${h(ap.investigator.task)}</td><td>${bm(ap.investigator.task_metrics)}</td><td>${pct(ap.investigator.task_metrics.false_positive_rate)}</td>
        <td class="small">History-anomaly detection: recall ${pct(ap.investigator.history_anomaly.recall)}, precision ${pct(ap.investigator.history_anomaly.precision)}</td>
        <td>${pct(ap.investigator.citation_validity.rate)}</td><td>${pct(ap.investigator.decision.accuracy)} <span class="small muted">advisory</span></td></tr>
      <tr><th>${h(ap.forensic.title)}</th><td class="small">${h(ap.forensic.task)}</td><td>${bm(ap.forensic.task_metrics)}</td><td>${pct(ap.forensic.task_metrics.false_positive_rate)}</td>
        <td class="small">Anomaly recall by type: ${Object.entries(ap.forensic.per_type_recall).map(([k, v]) => `${h(k.replaceAll("_", " "))} ${pct(v.recall)}`).join(" · ")}</td>
        <td>${pct(ap.forensic.citation_validity.rate)}</td><td>${pct(ap.forensic.decision.accuracy)} <span class="small muted">advisory</span></td></tr>
      <tr><th>${h(ap.combined.title)}</th><td class="small">${h(ap.combined.task)}</td>
        <td>${pct(ap.combined.decision.accuracy)} / ${pct(ap.combined.decision.macro.precision.value)} / ${pct(ap.combined.decision.macro.recall.value)} / ${pct(ap.combined.decision.macro.f1.value)} <span class="small muted">macro</span></td>
        <td>${pct(ap.combined.anomaly_escalation.false_positive_rate)} <span class="small muted">escalation</span></td>
        <td class="small">Weighted F1 ${pct(ap.combined.decision.weighted.f1)} · anomaly escalation recall ${pct(ap.combined.anomaly_escalation.recall)} · review status ${Object.entries(ap.combined.review_status).map(([k, v]) => `${k} ${v}`).join(", ")}</td>
        <td>${NA}</td><td>${pct(ap.combined.decision.accuracy)} <span class="small muted">final</span></td></tr>
      </tbody></table></section>`;

    const heat = `<section class="eval-section"><h2>Performance heatmaps</h2>${heatmap1(agg.heatmap_comparative)}${heatmap2(agg.heatmap_scenario)}</section>`;
    const boxes = `<section class="eval-section"><h2>Latency and usage distributions</h2>
      ${boxplot(agg.boxplots.latency_all)}${boxplot(agg.boxplots.latency_agents)}${boxplot(agg.boxplots.api_requests)}${boxplot(agg.boxplots.tokens)}</section>`;
    const cms = `<section class="eval-section"><h2>Confusion matrices</h2>
      ${confusion(agg.confusion.combined)}
      <div class="cm-grid">${confusion(agg.confusion.evidence)}${confusion(agg.confusion.investigator)}${confusion(agg.confusion.forensic)}</div>
      <details class="tech-details"><summary>Agents' advisory decision recommendations (4 classes)</summary>
        <div class="cm-grid">${Object.values(agg.confusion.advisory).map(confusion).join("")}</div></details></section>`;
    const scen = `<section class="eval-section"><h2>Scenario-level analysis</h2>
      <table class="eval-table"><thead><tr><th>Scenario</th><th>Cases</th><th>Final-decision accuracy</th><th>Completion (Evidence / Investigator / Forensic / Combined)</th><th>Mean combined latency</th><th>Wrong final decisions</th></tr></thead><tbody>
      ${agg.scenarios.map(s => `<tr><th>${h(s.label)}</th><td>${s.cases}</td><td>${pct(s.final_decision_accuracy)}</td>
        <td>${["evidence", "investigator", "forensic", "combined"].map(a => pct(s.completion[a])).join(" / ")}</td>
        <td>${s.mean_latency_ms.combined == null ? NA : num(s.mean_latency_ms.combined) + " ms"}</td>
        <td class="small">${s.wrong_final_decisions.length ? s.wrong_final_decisions.map(w => `${h(w.case_id)}: expected ${h(w.expected)}, got ${h(w.got)}`).join("<br>") : "none"}</td></tr>`).join("")}
      </tbody></table></section>`;
    const rel = `<section class="eval-section"><h2>Execution reliability</h2>${heatmap3(agg.heatmap_reliability)}</section>`;
    const u = agg.usage;
    const usage = `<section class="eval-section"><h2>Usage</h2><table class="eval-table"><tbody>
      <tr><th>Gemini API requests</th><td>${num(u.gemini_requests_total)} <span class="small muted">(${h(u.gemini_requests_source)})</span></td></tr>
      <tr><th>MCP tool calls</th><td>${num(u.mcp_calls_total)} <span class="small muted">mean ${num(u.mcp_calls_mean_per_case, 1)} per case (MCP server's own log)</span></td></tr>
      <tr><th>Tokens</th><td>${num(u.tokens_total)}</td></tr>
      <tr><th>Estimated cost</th><td>${NA} <span class="small muted">${h(u.estimated_cost_note)}</span></td></tr></tbody></table></section>`;
    q("eval-results").innerHTML = head + comparison + heat + boxes + cms + scen + rel + usage;
  }
})();
