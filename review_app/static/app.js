const API_BASE = "/api";

let currentFindings = [];
let activeCategory = "";
let DICTIONARY = { tables: {}, columns: {} };
let fullFindingCache = {};
let jobPollInterval = null;
let currentJobId = null; // set only while a job is RUNNING
let stopRequested = false;
let jobPollNow = null; // the active poll function, for an immediate re-check when back online

async function fetchJSON(url, options) {
  const res = await fetch(url, options);

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Request failed (${res.status}): ${text}`);
  }

  return res.json();
}

function escapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

const IST_PARTS_FORMATTER = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Kolkata",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

// Backend timestamps are UTC ISO strings; show them as "YYYY-MM-DD HH:mm:ss IST".
function formatIST(isoString) {
  if (!isoString) return "";
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return isoString;
  const p = Object.fromEntries(IST_PARTS_FORMATTER.formatToParts(date).map((x) => [x.type, x.value]));
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second} IST`;
}

// ---------------------------------------------------------------------------
// CR1/CR2: hover tooltips for TABLE.COLUMN references and pillar tabs
// ---------------------------------------------------------------------------

async function loadDictionary() {
  try {
    DICTIONARY = await fetchJSON(`${API_BASE}/dictionary?client_id=${encodeURIComponent(CLIENT_ID)}`);
  } catch (error) {
    console.error("Failed to load dictionary:", error);
  }
}

function wrapTableColumnRef(table, column, contextNote) {
  if (!table || !column) return escapeHtml(`${table || ""}${column ? "." + column : ""}`);
  return `<span class="sap-ref" data-table="${escapeHtml(table)}" data-column="${escapeHtml(column)}" data-context="${escapeHtml(contextNote || "")}">${escapeHtml(table)}.${escapeHtml(column)}</span>`;
}

const TABLE_COLUMN_REF_RE = /\b([A-Z][A-Z0-9_]{2,9})\.([A-Z][A-Z0-9_]{1,9})\b/g;

function linkifyTableColumnRefs(escapedText) {
  // escapedText must already be HTML-escaped plain text - safe to inject markup after.
  return escapedText.replace(TABLE_COLUMN_REF_RE, (match, table, column) => wrapTableColumnRef(table, column, ""));
}

function positionTooltip(el, x, y) {
  const offset = 14;
  const rect = el.getBoundingClientRect();
  let left = x + offset;
  let top = y + offset;
  if (left + rect.width > window.innerWidth - 10) left = Math.max(10, x - rect.width - offset);
  if (top + rect.height > window.innerHeight - 10) top = Math.max(10, y - rect.height - offset);
  el.style.left = `${left}px`;
  el.style.top = `${top}px`;
}

function showSapTooltip(x, y, html) {
  const el = document.getElementById("sapTooltip");
  el.innerHTML = html;
  el.classList.remove("hidden");
  positionTooltip(el, x, y);
}

function hideSapTooltip() {
  document.getElementById("sapTooltip").classList.add("hidden");
}

document.addEventListener("mouseover", (event) => {
  const ref = event.target.closest(".sap-ref");
  if (ref) {
    const table = ref.dataset.table;
    const column = ref.dataset.column;
    const context = ref.dataset.context;
    const tableDesc = DICTIONARY.tables[table] || "No description available for this table.";
    const colInfo = DICTIONARY.columns[`${table}.${column}`];
    const colDesc = colInfo
      ? `${colInfo.description}${colInfo.data_type ? ` (${colInfo.data_type})` : ""}`
      : "No description available for this column.";
    let html = `<div class="tt-title">${escapeHtml(table)}.${escapeHtml(column)}</div>` +
      `<div class="tt-line"><strong>Table:</strong> ${escapeHtml(tableDesc)}</div>` +
      `<div class="tt-line"><strong>Column:</strong> ${escapeHtml(colDesc)}</div>`;
    if (context) {
      html += `<div class="tt-line"><strong>In this finding:</strong> ${escapeHtml(context)}</div>`;
    }
    showSapTooltip(event.clientX, event.clientY, html);
    return;
  }
  const plain = event.target.closest("[data-tooltip]");
  if (plain) {
    showSapTooltip(event.clientX, event.clientY, `<div class="tt-line">${escapeHtml(plain.dataset.tooltip)}</div>`);
  }
});

document.addEventListener("mouseout", (event) => {
  if (event.target.closest(".sap-ref, [data-tooltip]")) {
    hideSapTooltip();
  }
});

// ---------------------------------------------------------------------------
// Runs / stats / findings list
// ---------------------------------------------------------------------------

// Page 2 is fixed to the client (and its uploaded data) chosen on page 1 -
// changing either means going back to page 1 (index.html).
const CLIENT_ID = new URLSearchParams(window.location.search).get("client") || "";
const CLIENT_STORAGE_KEY = "dq-review-client";
let WORKSPACE = null; // {client, dictionary, tables, ready} from /api/clients/{id}/workspace
let RUNS_BY_ID = {}; // this client's runs, for lookups in the detail modal

function selectedClientId() {
  return CLIENT_ID;
}

async function loadWorkspace() {
  WORKSPACE = await fetchJSON(`${API_BASE}/clients/${encodeURIComponent(CLIENT_ID)}/workspace`);
  try { localStorage.setItem(CLIENT_STORAGE_KEY, CLIENT_ID); } catch { /* per-viewer convenience only */ }
  document.title = `${WORKSPACE.client.name} · Findings Review`;

  const tables = WORKSPACE.tables.map((t) => t.table);
  document.getElementById("workspaceStrip").innerHTML = `
    <span class="ws-chip ws-client" title="Client">🏢 ${escapeHtml(WORKSPACE.client.name)}</span>
    <span class="ws-chip" title="Data dictionary">📘 ${WORKSPACE.dictionary ? escapeHtml(WORKSPACE.dictionary.file) : "No dictionary"}</span>
    <span class="ws-chip" title="${escapeHtml(tables.join(", "))}">🗂 ${tables.length} table${tables.length === 1 ? "" : "s"}${tables.length ? `: ${escapeHtml(tables.join(", "))}` : ""}</span>
    <a class="ws-change" href="./?client=${encodeURIComponent(CLIENT_ID)}">Change client / data</a>
  `;
}

async function loadRuns() {
  const clientId = selectedClientId();
  const runs = clientId ? await fetchJSON(`${API_BASE}/runs?client_id=${encodeURIComponent(clientId)}`) : [];
  RUNS_BY_ID = Object.fromEntries(runs.map((r) => [r.run_id, r]));
  const select = document.getElementById("runSelect");
  const previous = select.value;

  select.innerHTML =
    '<option value="">All runs</option>' +
    runs
      .map((run) => {
        let tableNames = "";
        try {
          tableNames = JSON.parse(run.table_names || "[]").join(", ");
        } catch {
          tableNames = run.table_names || "";
        }

        const startedAt = run.started_at ? formatIST(run.started_at) : "Unknown time";
        return `<option value="${escapeHtml(run.run_id)}">${escapeHtml(startedAt)} - ${escapeHtml(tableNames)}</option>`;
      })
      .join("");
  if ([...select.options].some((o) => o.value === previous)) select.value = previous;
  updateRunUsage();
}

// Token counts of the selected run (counts only - the server never stores prompts or responses).
function formatTokens(n) {
  n = n || 0;
  return n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n);
}

function updateRunUsage() {
  const el = document.getElementById("runUsage");
  const usage = (RUNS_BY_ID[document.getElementById("runSelect").value] || {}).llm_usage;
  el.classList.toggle("hidden", !usage);
  if (!usage) return;
  const thinking = usage.reasoning_tokens ? ` (thinking ${formatTokens(usage.reasoning_tokens)})` : "";
  el.textContent = `Tokens: ${formatTokens(usage.input_tokens)} in / ${formatTokens(usage.output_tokens)} out${thinking} · ${usage.calls} call${usage.calls === 1 ? "" : "s"}`;
  el.title = `Input ${usage.input_tokens.toLocaleString()} | Output ${usage.output_tokens.toLocaleString()} | Total ${usage.total_tokens.toLocaleString()} tokens over ${usage.seconds}s. Per-call detail: /api/runs/<run id>/llm-usage`;
}

// Where a finding came from. A fixed rule can only find what its authors thought of; an LLM check is the agent
// reading this client's columns and proposing something new, so a reviewer weighs them differently.
const SOURCE_LABELS = {
  BUILT_IN: { option: "Built-in rules", badge: "🧱 Built-in rule", tip: "A fixed, deterministic SAP rule from the rule pack. No LLM was involved in finding this." },
  LLM: { option: "LLM-proposed checks", badge: "🤖 LLM check", tip: "A pandas check the LLM proposed for this client's data and the sandbox ran locally. Read its code before trusting it." },
  DUPLICATE_ENGINE: { option: "Duplicate engine", badge: "👥 Duplicate engine", tip: "The deterministic duplicate matcher, using matching rules drafted once per client and schema." },
};

function sourceBadge(finding) {
  const label = SOURCE_LABELS[finding.source];
  return label ? `<span class="badge badge-source-${escapeHtml(finding.source)}" title="${escapeHtml(label.tip)}">${label.badge}</span>` : "";
}

let countsRequestSeq = 0;

// Counts are computed client-side from the light findings list so they follow
// the Run and Rule Scope filters (/api/stats only filters by run):
// - status chips (Pending/Approved/Rejected): Run + Rule Scope
// - per-tab counts: Run + Rule Scope + Status
async function loadCounts(runId, status, scope) {
  const seq = ++countsRequestSeq;
  const params = new URLSearchParams();
  if (runId) params.append("run_id", runId);
  params.append("client_id", selectedClientId());
  if (scope) params.append("rule_scope", scope);
  const all = await fetchJSON(`${API_BASE}/findings?${params.toString()}`);
  if (seq !== countsRequestSeq) return; // a newer filter change already won

  // How many findings each source produced (before the Source filter), so the dropdown answers
  // "how much of this is built-in rules and how much did the LLM add" at a glance.
  const bySource = { BUILT_IN: 0, LLM: 0, DUPLICATE_ENGINE: 0 };
  all.forEach((f) => { if (f.source in bySource) bySource[f.source] += 1; });
  document.querySelectorAll("#sourceFilter option[value]").forEach((opt) => {
    const base = SOURCE_LABELS[opt.value]?.option;
    if (base) opt.textContent = `${base} (${bySource[opt.value]})`;
    else opt.textContent = `All sources (${all.length})`;
  });
  const sourceValue = document.getElementById("sourceFilter").value;
  const findings = sourceValue ? all.filter((f) => f.source === sourceValue) : all;

  const byStatus = { PENDING: 0, APPROVED: 0, REJECTED: 0 };
  const byTab = { "": 0, ACTIVENESS: 0, DUPLICATE: 0, COMPLETENESS: 0, CORRECTNESS: 0, ANOMALIES: 0 };
  for (const f of findings) {
    if (f.status in byStatus) byStatus[f.status] += 1;
    if (status && f.status !== status) continue;
    byTab[""] += 1;
    if (f.category in byTab) byTab[f.category] += 1;
    if (f.is_anomaly) byTab.ANOMALIES += 1;
  }

  document.querySelectorAll("#categoryTabs .tab-btn").forEach((tab) => {
    tab.querySelector(".tab-count").textContent = byTab[tab.dataset.category] ?? 0;
  });

  const chip = (tone, label, count) =>
    `<span class="stat-chip"><span class="stat-dot stat-dot-${tone}"></span>${label} <span class="count">${count}</span></span>`;
  document.getElementById("statsBar").innerHTML = [
    chip("pending", "Pending", byStatus.PENDING),
    chip("approved", "Approved", byStatus.APPROVED),
    chip("rejected", "Rejected", byStatus.REJECTED),
  ].join("");
}

let scorecardKey = null; // run shown in the scorecard - it only changes when a run is added or picked

async function loadFindings() {
  const runId = document.getElementById("runSelect").value;
  const key = `${runId}|${Object.keys(RUNS_BY_ID).length}`;
  if (key !== scorecardKey) {
    scorecardKey = key;
    loadScorecard();
  }
  const status = document.getElementById("statusFilter").value;
  const scope = document.getElementById("scopeFilter").value;
  const params = new URLSearchParams();
  const container = document.getElementById("findingsContainer");

  if (runId) params.append("run_id", runId);
  params.append("client_id", selectedClientId());
  if (status) params.append("status", status);
  if (scope) params.append("rule_scope", scope);
  const source = document.getElementById("sourceFilter").value;
  if (source) params.append("source", source);

  if (activeCategory === "ANOMALIES") {
    params.append("is_anomaly", "true");
  } else if (activeCategory) {
    params.append("category", activeCategory);
  }

  container.innerHTML = '<p class="empty-state">Loading findings...</p>';

  try {
    // Light payload only (no check_code/raw_result) - see CR3 lazy loading.
    const [findings] = await Promise.all([
      fetchJSON(`${API_BASE}/findings?${params.toString()}`),
      loadCounts(runId, status, scope),
    ]);
    currentFindings = findings;

    if (findings.length === 0) {
      container.innerHTML = '<p class="empty-state">No findings match this filter.</p>';
      return;
    }

    container.innerHTML = findings.map(renderCard).join("");
    attachCardHandlers();
  } catch (error) {
    container.innerHTML = `<p class="empty-state">Unable to load findings: ${escapeHtml(error.message)}</p>`;
  }
}

function renderCard(finding) {
  const decided = finding.status !== "PENDING";
  const reviewedAt = formatIST(finding.reviewed_at);
  const comment = finding.reviewer_comment
    ? ` - ${escapeHtml(finding.reviewer_comment)}`
    : "";

  const categoryLabel = {
    ACTIVENESS: "🕒 Activeness",
    DUPLICATE: "👥 Duplicate",
    COMPLETENESS: "📋 Completeness",
    CORRECTNESS: "✅ Correctness",
  }[finding.category] || finding.category;

  const scopeLabel = finding.rule_scope === "INDUSTRY_SPECIFIC"
    ? `Industry: ${finding.industry || "General"}`
    : (finding.rule_scope === "CLIENT_SPECIFIC" ? "Client-Specific" : "Universal");

  const scopeClass = finding.rule_scope === "INDUSTRY_SPECIFIC"
    ? "badge-scope-INDUSTRY"
    : (finding.rule_scope === "CLIENT_SPECIFIC" ? "badge-scope-CLIENT" : "badge-scope-UNIVERSAL");

  return `
    <div class="finding-card sev-${escapeHtml(finding.severity)}" data-id="${escapeHtml(finding.id)}">
      <div class="top-row">
        <div class="table-col">${wrapTableColumnRef(finding.table_name, finding.column_name, finding.hypothesis || "")}</div>
        <div style="display: flex; gap: 8px; flex-wrap: wrap;">
          ${sourceBadge(finding)}
          <span class="badge badge-cat-${escapeHtml(finding.category)}">${escapeHtml(categoryLabel)}</span>
          <span class="badge ${scopeClass}">${escapeHtml(scopeLabel)}</span>
          ${finding.is_anomaly ? '<span class="badge badge-anomaly">⚡ Anomaly</span>' : ''}
          ${finding.fix_type === 'AUTO_FIXABLE' ? `<span class="badge badge-fix-AUTO">⚡ Auto-fixable: ${escapeHtml(finding.auto_fix_value || 'Default')}</span>` : ''}
          <span class="badge ${escapeHtml(finding.severity)}">${escapeHtml(finding.severity)}</span>
          <span class="badge status-${escapeHtml(finding.status)}">${escapeHtml(finding.status)}</span>
        </div>
      </div>
      <div class="summary">${linkifyTableColumnRefs(escapeHtml(finding.result_summary))}</div>
      <div class="meta">
        <span>Confidence: ${escapeHtml(finding.confidence)}</span>
        <span>Scope: ${escapeHtml(finding.rule_scope)}</span>
        <span>Reusable Skill: ${finding.reusable ? "Yes" : "No"}</span>
        <span>${escapeHtml(formatIST(finding.created_at))}</span>
      </div>
      ${finding.item_count ? renderReviewProgress(finding) : ""}
      <div class="action-row">
        <button class="btn-detail" data-action="detail">
          ${finding.category === 'DUPLICATE' ? '👥 Review Duplicates' : 'View Details'}
        </button>
        ${decided ? `<span class="decided-note">Reviewed ${reviewedAt}${comment}</span>` : ""}
      </div>
    </div>
  `;
}

function renderReviewProgress(finding) {
  const pct = Math.round((finding.reviewed_count / finding.item_count) * 100);
  const scope = finding.category === "DUPLICATE" && finding.group_count
    ? ` in ${finding.group_count} group${finding.group_count === 1 ? "" : "s"}` : "";
  return `
    <div class="card-progress">
      <div class="card-progress-bar"><span style="width:${pct}%"></span></div>
      <span>${finding.reviewed_count} / ${finding.item_count} records reviewed${scope}</span>
    </div>`;
}

function attachCardHandlers() {
  document.querySelectorAll(".finding-card").forEach((card) => {
    card.querySelector('[data-action="detail"]').addEventListener("click", () => openDetail(card.dataset.id));
  });
}

async function submitDecision(id, status) {
  const comment = status === "REJECTED"
    ? prompt("Optional: reason for rejection?") || ""
    : "";

  await fetchJSON(`${API_BASE}/findings/${encodeURIComponent(id)}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status, comment }),
  });

  await loadFindings();
}

// ---------------------------------------------------------------------------
// CR3: detail modal with lazy-loaded metrics/code/records
// ---------------------------------------------------------------------------

function syntheticRowFor(finding) {
  return {
    id: null, row_index: null, key_field: `${finding.table_name}.${finding.column_name}`,
    key_value: "", issue_detail: finding.result_summary, corrected_data: "",
    status: finding.status, review_verdict: "PENDING",
  };
}

async function fetchFullFinding(id) {
  if (!fullFindingCache[id]) {
    fullFindingCache[id] = await fetchJSON(`${API_BASE}/findings/${encodeURIComponent(id)}`);
  }
  return fullFindingCache[id];
}

async function openDetail(id) {
  const finding = currentFindings.find((item) => item.id === id);
  if (!finding) return;

  const categoryLabel = {
    ACTIVENESS: "🕒 Activeness Check",
    DUPLICATE: "👥 Duplicate Analysis",
    COMPLETENESS: "📋 Completeness Check",
    CORRECTNESS: "✅ Correctness Check",
  }[finding.category] || finding.category;

  const isDuplicate = finding.category === "DUPLICATE";
  const codeLabel = isDuplicate ? "View Matching Rules" : "View Local Pandas Check Code";

  document.querySelector("#detailModal .modal-content").classList.toggle("modal-wide", isDuplicate);
  document.getElementById("modalBody").innerHTML = `
    <div class="detail-row detail-top-meta">
      <span class="table-col">${wrapTableColumnRef(finding.table_name, finding.column_name, finding.hypothesis || "")}</span>
      ${sourceBadge(finding)}
      <span class="badge badge-cat-${escapeHtml(finding.category)}">${escapeHtml(categoryLabel)}</span>
      <span class="badge ${escapeHtml(finding.severity)}">${escapeHtml(finding.severity)}</span>
      <span class="badge status-${escapeHtml(finding.status)}">${escapeHtml(finding.status)}</span>
    </div>
    <div class="detail-row">
      <div class="detail-label">Hypothesis &amp; Rule Context</div>
      <pre>${linkifyTableColumnRefs(escapeHtml(finding.hypothesis || "(not captured)"))}</pre>
    </div>
    <div class="detail-row lazy-section" id="lazySectionCode">
      <button class="btn-lazy-load" data-lazy="code">${codeLabel}</button>
    </div>
    ${isDuplicate
      ? `<div class="detail-row" id="duplicateReview"><p class="hint-text">Loading duplicate groups...</p></div>`
      : `<div class="detail-row lazy-section" id="lazySectionRecords">
           <button class="btn-lazy-load" data-lazy="records">View Individual Records</button>
         </div>
         ${renderFindingDecisionRow(finding)}`}
  `;

  attachLazyLoadHandlers(id, finding, codeLabel);
  if (!isDuplicate) {
    attachFindingDecisionHandlers(id);
  }
  document.getElementById("detailModal").classList.remove("hidden");
  if (isDuplicate) {
    await loadDuplicateReview(id);
  }
}

function attachLazyLoadHandlers(id, finding, codeLabel) {
  // Toggle: first click loads and shows the code/rules, next click hides them.
  const codeBtn = document.querySelector('#lazySectionCode [data-lazy="code"]');
  if (codeBtn) {
    codeBtn.addEventListener("click", async () => {
      const section = document.getElementById("lazySectionCode");
      let pre = section.querySelector("pre");
      if (!pre) {
        const full = await fetchFullFinding(id);
        pre = document.createElement("pre");
        pre.className = "hidden";
        pre.textContent = full.check_code || "(not captured)";
        section.appendChild(pre);
      }
      const show = pre.classList.contains("hidden");
      pre.classList.toggle("hidden", !show);
      codeBtn.textContent = show ? codeLabel.replace(/^View /, "Hide ") : codeLabel;
      codeBtn.setAttribute("aria-expanded", String(show));
    });
  }

  const recordsBtn = document.querySelector('#lazySectionRecords [data-lazy="records"]');
  if (recordsBtn) {
    recordsBtn.addEventListener("click", async () => {
      document.getElementById("lazySectionRecords").innerHTML = '<p class="hint-text">Loading...</p>';
      await reopenRecordsSection(id, finding);
    });
  }
}

// Helper columns: the client's chosen context columns (page 1), read from the uploaded CSV by
// row position. Display only - nothing here is analysed or sent to an LLM.
const HELPER_CACHE = {}; // findingId -> {columns:[{name, description, data_type}], rows:{itemId:{col:value}}, stale, note}

async function loadHelper(findingId) {
  try {
    HELPER_CACHE[findingId] = await fetchJSON(`${API_BASE}/findings/${encodeURIComponent(findingId)}/helper-columns`);
  } catch {
    HELPER_CACHE[findingId] = { columns: [], rows: {}, stale: false, note: null };
  }
  return HELPER_CACHE[findingId];
}

function helperColumnsFor(findingId, skip = []) {
  const helper = HELPER_CACHE[findingId];
  return helper && !helper.stale ? helper.columns.filter((c) => !skip.includes(c.name)) : [];
}

function helperHeader(col) {
  const tip = [col.description, col.data_type].filter(Boolean).join(" · ");
  return `<th class="helper-col" title="${escapeHtml(tip || "Helper column")}">${escapeHtml(col.name)}</th>`;
}

function helperCell(findingId, itemId, col) {
  const value = HELPER_CACHE[findingId]?.rows?.[itemId]?.[col.name];
  return `<td class="helper-col">${value ? escapeHtml(value) : '<span class="blank-cell">—</span>'}</td>`;
}

function helperNote(findingId) {
  const helper = HELPER_CACHE[findingId];
  if (!helper) return "";
  if (helper.stale) return `<p class="hint-text helper-note">${escapeHtml(helper.note || "")}</p>`;
  if (!helper.columns.length) return "";
  return `<p class="hint-text helper-note">Helper columns from ${escapeHtml(helper.table)}: ${helper.columns.map((c) => escapeHtml(c.name)).join(", ")}
    - shaded, to help you decide. Change them on page 1 (Change client / data).</p>`;
}

// The server may have rolled the finding's status up from its records: mirror it in the open modal and mark
// the card list for a reload when the modal closes.
async function refreshFindingStatus(findingId) {
  try {
    const fresh = await fetchJSON(`${API_BASE}/findings/${encodeURIComponent(findingId)}`);
    const cached = currentFindings.find((f) => f.id === findingId);
    if (cached && cached.status !== fresh.status) {
      cached.status = fresh.status;
      cached.reviewed_at = fresh.reviewed_at;
      cached.reviewer_comment = fresh.reviewer_comment;
      findingsDirty = true;
    }
    const badge = document.querySelector("#modalBody .detail-top-meta .badge[class*='status-']");
    if (badge) {
      badge.textContent = fresh.status;
      badge.className = `badge status-${fresh.status}`;
    }
  } catch { /* the badge just stays as it was */ }
}

async function reopenRecordsSection(findingId, finding) {
  const items = await fetchJSON(`${API_BASE}/findings/${encodeURIComponent(findingId)}/items`);
  await loadHelper(findingId);
  refreshFindingStatus(findingId);
  const usingSyntheticRow = items.length === 0;
  const effectiveItems = usingSyntheticRow ? [syntheticRowFor(finding)] : items;
  const workflowKey = getWorkflowKey(finding);
  document.getElementById("lazySectionRecords").innerHTML =
    renderWorkflowItemsTable(finding, effectiveItems, usingSyntheticRow, workflowKey);
  attachPillarWorkflowHandlers(findingId, finding, usingSyntheticRow);
}

// ---------------------------------------------------------------------------
// Section 6: pillar-specific decision workflows
// ---------------------------------------------------------------------------

const VERDICT_TONE = {
  ALLOWED_ACTIVE: "positive", UNIQUE: "positive", FALSE_POSITIVE: "positive", LEGITIMATE: "positive",
  NOT_APPLICABLE: "positive", INTENTIONALLY_BLANK: "positive", APPROVED: "positive",
  TO_BE_CONFIRMED: "warning", REQUIRES_BUSINESS_INPUT: "warning", REQUIRES_BUSINESS_REVIEW: "warning",
  REQUIRES_MASTER_DATA_CORRECTION: "warning", NEEDS_INVESTIGATION: "warning", PENDING: "warning",
  CONFIRMED_INACTIVE: "negative", DUPLICATE: "negative", CONFIRMED_ISSUE: "negative",
  MISSING_VALUE: "negative", EXCLUDE_FROM_PROFILING: "negative", REJECTED: "negative",
};

const PILLAR_WORKFLOWS = {
  ACTIVENESS: {
    title: "Flagged Records",
    mode: "verdict",
    showCorrectedInput: false,
    dispositions: [
      { verdict: "ALLOWED_ACTIVE", label: "✅ Allow for Data Profiling" },
      { verdict: "CONFIRMED_INACTIVE", label: "🔴 Confirm Inactive" },
    ],
  },
  COMPLETENESS_AUTO: {
    title: "Individual Issues",
    mode: "decision",
    showCorrectedInput: true,
    correctedLabel: "Corrected Data",
    showAutofill: true,
  },
  COMPLETENESS_MANUAL: {
    title: "Missing / Incomplete Records",
    mode: "verdict",
    showCorrectedInput: true,
    correctedLabel: "Business Input (optional)",
    dispositions: [
      { verdict: "MISSING_VALUE", label: "Missing Value" },
      { verdict: "NOT_APPLICABLE", label: "Not Applicable" },
      { verdict: "INTENTIONALLY_BLANK", label: "Intentionally Blank" },
      { verdict: "REQUIRES_BUSINESS_INPUT", label: "Requires Business Input" },
    ],
  },
  CORRECTNESS_VALUE_ERROR: {
    title: "Individual Issues",
    mode: "decision",
    showCorrectedInput: true,
    correctedLabel: "Corrected Data",
  },
  CORRECTNESS_RELATIONSHIP: {
    title: "Relationship / Integrity Issues",
    mode: "verdict",
    showCorrectedInput: false,
    dispositions: [
      { verdict: "CONFIRMED_ISSUE", label: "Confirm Issue" },
      { verdict: "FALSE_POSITIVE", label: "False Positive" },
      { verdict: "REQUIRES_MASTER_DATA_CORRECTION", label: "Requires Master Data Correction" },
      { verdict: "REQUIRES_BUSINESS_REVIEW", label: "Requires Business Review" },
      { verdict: "EXCLUDE_FROM_PROFILING", label: "Exclude from Future Profiling" },
    ],
  },
  ANOMALY: {
    title: "Flagged Values",
    mode: "verdict",
    showCorrectedInput: false,
    dispositions: [
      { verdict: "LEGITIMATE", label: "Legitimate Value" },
      { verdict: "NEEDS_INVESTIGATION", label: "Needs Investigation" },
    ],
  },
};

function getWorkflowKey(finding) {
  if (finding.is_anomaly) return "ANOMALY";
  if (finding.category === "ACTIVENESS") return "ACTIVENESS";
  if (finding.category === "COMPLETENESS") {
    return finding.fix_type === "AUTO_FIXABLE" ? "COMPLETENESS_AUTO" : "COMPLETENESS_MANUAL";
  }
  if (finding.category === "CORRECTNESS") {
    return finding.effective_sub_type === "RELATIONSHIP_INTEGRITY"
      ? "CORRECTNESS_RELATIONSHIP" : "CORRECTNESS_VALUE_ERROR";
  }
  return "CORRECTNESS_VALUE_ERROR";
}

function verdictButton(itemId, verdict, label) {
  const tone = VERDICT_TONE[verdict] || "warning";
  return `<button class="btn-action btn-tone-${tone}" data-verdict="${escapeHtml(verdict)}" data-item-id="${escapeHtml(itemId)}">${escapeHtml(label)}</button>`;
}

function renderWorkflowItemsTable(finding, items, isSynthetic, workflowKey) {
  const cfg = PILLAR_WORKFLOWS[workflowKey];
  const helperCols = isSynthetic ? [] : helperColumnsFor(finding.id);
  const isAutoFixable = cfg.showAutofill && finding.fix_type === "AUTO_FIXABLE" && finding.auto_fix_value;

  const bulkBar = isSynthetic ? "" : `
    <div class="item-actions-bar">
      ${cfg.mode === "decision" ? `
        <button data-bulk="APPROVED">Approve Selected</button>
        <button data-bulk="REJECTED">Reject Selected</button>
      ` : ""}
      ${isAutoFixable ? `<button id="bulkAutofillBtn" class="btn-autofill">⚡ Apply Recommended Default (${escapeHtml(finding.auto_fix_value)}) to All Pending</button>` : ""}
    </div>
  `;

  return `
    <div class="detail-row" data-workflow="${escapeHtml(workflowKey)}">
      <div class="detail-label">
        ${escapeHtml(cfg.title)} ${isSynthetic ? "(no row-level detail captured)" : `(${items.length})`}
      </div>
      ${bulkBar}
      ${isSynthetic ? "" : helperNote(finding.id)}
      <table class="items-table">
        <thead>
          <tr>
            ${cfg.mode === "decision" ? "<th></th>" : ""}
            <th>Row</th><th>Key Field</th><th>Details</th>${helperCols.map(helperHeader).join("")}
            ${cfg.showCorrectedInput ? `<th>${escapeHtml(cfg.correctedLabel || "Corrected Data")}</th>` : ""}
            <th>${cfg.mode === "decision" ? "Status" : "Disposition"}</th>
          </tr>
        </thead>
        <tbody>
          ${items.map((item) => renderWorkflowRow(finding, item, isSynthetic, cfg, isAutoFixable, helperCols)).join("")}
        </tbody>
      </table>
      ${isSynthetic ? `<p class="hint-text">This check did not produce row-level detail. Use "Approve Finding" / "Reject Finding" below.</p>` : ""}
    </div>
  `;
}

function renderWorkflowRow(finding, item, isSynthetic, cfg, isAutoFixable, helperCols = []) {
  const disabled = isSynthetic || item.status !== "PENDING";
  const keyCell = item.key_field ? wrapTableColumnRef(finding.table_name, item.key_field, "") : "";
  const detailsCell = linkifyTableColumnRefs(escapeHtml(item.issue_detail || "")) +
    (isAutoFixable && item.status === "PENDING" ? `
      <button class="btn-autofill" data-autofill-item="${escapeHtml(item.id)}" data-autofill-val="${escapeHtml(finding.auto_fix_value)}">
        ⚡ Autofill '${escapeHtml(finding.auto_fix_value)}'
      </button>` : "");

  const whyButton = !isSynthetic && item.id
    ? `<div><button type="button" class="btn-why" data-why-item="${escapeHtml(item.id)}" aria-expanded="false">Why flagged?</button></div>` : "";

  let dispositionCell;
  if (cfg.mode === "decision") {
    dispositionCell = `<span class="badge status-${escapeHtml(item.status)}">${escapeHtml(item.status)}</span>`;
  } else {
    const verdict = item.review_verdict || "PENDING";
    if (disabled) {
      const tone = VERDICT_TONE[verdict] || "warning";
      dispositionCell = `<span class="verdict-badge verdict-tone-${tone}">${escapeHtml(verdict)}</span>`;
    } else {
      dispositionCell = cfg.dispositions.map((d) => verdictButton(item.id, d.verdict, d.label)).join(" ");
    }
  }

  return `
    <tr data-item-id="${escapeHtml(item.id || "")}" class="item-row status-${escapeHtml(item.status)}${isSynthetic ? " synthetic-row" : ""}">
      ${cfg.mode === "decision" ? `<td><input type="checkbox" class="item-checkbox" ${disabled ? "disabled" : ""}></td>` : ""}
      <td>${escapeHtml(item.row_index ?? "-")}</td>
      <td>${keyCell}${item.key_value ? `: ${escapeHtml(item.key_value)}` : ""}</td>
      <td>${detailsCell}${whyButton}</td>
      ${helperCols.map((c) => helperCell(finding.id, item.id, c)).join("")}
      ${cfg.showCorrectedInput ? `<td><input type="text" class="corrected-input" placeholder="Enter value..." value="${escapeHtml(item.corrected_data || "")}" ${disabled ? "disabled" : ""}></td>` : ""}
      <td>${dispositionCell}</td>
    </tr>
  `;
}

// ---------------------------------------------------------------------------
// "Why flagged?": the exact reason for one record, and an optional local-model paraphrase
// ---------------------------------------------------------------------------

function renderPlainLanguage(data) {
  const plain = data.plain_language;
  if (plain) {
    return `<p class="why-plain">${escapeHtml(plain.text)}</p>
      <p class="hint-text">AI-generated by the local model${plain.created_at ? ` · ${escapeHtml(formatIST(plain.created_at))}` : ""} ·
      check it against the exact reason above. <button type="button" class="link-btn" data-why-plain="regen">Write again</button></p>`;
  }
  if (data.plain_language_enabled) {
    return `<button type="button" class="btn-why-ai" data-why-plain="new">Explain in plain language</button>
      <span class="hint-text"> Written by the local model on this machine; the record's values never leave it. Can take a minute.</span>`;
  }
  return `<span class="hint-text">Plain-language explanation is off. Set <code>explain.local_llm.enabled: true</code> in config.yaml
    to let the local model write one (it then reads this record's values, on this machine only).</span>`;
}

function renderWhy(data) {
  const rule = data.rule;
  const kind = rule.kind === "BUILT_IN" ? "Built-in SAP rule" : "Check proposed by the LLM";
  const evidence = data.evidence.length ? `
    <table class="why-evidence">
      <thead><tr><th>Column</th><th>What it is</th><th>Value in this record</th></tr></thead>
      <tbody>${data.evidence.map((e) => `
        <tr>
          <td><strong>${escapeHtml(e.column)}</strong></td>
          <td>${escapeHtml(e.description || "")}${e.data_type ? ` <span class="why-type">${escapeHtml(e.data_type)}</span>` : ""}
              ${e.concept ? `<div class="hint-text">${escapeHtml(e.concept)}${e.why ? ` - ${escapeHtml(e.why)}` : ""}</div>` : ""}</td>
          <td>${e.value ? `<code>${escapeHtml(e.value)}</code>` : '<span class="blank-cell">— (empty)</span>'}</td>
        </tr>`).join("")}
      </tbody>
    </table>` : `<p class="hint-text">${escapeHtml(data.note || "This record's values are not available.")}</p>`;
  return `
    <div class="why-panel" data-why-for="${escapeHtml(data.item_id)}">
      <div class="why-grid">
        <section><h4>What was found</h4><p>${escapeHtml(data.what || "")}</p></section>
        <section><h4>The rule</h4>
          <p><span class="why-kind">${escapeHtml(kind)}</span>${rule.family ? ` · ${escapeHtml(rule.family)}` : ""}
            ${rule.id ? `<code class="why-rule-id">${escapeHtml(rule.id)}</code>` : ""}</p>
          <p>${escapeHtml(rule.statement || "")}</p>
          ${rule.impact ? `<p class="why-impact"><strong>Why it matters:</strong> ${escapeHtml(rule.impact)}</p>` : ""}
          ${rule.kind === "LLM_CHECK" ? '<p class="hint-text">The check code is under "View Local Pandas Check Code" above.</p>' : ""}
        </section>
      </div>
      <section><h4>Evidence for this record</h4>${evidence}${data.stale && data.note ? `<p class="hint-text">${escapeHtml(data.note)}</p>` : ""}</section>
      <section class="why-ai"><h4>In plain language</h4><div class="why-ai-body">${renderPlainLanguage(data)}</div></section>
    </div>`;
}

async function generatePlainLanguage(panel, itemId, force) {
  const body = panel.querySelector(".why-ai-body");
  body.innerHTML = '<span class="hint-text">Writing with the local model… the first one loads the model and can take a minute.</span>';
  try {
    const result = await fetchJSON(
      `${API_BASE}/finding-items/${encodeURIComponent(itemId)}/explanation/plain-language${force ? "?force=true" : ""}`,
      { method: "POST" });
    body.innerHTML = renderPlainLanguage({ plain_language: { text: result.text, created_at: new Date().toISOString() }, plain_language_enabled: true });
  } catch (error) {
    body.innerHTML = `<span class="run-explorer-error">${escapeHtml(error.message)}</span>
      <button type="button" class="link-btn" data-why-plain="new">Try again</button>`;
  }
  attachPlainHandler(panel, itemId);
}

function attachPlainHandler(panel, itemId) {
  panel.querySelectorAll("[data-why-plain]").forEach((btn) =>
    btn.addEventListener("click", () => generatePlainLanguage(panel, itemId, btn.dataset.whyPlain === "regen")));
}

async function toggleWhy(button) {
  const row = button.closest("tr");
  const open = row.nextElementSibling;
  if (open && open.classList.contains("why-row")) {
    open.remove();
    button.setAttribute("aria-expanded", "false");
    return;
  }
  const whyRow = document.createElement("tr");
  whyRow.className = "why-row";
  const cell = document.createElement("td");
  cell.colSpan = row.children.length;
  cell.innerHTML = '<div class="why-panel"><p class="hint-text">Loading…</p></div>';
  whyRow.appendChild(cell);
  row.after(whyRow);
  button.setAttribute("aria-expanded", "true");
  const itemId = button.dataset.whyItem;
  try {
    const data = await fetchJSON(`${API_BASE}/finding-items/${encodeURIComponent(itemId)}/explanation`);
    cell.innerHTML = renderWhy(data);
    attachPlainHandler(cell.querySelector(".why-panel"), itemId);
  } catch (error) {
    cell.innerHTML = `<div class="why-panel"><p class="run-explorer-error">Could not load the explanation: ${escapeHtml(error.message)}</p></div>`;
  }
}

function attachPillarWorkflowHandlers(findingId, finding, isSynthetic) {
  if (isSynthetic) return;
  const section = document.getElementById("lazySectionRecords");
  section.querySelectorAll("[data-why-item]").forEach((btn) => btn.addEventListener("click", () => toggleWhy(btn)));

  section.querySelectorAll("[data-bulk]").forEach((button) => {
    button.addEventListener("click", async () => {
      const status = button.dataset.bulk;
      const checkedRows = section.querySelectorAll(".item-checkbox:checked");

      if (checkedRows.length === 0) {
        alert("Select at least one pending item first.");
        return;
      }

      for (const checkbox of checkedRows) {
        const row = checkbox.closest("tr");
        const itemId = row.dataset.itemId;
        const correctedInput = row.querySelector(".corrected-input");
        const correctedData = correctedInput ? correctedInput.value : "";

        await fetchJSON(`${API_BASE}/finding-items/${encodeURIComponent(itemId)}/decision`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status, corrected_data: correctedData }),
        });
      }

      await loadFindings();
      await reopenRecordsSection(findingId, finding);
    });
  });

  section.querySelectorAll("[data-autofill-item]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const itemId = btn.dataset.autofillItem;
      const fixVal = btn.dataset.autofillVal;
      await fetchJSON(`${API_BASE}/finding-items/${encodeURIComponent(itemId)}/autofill`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ fix_value: fixVal }),
      });
      await loadFindings();
      await reopenRecordsSection(findingId, finding);
    });
  });

  const bulkAutofillBtn = section.querySelector("#bulkAutofillBtn");
  if (bulkAutofillBtn && finding.auto_fix_value) {
    bulkAutofillBtn.addEventListener("click", async () => {
      const pendingRows = section.querySelectorAll(".item-row.status-PENDING");
      for (const row of pendingRows) {
        const itemId = row.dataset.itemId;
        if (itemId) {
          await fetchJSON(`${API_BASE}/finding-items/${encodeURIComponent(itemId)}/autofill`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ fix_value: finding.auto_fix_value }),
          });
        }
      }
      await loadFindings();
      await reopenRecordsSection(findingId, finding);
    });
  }

  section.querySelectorAll("[data-verdict]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const itemId = btn.dataset.itemId;
      const verdict = btn.dataset.verdict;
      const row = btn.closest("tr");
      const correctedInput = row ? row.querySelector(".corrected-input") : null;
      const correctedData = correctedInput ? correctedInput.value : "";
      try {
        await fetchJSON(`${API_BASE}/finding-items/${encodeURIComponent(itemId)}/verdict`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ verdict, corrected_data: correctedData }),
        });
        await loadFindings();
        await reopenRecordsSection(findingId, finding);
      } catch (err) {
        alert(`Failed to set verdict: ${err.message}`);
      }
    });
  });
}

// ---------------------------------------------------------------------------
// Duplicates: one cluster at a time. The recommended golden record (highest
// record quality score, explorer_agent/survivorship.py) is pre-selected: the
// Unique record the others merge into. Every other record is a Duplicate of it,
// unless the reviewer marks it Unique = a separate entity (look-alike).
// Nothing is saved until "Accept"; "To be confirmed" parks the whole cluster.
// Either locks the cluster; the ↺ button undoes the last action.
// ---------------------------------------------------------------------------

const DUP_FILTERS = [
  { key: "all", label: "All groups", test: () => true },
  { key: "open", label: "Needs review", test: (g) => g.members.some((m) => isOpenVerdict(m.review_verdict)) },
  { key: "EXACT", label: "Exact", test: (g) => g.match_type === "EXACT" },
  { key: "PROBABLE", label: "Probable", test: (g) => g.match_type === "PROBABLE" },
  { key: "SIMILAR", label: "Similar", test: (g) => g.match_type === "SIMILAR" },
];

const SCORE_PARTS = { completeness: "Completeness", active: "Active", usage: "Usage", recency: "Recency" };

let dupReview = null; // { findingId, groups, filter, drafts }
let findingsDirty = false; // reload the card list when the modal closes

function isOpenVerdict(verdict) {
  return !verdict || verdict === "PENDING" || verdict === "TO_BE_CONFIRMED";
}

function reviewerName() {
  try { return localStorage.getItem("reviewerName") || ""; } catch { return ""; }
}

async function loadDuplicateReview(findingId) {
  const container = document.getElementById("duplicateReview");
  try {
    const groups = await fetchJSON(`${API_BASE}/findings/${encodeURIComponent(findingId)}/duplicate-groups`);
    await loadHelper(findingId);
    const keepFilter = dupReview?.findingId === findingId ? dupReview.filter : "all";
    dupReview = { findingId, groups, filter: keepFilter, drafts: {} };
    groups.forEach((g) => { dupReview.drafts[g.duplicate_group_id] = initialDraft(g); });
    renderDuplicateReview();
  } catch (error) {
    container.innerHTML = `<p class="empty-state">Unable to load duplicate groups: ${escapeHtml(error.message)}</p>`;
  }
}

// What the cluster shows before the reviewer touches it: the saved decision if
// there is one (this run or remembered), else the recommendation, else nothing.
function initialDraft(group) {
  const members = group.members;
  const decided = members.some((m) => m.review_verdict === "UNIQUE" || m.review_verdict === "DUPLICATE");
  if (decided) {
    const uniques = members.filter((m) => m.review_verdict === "UNIQUE");
    const survivor = uniques.find((m) => m.is_golden_record === 1)
      || (uniques.length === 1 && members.some((m) => m.review_verdict === "DUPLICATE") ? uniques[0] : null);
    return {
      survivor: survivor ? survivor.id : null,
      separate: new Set(uniques.filter((m) => !survivor || m.id !== survivor.id).map((m) => m.id)),
      touched: false,
    };
  }
  return { survivor: group.recommended_survivor_id || null, separate: new Set(), touched: false };
}

function draftVerdict(draft, member) {
  if (draft.survivor === member.id) return "KEEP";
  if (draft.separate.has(member.id)) return "UNIQUE";
  return draft.survivor ? "DUPLICATE" : "PENDING";
}

function renderDuplicateReview() {
  const container = document.getElementById("duplicateReview");
  if (!container || !dupReview) return;
  const { groups } = dupReview;

  if (groups.length === 0) {
    container.innerHTML = `
      <div class="dup-empty">
        <strong>No duplicate groups to review in this finding.</strong>
        <p class="hint-text">Groups a reviewer already decided in earlier runs are not shown again. Older,
        LLM-generated duplicate checks did not save row-level matches - re-run the explorer to get reviewable groups.</p>
      </div>`;
    return;
  }

  const members = groups.flatMap((g) => g.members);
  const counts = { DUPLICATE: 0, UNIQUE: 0, TO_BE_CONFIRMED: 0, PENDING: 0 };
  members.forEach((m) => { counts[m.review_verdict in counts ? m.review_verdict : "PENDING"] += 1; });
  const decided = counts.DUPLICATE + counts.UNIQUE;
  const pct = (n) => (members.length ? (n / members.length) * 100 : 0);
  const judged = groups.filter((g) => g.accepted_as_recommended !== null && g.accepted_as_recommended !== undefined);
  const kept = judged.filter((g) => g.accepted_as_recommended).length;

  const filter = DUP_FILTERS.find((f) => f.key === dupReview.filter) || DUP_FILTERS[0];
  const visible = groups.filter(filter.test);

  container.innerHTML = `
    <div class="dup-summary">
      <div class="dup-summary-top">
        <div class="dup-summary-title">
          <strong>${groups.length}</strong> duplicate group${groups.length === 1 ? "" : "s"} ·
          <strong>${members.length}</strong> records ·
          <strong>${decided}</strong> decided
          ${judged.length ? ` · recommendation kept in <strong>${kept}/${judged.length}</strong>` : ""}
        </div>
        <div class="dup-legend">
          <span><i class="legend-dot tone-negative"></i>Duplicate ${counts.DUPLICATE}</span>
          <span><i class="legend-dot tone-positive"></i>Unique ${counts.UNIQUE}</span>
          <span><i class="legend-dot tone-warning"></i>To confirm ${counts.TO_BE_CONFIRMED}</span>
          <span><i class="legend-dot tone-pending"></i>Pending ${counts.PENDING}</span>
        </div>
      </div>
      <div class="dup-progress" role="img" aria-label="${decided} of ${members.length} records decided">
        <span class="tone-negative" style="width:${pct(counts.DUPLICATE)}%"></span>
        <span class="tone-positive" style="width:${pct(counts.UNIQUE)}%"></span>
        <span class="tone-warning" style="width:${pct(counts.TO_BE_CONFIRMED)}%"></span>
      </div>
      <div class="dup-filters">
        ${DUP_FILTERS.map((f) => {
          const n = groups.filter(f.test).length;
          return `<button class="dup-filter ${f.key === filter.key ? "active" : ""}" data-dup-filter="${f.key}" ${n === 0 && f.key !== "all" ? "disabled" : ""}>
            ${escapeHtml(f.label)} <span>${n}</span></button>`;
        }).join("")}
        <label class="reviewer-field">Reviewer
          <input id="reviewerName" type="text" placeholder="your name" value="${escapeHtml(reviewerName())}">
        </label>
      </div>
    </div>
    ${renderClientMemoryNote()}
    ${helperNote(dupReview.findingId)}
    <p class="hint-text dup-hint"><strong>★ Golden record</strong> is the record the others merge into (pre-selected:
      highest quality score). Every other record is a <em>Duplicate</em> of it unless you mark it <em>Unique</em> - a
      separate entity. Nothing is saved until you click <strong>Accept</strong>, which locks the cluster;
      <strong>↺</strong> undoes it. Highlighted cells are values the records share.</p>
    <div class="dup-groups">
      ${visible.length ? visible.map(renderDuplicateGroup).join("") : `<p class="empty-state">No groups match this filter.</p>`}
    </div>
  `;
  attachDuplicateReviewHandlers();
}

function scoreCell(m) {
  if (m.quality_score === null || m.quality_score === undefined) return '<span class="blank-cell">—</span>';
  let parts = {};
  try { parts = JSON.parse(m.score_breakdown || "{}"); } catch { parts = {}; }
  const tip = Object.entries(parts).map(([k, v]) => `${SCORE_PARTS[k] || k}: ${Math.round(v * 100)}%`).join(" · ");
  return `<span class="quality-score" data-tooltip="${escapeHtml(tip)}">${Math.round(m.quality_score)}</span>`;
}

function actionText(m, verdict, survivorKey) {
  if (verdict === "KEEP") return "Golden record";
  if (verdict === "UNIQUE") return "Separate entity";
  if (verdict !== "DUPLICATE") return m.suggested_action === "CONFIRM_DUPLICATE_FIRST" ? "Confirm duplicate first" : "";
  const block = (m.suggested_action || "").startsWith("BLOCK");
  return block ? `Block & delete (dup. of ${survivorKey})` : `Merge into ${survivorKey}`;
}

function renderDuplicateGroup(group) {
  const draft = dupReview.drafts[group.duplicate_group_id];
  const keyField = group.members[0]?.key_field || "Key";
  const columns = [];
  group.members.forEach((m) => Object.keys(m.record || {}).forEach((c) => { if (!columns.includes(c)) columns.push(c); }));
  const legacy = columns.length === 0; // rows saved before record_data existed
  // Helper columns the comparison doesn't already show.
  const helperCols = legacy ? [] : helperColumnsFor(dupReview.findingId, columns);

  // A value is highlighted when another record in the same group has it too.
  const valueCounts = {};
  columns.forEach((c) => {
    valueCounts[c] = {};
    group.members.forEach((m) => {
      const v = String(m.record?.[c] ?? "").trim().toLowerCase();
      if (v) valueCounts[c][v] = (valueCounts[c][v] || 0) + 1;
    });
  });
  const reasons = [...new Set(group.members.flatMap((m) =>
    (m.match_reasons || "").split(/(?:^|;\s)vs [^:]+:\s/).map((r) => r.trim()).filter(Boolean)))];
  const typeClass = { EXACT: "sim-exact", PROBABLE: "sim-probable", SIMILAR: "sim-similar" }[group.match_type] || "sim-similar";
  const open = group.members.filter((m) => isOpenVerdict(m.review_verdict)).length;
  const parked = group.members.every((m) => m.review_verdict === "TO_BE_CONFIRMED");
  const survivor = group.members.find((m) => m.id === draft.survivor);
  const allUnique = !draft.survivor && group.members.every((m) => draft.separate.has(m.id));
  const canAccept = Boolean(draft.survivor) || allUnique;
  const recommended = group.members.find((m) => m.id === group.recommended_survivor_id);
  const status = open === 0
    ? (group.accepted_as_recommended === false ? "✓ accepted (recommendation changed)" : "✓ accepted")
    : parked ? "to be confirmed" : `${open} open`;
  // Accepted or parked clusters are read-only until the reviewer undoes the action.
  const locked = open === 0 || parked;
  const lockAttr = locked ? "disabled" : "";

  return `
    <section class="dup-group ${open === 0 ? "is-done" : ""} ${locked ? "is-locked" : ""}" data-group-id="${escapeHtml(group.duplicate_group_id)}">
      <div class="dup-group-header">
        <div class="dup-group-title">
          <span class="group-id-title">${escapeHtml(group.duplicate_group_id)}</span>
          <span class="similarity-badge ${typeClass}">${escapeHtml(group.match_type)} · ${Number(group.similarity_score ?? 0)}%</span>
          <span class="dup-group-count">${group.member_count} records · ${status}</span>
        </div>
        <div class="dup-group-actions">
          ${locked
            ? `<span class="lock-note">🔒 ${parked ? "Parked" : "Locked"}</span>
               <button class="btn-undo" data-group-undo aria-label="Undo - back to the previous state"
                 data-tooltip="Undo - back to the previous state">↺</button>`
            : `<button class="btn-action btn-tone-warning" data-group-tbc>To be confirmed</button>
               <button class="btn-approve" data-group-accept ${canAccept ? "" : "disabled"}
                 title="${canAccept ? "Save these decisions and lock the cluster" : "Choose the golden record, or mark every record Unique"}">Accept</button>`}
        </div>
      </div>
      ${reasons.length ? `<div class="group-reasons-note"><strong>Why matched:</strong> ${reasons.map((r) => escapeHtml(r)).join("<br>")}</div>` : ""}
      ${recommended ? `<div class="recommendation-note">Recommended golden record: <strong>${escapeHtml(recommended.key_value)}</strong>
          (quality score ${Math.round(recommended.quality_score ?? 0)}) - most complete, active, used and recent record.</div>`
        : group.match_type === "SIMILAR" && open ? `<div class="recommendation-note">Similar match only - these may be look-alikes.
          Confirm they are duplicates before choosing a golden record.</div>` : ""}
      <div class="dup-table-wrap">
        <table class="dup-table">
          <thead>
            <tr>
              <th>${escapeHtml(keyField)}</th>
              <th title="Record quality score 0-100 (hover a score for its parts)">Score</th>
              ${legacy ? "<th>Details</th>" : columns.map((c) => `
                <th><span class="sap-ref" data-table="${escapeHtml(currentDupTable())}" data-column="${escapeHtml(c)}" data-context="">${escapeHtml(c)}</span></th>`).join("")}
              ${helperCols.map(helperHeader).join("")}
              <th>Action</th>
              <th class="dup-decision-col">Decision</th>
            </tr>
          </thead>
          <tbody>
            ${group.members.map((m) => {
              const verdict = draftVerdict(draft, m);
              const rowClass = { KEEP: "UNIQUE", UNIQUE: "UNIQUE", DUPLICATE: "DUPLICATE" }[verdict] || "PENDING";
              return `
                <tr class="dup-row verdict-${rowClass} ${verdict === "KEEP" ? "is-survivor" : ""}" data-item-id="${escapeHtml(m.id)}">
                  <td class="dup-key">${escapeHtml(m.key_value)}
                    ${m.id === group.recommended_survivor_id ? '<span class="recommended-tag" data-tooltip="Recommended golden record">★</span>' : ""}
                    ${m.decision_source === "REMEMBERED" && !isOpenVerdict(m.review_verdict)
                      ? `<span class="remembered-tag" aria-label="Remembered decision" data-tooltip="${escapeHtml(m.reviewer_comment || "Remembered from an earlier review")}">↺</span>`
                      : ""}</td>
                  <td>${scoreCell(m)}</td>
                  ${legacy
                    ? `<td>${escapeHtml(m.issue_detail || "")}</td>`
                    : columns.map((c) => {
                        const raw = String(m.record?.[c] ?? "");
                        const shared = raw.trim() && valueCounts[c][raw.trim().toLowerCase()] > 1;
                        return `<td class="${shared ? "match-cell" : ""}">${raw ? escapeHtml(raw) : '<span class="blank-cell">—</span>'}</td>`;
                      }).join("")}
                  ${helperCols.map((c) => helperCell(dupReview.findingId, m.id, c)).join("")}
                  <td class="dup-action">${escapeHtml(actionText(m, verdict, survivor?.key_value || ""))}</td>
                  <td class="dup-decision-col">
                    <div class="verdict-seg" role="group" aria-label="Decision for ${escapeHtml(m.key_value)}">
                      <button class="seg-btn tone-positive ${verdict === "KEEP" ? "active" : ""}" data-draft="KEEP" ${lockAttr}
                        aria-pressed="${verdict === "KEEP"}" title="Unique - the golden record the others merge into">★ Golden record</button>
                      <button class="seg-btn tone-positive ${verdict === "UNIQUE" ? "active" : ""}" data-draft="UNIQUE" ${lockAttr}
                        aria-pressed="${verdict === "UNIQUE"}" title="Unique - a separate entity, not a duplicate">Unique</button>
                      <button class="seg-btn tone-negative ${verdict === "DUPLICATE" ? "active" : ""}" data-draft="DUPLICATE"
                        aria-pressed="${verdict === "DUPLICATE"}" ${!locked && draft.survivor && verdict !== "KEEP" ? "" : "disabled"}
                        title="Duplicate of the golden record">Duplicate</button>
                    </div>
                  </td>
                </tr>`;
            }).join("")}
          </tbody>
        </table>
      </div>
    </section>
  `;
}

function currentDupRun() {
  const finding = currentFindings.find((f) => f.id === dupReview?.findingId);
  return finding ? RUNS_BY_ID[finding.run_id] : null;
}

// Tells the reviewer that their decisions become this client's knowledge.
function renderClientMemoryNote() {
  const run = currentDupRun();
  if (!run?.client_name) return "";
  const exportUrl = `${API_BASE}/clients/${encodeURIComponent(run.client_id)}/duplicate-decisions.csv`;
  return `
    <div class="client-memory-note">
      🧠 Accepted decisions are remembered for <strong>${escapeHtml(run.client_name)}</strong>: a fully decided group
      is not shown again in future runs (unless a record changes or a new duplicate joins it), and records all marked
      <em>Unique</em> are never grouped again.
      <a href="${exportUrl}" download>Export decisions (CSV merge map)</a>
    </div>`;
}

function currentDupTable() {
  return currentFindings.find((f) => f.id === dupReview?.findingId)?.table_name || "";
}

function rerenderKeepingScroll() {
  const scroller = document.querySelector("#detailModal .modal-content");
  const scrollTop = scroller ? scroller.scrollTop : 0;
  renderDuplicateReview();
  if (scroller) scroller.scrollTop = scrollTop;
}

function attachDuplicateReviewHandlers() {
  const container = document.getElementById("duplicateReview");

  container.querySelectorAll("[data-dup-filter]").forEach((btn) => {
    btn.addEventListener("click", () => {
      dupReview.filter = btn.dataset.dupFilter;
      renderDuplicateReview();
    });
  });

  const reviewerInput = container.querySelector("#reviewerName");
  reviewerInput?.addEventListener("change", () => {
    try { localStorage.setItem("reviewerName", reviewerInput.value.trim()); } catch { /* private window */ }
  });

  // Draft changes only - nothing is saved until Accept.
  container.querySelectorAll(".dup-row [data-draft]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const groupId = btn.closest(".dup-group").dataset.groupId;
      const itemId = btn.closest(".dup-row").dataset.itemId;
      const draft = dupReview.drafts[groupId];
      const choice = btn.dataset.draft;
      draft.touched = true;
      if (choice === "KEEP") {
        // Moving the survivor: the new one leaves the "separate" set, the old one becomes a Duplicate.
        draft.separate.delete(itemId);
        draft.survivor = itemId;
      } else if (choice === "UNIQUE") {
        if (draft.survivor === itemId) draft.survivor = null;
        draft.separate.add(itemId);
      } else {
        draft.separate.delete(itemId);
      }
      rerenderKeepingScroll();
    });
  });

  container.querySelectorAll("[data-group-accept]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const groupId = btn.closest(".dup-group").dataset.groupId;
      const draft = dupReview.drafts[groupId];
      await saveGroup(groupId, `accept`, {
        survivor_item_id: draft.survivor,
        separate_item_ids: [...draft.separate],
        reviewer: reviewerName(),
      }, (m) => {
        m.review_verdict = m.id === draft.survivor || draft.separate.has(m.id) ? "UNIQUE" : "DUPLICATE";
        m.is_golden_record = m.id === draft.survivor ? 1 : 0;
      });
    });
  });

  container.querySelectorAll("[data-group-undo]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const groupId = btn.closest(".dup-group").dataset.groupId;
      let result;
      try {
        result = await fetchJSON(
          `${API_BASE}/findings/${encodeURIComponent(dupReview.findingId)}/duplicate-groups/${encodeURIComponent(groupId)}/undo`,
          { method: "POST" },
        );
      } catch (error) {
        alert(`Failed to undo: ${error.message}`);
        return;
      }
      const i = dupReview.groups.findIndex((g) => g.duplicate_group_id === groupId);
      if (result.group && i >= 0) dupReview.groups[i] = result.group;
      dupReview.drafts[groupId] = initialDraft(dupReview.groups[i]);
      findingsDirty = true;
      refreshFindingStatus(dupReview.findingId);
      rerenderKeepingScroll();
    });
  });

  container.querySelectorAll("[data-group-tbc]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const groupId = btn.closest(".dup-group").dataset.groupId;
      await saveGroup(groupId, `verdict`, { verdict: "TO_BE_CONFIRMED", reviewer: reviewerName() },
        (m) => { m.review_verdict = "TO_BE_CONFIRMED"; });
    });
  });
}

// Saves one cluster, then updates local state and re-renders in place (keeping
// the modal's scroll position) instead of re-fetching every group.
async function saveGroup(groupId, endpoint, body, applyLocally) {
  try {
    await fetchJSON(
      `${API_BASE}/findings/${encodeURIComponent(dupReview.findingId)}/duplicate-groups/${encodeURIComponent(groupId)}/${endpoint}`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
    );
  } catch (error) {
    alert(`Failed to save decision: ${error.message}`);
    await loadDuplicateReview(dupReview.findingId);
    return;
  }
  const group = dupReview.groups.find((g) => g.duplicate_group_id === groupId);
  group.members.forEach((m) => { applyLocally(m); m.decision_source = "HUMAN"; });
  if (endpoint === "accept" && group.recommended_survivor_id) {
    group.accepted_as_recommended = group.recommended_survivor_id === body.survivor_item_id;
  }
  dupReview.drafts[groupId] = initialDraft(group);
  findingsDirty = true;
  refreshFindingStatus(dupReview.findingId);
  rerenderKeepingScroll();
}

// ---------------------------------------------------------------------------
// Record readiness (dashboard panel) and the DQ Index (mini window behind the
// small "DQ" chip under Promote). Both from /api/scorecard (explorer_agent/scorecard.py).
// Meters: the fill carries the status band, the track is a lighter step of the
// same hue, and the value is always printed next to it (never color alone).
// ---------------------------------------------------------------------------

const PILLAR_INFO = {
  completeness: { label: "Completeness", unit: "mandatory cells filled" },
  correctness: { label: "Correctness", unit: "checked cells without a defect" },
  uniqueness: { label: "Uniqueness", unit: "records that are not redundant duplicates" },
  activeness: { label: "Activeness", unit: "records neither deleted nor dormant" },
};
let scorecardData = null;

function band(score, bands) {
  const b = bands || { good: 0.95, fair: 0.85 };
  if (score === null || score === undefined) return { key: "na", label: "n/a", icon: "·" };
  if (score >= b.good) return { key: "good", label: "Good", icon: "✓" };
  if (score >= b.fair) return { key: "fair", label: "Fair", icon: "!" };
  return { key: "poor", label: "Poor", icon: "✕" };
}

const pct = (v, digits = 1) => (v === null || v === undefined ? "n/a" : `${(v * 100).toFixed(digits)}%`);
const num = (v) => Number(v || 0).toLocaleString();

function meterBar(score, bands, tip) {
  const b = band(score, bands);
  return `<div class="sc-meter sc-${b.key}" role="meter" aria-valuemin="0" aria-valuemax="100"
      aria-valuenow="${Math.round((score || 0) * 100)}" aria-label="${escapeHtml(tip)}"><span style="width:${((score || 0) * 100).toFixed(1)}%"></span></div>`;
}

function deltaText(now, before, unit = "pts") {
  if (now === null || now === undefined || before === null || before === undefined) return "";
  const d = (now - before) * 100;
  if (Math.abs(d) < 0.05) return `<span class="sc-delta">±0.0 ${unit}</span>`;
  return `<span class="sc-delta ${d > 0 ? "up" : "down"}">${d > 0 ? "▲" : "▼"} ${Math.abs(d).toFixed(1)} ${unit}</span>`;
}

async function loadScorecard() {
  const runId = document.getElementById("runSelect").value;
  const params = new URLSearchParams({ client_id: selectedClientId() });
  if (runId) params.append("run_id", runId);
  try {
    scorecardData = await fetchJSON(`${API_BASE}/scorecard?${params.toString()}`);
  } catch {
    scorecardData = null;
  }
  const chips = { dqIndexBtn: scorecardData?.overall?.dq_index,
                  readinessBtn: scorecardData?.overall?.details?.readiness?.score };
  const labels = { dqIndexBtn: "DQ Index", readinessBtn: "Record readiness" };
  for (const [id, value] of Object.entries(chips)) {
    const chip = document.getElementById(id);
    if (!chip) continue;
    chip.classList.toggle("hidden", !scorecardData);
    chip.innerHTML = `<span aria-hidden="true">${id === "dqIndexBtn" ? "▦" : "◔"}</span> ${labels[id]} <strong>${pct(value)}</strong>`;
  }
  if (!scorecardData) { closeMiniWindows(); return; }
  if (!document.getElementById("dqWindow").classList.contains("hidden")) renderDqWindow();
  if (!document.getElementById("readinessWindow").classList.contains("hidden")) renderReadinessWindow();
}

// ----------------------------------------------------------------- readiness mini window

function readinessWindowRow(entry, kind) {
  const r = entry.details?.readiness || {};
  const b = band(r.score, scorecardData.readiness_bands);
  const tip = `${num(r.ready)} of ${num(r.in_scope)} in-scope records can be loaded as they are`
    + (r.out_of_scope ? ` · ${num(r.out_of_scope)} out of scope (marked for deletion or dormant)` : "")
    + ((r.top_reasons || []).length ? ` · top reason: ${r.top_reasons[0].reason} (${num(r.top_reasons[0].records)})` : "");
  const name = kind === "object"
    ? `<button class="sc-toggle" data-rw-object="${escapeHtml(entry.name)}" aria-expanded="false">▸</button> ${escapeHtml(entry.name)}`
    : `<span class="rw-table">${escapeHtml(entry.name)}</span>`;
  const view = kind === "table" && r.not_ready
    ? `<button class="btn-link" data-not-ready="${escapeHtml(entry.name)}">View</button>` : "";
  return `<tr class="${kind === "run" ? "dq-total" : ""}" ${kind === "table" ? `data-rw-parent="${escapeHtml(entry.object_name || "")}" hidden` : ""}>
      <td>${kind === "run" ? "All tables" : name}</td>
      <td class="dq-cell" data-tooltip="${escapeHtml(tip)}">${r.score === null || r.score === undefined ? "n/a"
        : `<span class="sc-badge sc-${b.key}"><i aria-hidden="true">${b.icon}</i>${pct(r.score)}</span>`}</td>
      <td>${num(r.not_ready)}</td>
      <td class="sc-muted">${num(r.out_of_scope)}</td>
      <td>${view}</td>
    </tr>`;
}

function renderReadinessWindow() {
  const win = document.getElementById("readinessWindow");
  const sc = scorecardData;
  if (!sc) return;
  const rows = (sc.objects || []).map((o) => readinessWindowRow(o, "object")
    + (sc.tables || []).filter((t) => t.object_name === o.name).map((t) => readinessWindowRow(t, "table")).join("")).join("");
  win.innerHTML = `
    <div class="dq-win-head">
      <strong>Record readiness</strong>
      <button class="dq-win-close" aria-label="Close">×</button>
    </div>
    <table class="dq-win-table">
      <thead><tr><th></th><th>Ready</th><th>Not ready</th><th class="sc-muted">Out of scope</th><th></th></tr></thead>
      <tbody>${rows}${sc.overall ? readinessWindowRow(sc.overall, "run") : ""}</tbody>
    </table>
    <p class="dq-win-note">Share of in-scope records that can be loaded as they are - no open defect from the built-in
      checks and not a duplicate that will be merged away. Deleted or dormant records are out of scope. Expand an object
      and click View for the records and their reasons.</p>`;
  win.querySelector(".dq-win-close").addEventListener("click", closeMiniWindows);
  win.querySelectorAll("[data-rw-object]").forEach((btn) => btn.addEventListener("click", () => {
    const open = btn.getAttribute("aria-expanded") !== "true";
    btn.setAttribute("aria-expanded", String(open));
    btn.textContent = open ? "▾" : "▸";
    win.querySelectorAll(`tr[data-rw-parent="${CSS.escape(btn.dataset.rwObject)}"]`).forEach((tr) => { tr.hidden = !open; });
  }));
  win.querySelectorAll("[data-not-ready]").forEach((btn) => btn.addEventListener("click", () => {
    closeMiniWindows();
    openNotReady(btn.dataset.notReady);
  }));
}

// The cleansing worklist of one table, in the detail modal: reasons as filters.
async function openNotReady(table) {
  const modal = document.getElementById("detailModal");
  const body = document.getElementById("modalBody");
  body.innerHTML = '<p class="empty-state">Loading...</p>';
  modal.classList.remove("hidden");
  let w;
  try {
    w = await fetchJSON(`${API_BASE}/scorecard/not-ready?client_id=${encodeURIComponent(selectedClientId())}`
      + `&table=${encodeURIComponent(table)}&run_id=${encodeURIComponent(scorecardData.run_id)}`);
  } catch (error) {
    body.innerHTML = `<p class="empty-state">Unable to load: ${escapeHtml(error.message)}</p>`;
    return;
  }
  let filter = null;
  const render = () => {
    const list = filter ? w.worklist.filter((x) => x.reasons.includes(filter)) : w.worklist;
    body.innerHTML = `
      <h2>${escapeHtml(table)} - records not ready</h2>
      <p class="hint-text">${num(w.not_ready)} of ${num(w.in_scope)} in-scope records cannot be loaded as they are
        (readiness ${pct(w.score)}). ${w.unlisted ? `${num(w.unlisted)} more are beyond the stored list.` : ""}
        Click a reason to filter.</p>
      <div class="nr-reasons">
        ${(w.top_reasons || []).map((x) => `<button class="dup-filter ${filter === x.reason ? "active" : ""}" data-reason="${escapeHtml(x.reason)}">
          ${escapeHtml(x.reason)} <span>${num(x.records)}</span></button>`).join("")}
        ${filter ? '<button class="dup-filter" data-reason="">Show all</button>' : ""}
      </div>
      <div class="dup-table-wrap">
        <table class="dup-table nr-table">
          <thead><tr><th>Key</th><th>Why it is not ready</th></tr></thead>
          <tbody>${list.map((x) => `<tr><td class="dup-key">${escapeHtml(x.key)}</td>
            <td>${x.reasons.map((r) => `<span class="nr-reason">${escapeHtml(r)}</span>`).join("")}</td></tr>`).join("")}</tbody>
        </table>
      </div>`;
    body.querySelectorAll("[data-reason]").forEach((btn) => btn.addEventListener("click", () => {
      filter = btn.dataset.reason || null;
      render();
    }));
  };
  render();
}

// ----------------------------------------------------------------- DQ Index mini window

const MINI_WINDOWS = { dqIndexBtn: "dqWindow", readinessBtn: "readinessWindow" };

function closeMiniWindows() {
  for (const [chipId, winId] of Object.entries(MINI_WINDOWS)) {
    document.getElementById(winId)?.classList.add("hidden");
    document.getElementById(chipId)?.setAttribute("aria-expanded", "false");
  }
}

function renderDqWindow() {
  const win = document.getElementById("dqWindow");
  const sc = scorecardData;
  if (!sc) return;
  const bands = sc.bands;
  const w = sc.weights || {};
  const weightText = Object.entries(w).filter(([, v]) => v > 0).map(([k, v]) => `${PILLAR_INFO[k]?.label || k} ${Math.round(v * 100)}%`).join(" · ");
  const cell = (e, p) => {
    const x = e.details?.pillars?.[p];
    if (!x) return '<td class="sc-na">n/a</td>';
    const tip = `${PILLAR_INFO[p].label}: ${num(x.total - x.bad)} of ${num(x.total)} ${PILLAR_INFO[p].unit}`;
    return `<td class="dq-cell" data-tooltip="${escapeHtml(tip)}">${pct(x.score)}</td>`;
  };
  const row = (e, cls) => {
    const b = band(e.dq_index, bands);
    return `<tr class="${cls}"><td>${escapeHtml(e.name)}</td>${["completeness", "correctness", "uniqueness", "activeness"].map((p) => cell(e, p)).join("")}
      <td><span class="sc-badge sc-${b.key}"><i aria-hidden="true">${b.icon}</i>${pct(e.dq_index)}</span></td></tr>`;
  };
  win.innerHTML = `
    <div class="dq-win-head">
      <strong>DQ Index</strong>
      <button class="dq-win-close" aria-label="Close">×</button>
    </div>
    <table class="dq-win-table">
      <thead><tr><th></th><th>Compl.</th><th>Correct.</th><th>Unique.</th><th class="sc-muted">Active.*</th><th>Index</th></tr></thead>
      <tbody>${(sc.objects || []).map((o) => row(o, "")).join("")}${sc.overall ? row(sc.overall, "dq-total") : ""}</tbody>
    </table>
    <p class="dq-win-note">Share of cells / records that pass the built-in checks. Index = ${escapeHtml(weightText)}.
      *Activeness is shown, not weighted. Hover a value for its counts.</p>`;
  win.querySelector(".dq-win-close").addEventListener("click", closeMiniWindows);
}

function initDqWindow() {
  // Two small chips under "Promote Approved Skills", each opening its mini window;
  // opening one closes the other. Esc or a click outside closes them.
  const renderers = { dqIndexBtn: renderDqWindow, readinessBtn: renderReadinessWindow };
  for (const [chipId, winId] of Object.entries(MINI_WINDOWS)) {
    const chip = document.getElementById(chipId);
    const win = document.getElementById(winId);
    if (!chip || !win) continue;
    chip.addEventListener("click", (event) => {
      event.stopPropagation();
      const wasOpen = !win.classList.contains("hidden");
      closeMiniWindows();
      if (wasOpen) return;
      renderers[chipId]();
      win.classList.remove("hidden");
      chip.setAttribute("aria-expanded", "true");
    });
  }
  document.addEventListener("click", (event) => {
    const inside = Object.entries(MINI_WINDOWS).some(([chipId, winId]) =>
      document.getElementById(winId)?.contains(event.target) || event.target.closest?.(`#${chipId}`));
    if (!inside) closeMiniWindows();
  });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeMiniWindows(); });
}

// ---------------------------------------------------------------------------
// Overall finding decision (not shown for DUPLICATE - see openDetail)
// ---------------------------------------------------------------------------

// A finding has a decision of its own only when approving it means something: a reusable LLM check that can be
// promoted into the skill library (that approval is the human gate before reuse). Built-in rule findings and
// duplicates can never become skills, so their status simply follows the record decisions above - the server
// marks the finding reviewed once every record is decided (episodic_store._sync_finding_status).
function isPromotable(finding) {
  return Boolean(finding.reusable) && Boolean(finding.has_check_code);
}

function renderFindingDecisionRow(finding) {
  if (!isPromotable(finding) && finding.item_count > 0) {
    return `<p class="hint-text finding-status-note">This finding's status follows your decisions on the records above:
      it is marked reviewed once every record is decided.</p>`;
  }
  const promotable = isPromotable(finding);
  const decided = finding.status !== "PENDING";
  const reviewedAt = formatIST(finding.reviewed_at);
  const comment = finding.reviewer_comment
    ? ` - ${escapeHtml(finding.reviewer_comment)}`
    : "";

  return `
    <div class="detail-row finding-decision-row">
      <div class="detail-label">${promotable ? "Reusable check" : "Finding decision"}</div>
      ${promotable ? `<p class="hint-text">Approving adds this check to the pool that <strong>Promote Approved Skills</strong>
        turns into a reusable skill for future runs. It is separate from your decisions on the individual records.</p>` : ""}
      ${decided
        ? `<span class="decided-note">${promotable ? "Decided" : "Reviewed"} ${reviewedAt}${comment}</span>`
        : `
          <div class="decision-buttons">
            <button class="btn-approve" data-finding-action="approve">${promotable ? "Approve as reusable check" : "Approve Finding"}</button>
            <button class="btn-reject" data-finding-action="reject">${promotable ? "Reject check" : "Reject Finding"}</button>
          </div>
        `}
    </div>
  `;
}

function attachFindingDecisionHandlers(findingId) {
  document.querySelectorAll("[data-finding-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      const status = button.dataset.findingAction === "approve" ? "APPROVED" : "REJECTED";
      await submitDecision(findingId, status);
      document.getElementById("detailModal").classList.add("hidden");
    });
  });
}

// ---------------------------------------------------------------------------
// CR4: Run Explorer Agent (background subprocess + polling)
// ---------------------------------------------------------------------------

async function loadConfigOptions() {
  try {
    return await fetchJSON(`${API_BASE}/config/options`);
  } catch (error) {
    console.error("Failed to load config options:", error);
    return null;
  }
}

async function openRunExplorerModal() {
  const options = await loadConfigOptions();
  const modalBody = document.getElementById("runExplorerModalBody");

  if (!options) {
    modalBody.innerHTML = `<p class="empty-state">Unable to load configuration options.</p>`;
    document.getElementById("runExplorerModal").classList.remove("hidden");
    return;
  }

  await loadWorkspace(); // pick up any change made on page 1 in another tab
  const changeLink = `<a href="./?client=${encodeURIComponent(CLIENT_ID)}">Change client / data</a>`;
  if (!WORKSPACE.ready) {
    modalBody.innerHTML = `
      <p class="empty-state">${escapeHtml(WORKSPACE.client.name)} needs a data dictionary and at least one table
        before a run can start. ${changeLink}</p>`;
    document.getElementById("runExplorerModal").classList.remove("hidden");
    return;
  }

  modalBody.innerHTML = `
    <div class="run-workspace-summary">
      <div><span class="rws-label">Client</span><strong>${escapeHtml(WORKSPACE.client.name)}</strong></div>
      <div><span class="rws-label">Data dictionary</span>${escapeHtml(WORKSPACE.dictionary.file)}</div>
      <div><span class="rws-label">Tables</span>
        ${WORKSPACE.tables.map((t) => `<span class="rws-table" title="${t.rows} rows · ${t.columns} columns">${escapeHtml(t.table)}</span>`).join("")}
      </div>
      <p class="hint-text">These come from page 1 and can't be changed here. ${changeLink}</p>
    </div>
    <div class="form-row">
      <label for="reProvider">LLM Provider</label>
      <select id="reProvider">
        ${options.llm_providers.map((p) => `<option value="${escapeHtml(p)}" ${p === options.default_llm_provider ? "selected" : ""}>${escapeHtml(p)}</option>`).join("")}
      </select>
    </div>
    <div class="form-row checkbox-row">
      <label class="checkbox-inline" data-tooltip="Only run the built-in duplicate detection. No LLM calls, so no API cost.">
        <input type="checkbox" id="reDuplicatesOnly"> Duplicates only (no LLM)
      </label>
      <label class="checkbox-inline" data-tooltip="Run the profiling process from fresh data instead of reusing previously generated cached results">
        <input type="checkbox" id="reNoCache"> No Cache
      </label>
    </div>
    <details class="advanced-options">
      <summary>Advanced</summary>
      <div class="form-row">
        <label for="reModel">Model (optional - provider default if blank)</label>
        <input type="text" id="reModel" placeholder="${escapeHtml(options.gemini_model_default)}">
      </div>
      <div class="form-row">
        <label for="reTemperature">Temperature (optional)</label>
        <input type="number" step="0.1" id="reTemperature">
      </div>
      <div class="form-row">
        <label for="reRepairRounds" title="Failed planner checks are sent back to the planner once per round, in one call. 0 = off.">Repair rounds (0 = off)</label>
        <input type="number" min="0" id="reRepairRounds" value="${options.default_max_repair_rounds}">
      </div>
    </details>
    <div id="runExplorerError" class="hint-text run-explorer-error"></div>
    <div class="run-explorer-actions">
      <button id="submitRunExplorerBtn" class="btn-run-explorer">Run Explorer Agent</button>
    </div>
  `;

  document.getElementById("submitRunExplorerBtn").addEventListener("click", submitRunExplorer);
  document.getElementById("runExplorerModal").classList.remove("hidden");
}

async function submitRunExplorer() {
  const modelVal = document.getElementById("reModel").value.trim();
  const tempVal = document.getElementById("reTemperature").value;
  const repairVal = document.getElementById("reRepairRounds").value;

  const body = {
    client_id: CLIENT_ID, // the server resolves data folder, dictionary and tables from this client's workspace
    llm_provider: document.getElementById("reProvider").value,
    no_cache: document.getElementById("reNoCache").checked,
    duplicates_only: document.getElementById("reDuplicatesOnly").checked,
  };
  if (modelVal) body.model = modelVal;
  if (tempVal !== "") body.temperature = parseFloat(tempVal);
  if (repairVal !== "") body.max_repair_rounds = parseInt(repairVal, 10);

  const errorEl = document.getElementById("runExplorerError");
  errorEl.textContent = "";
  try {
    const result = await fetchJSON(`${API_BASE}/jobs/run-explorer`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    document.getElementById("runExplorerModal").classList.add("hidden");
    startJobPolling(result.job_id);
  } catch (error) {
    errorEl.textContent = error.message;
  }
}

const RUN_BUTTON_STATES = {
  idle: { html: "Run", title: "Run the Explorer Agent" },
  // "Running" (with spinner) flips to "Stop" on hover and to "Offline" when
  // status polling can't reach the server - see .btn-stop-explorer in style.css.
  running: {
    html: `<span class="lbl-running"><span class="spinner"></span>Running</span>`
      + `<span class="lbl-offline">Offline</span><span class="lbl-stop">Stop</span>`,
    title: "Stop the Explorer Agent run",
  },
  stopping: { html: `<span class="lbl-running"><span class="spinner"></span>Stopping</span>`, title: "Stopping..." },
};

// Only rebuilds the button when the state changes, so the spinner animation
// isn't restarted by every 2-second status poll.
function setRunButtonState(state) {
  const runBtn = document.getElementById("runExplorerBtn");
  runBtn.disabled = state === "stopping";
  runBtn.classList.toggle("btn-run-explorer", state === "idle");
  runBtn.classList.toggle("btn-stop-explorer", state !== "idle");
  if (runBtn.dataset.state === state) return;
  runBtn.dataset.state = state;
  runBtn.innerHTML = RUN_BUTTON_STATES[state].html;
  runBtn.title = RUN_BUTTON_STATES[state].title;
}

function setJobOffline(offline) {
  const runBtn = document.getElementById("runExplorerBtn");
  runBtn.classList.toggle("is-offline", offline && !!currentJobId);
  if (currentJobId) {
    runBtn.title = offline ? "Connection lost - retrying..." : RUN_BUTTON_STATES[runBtn.dataset.state].title;
  }
}

// Swaps the header Run button into a Stop button while a job runs, and shows
// a grey/green/red dot on its corner once the job has finished.
function renderJobStatus(job) {
  const runBtn = document.getElementById("runExplorerBtn");
  const statusEl = document.getElementById("runStatus");
  const running = job?.status === "RUNNING";

  currentJobId = running ? job.job_id : null;
  if (!running) stopRequested = false;
  setRunButtonState(running ? "running" : "idle");
  setJobOffline(false);

  if (!job || running) {
    statusEl.className = "run-status-dot hidden";
    delete statusEl.dataset.tooltip;
    return;
  }

  const outcome = {
    COMPLETED: { tone: "success", label: "Last run succeeded" },
    FAILED: { tone: "failure", label: "Last run failed" },
    STOPPED: { tone: "stopped", label: "Last run stopped" },
  }[job.status] || { tone: "stopped", label: `Last run: ${job.status}` };

  statusEl.className = `run-status-dot status-dot-${outcome.tone}`;
  statusEl.dataset.tooltip = outcome.label;
}

async function stopRunningJob() {
  if (!currentJobId) return;
  if (!confirm("Stop the running Explorer Agent?")) return;
  stopRequested = true;
  setRunButtonState("stopping");
  try {
    await fetchJSON(`${API_BASE}/jobs/${encodeURIComponent(currentJobId)}/stop`, { method: "POST" });
  } catch (error) {
    // 409 means the job finished on its own in the meantime; polling picks that up.
    console.error("Failed to stop job:", error);
    stopRequested = false;
    setRunButtonState("running");
  }
}

function startJobPolling(jobId) {
  if (jobPollInterval) clearInterval(jobPollInterval);
  const poll = async () => {
    let job;
    try {
      job = await fetchJSON(`${API_BASE}/jobs/${encodeURIComponent(jobId)}`);
    } catch (error) {
      if (String(error.message).includes("(404)")) {
        // Server restarted and no longer knows this job - nothing left to track.
        clearInterval(jobPollInterval);
        jobPollInterval = null;
        renderJobStatus(null);
      } else {
        // Network/server hiccup: show "Offline" and keep retrying until it's back.
        setJobOffline(true);
      }
      return;
    }
    setJobOffline(false);
    // Keep the "Stopping" button state until the process actually exits.
    if (!(job.status === "RUNNING" && stopRequested)) renderJobStatus(job);
    if (job.status !== "RUNNING") {
      clearInterval(jobPollInterval);
      jobPollInterval = null;
      await loadRuns();
      await loadFindings();
    }
  };
  jobPollNow = poll;
  poll();
  jobPollInterval = setInterval(poll, 2000);
}

async function resumeJobIfRunning() {
  try {
    const job = await fetchJSON(`${API_BASE}/jobs/current`);
    if (job.status === "RUNNING") {
      startJobPolling(job.job_id);
    } else {
      renderJobStatus(job);
    }
  } catch (error) {
    // No job has ever been run in this process - nothing to resume.
  }
}

// ---------------------------------------------------------------------------
// Static event wiring
// ---------------------------------------------------------------------------

document.getElementById("closeModal").addEventListener("click", () => {
  document.getElementById("detailModal").classList.add("hidden");
  dupReview = null;
  if (findingsDirty) {
    findingsDirty = false;
    loadFindings(); // refresh the cards' review progress
  }
});

document.getElementById("runSelect").addEventListener("change", () => { updateRunUsage(); loadFindings(); });
document.getElementById("statusFilter").addEventListener("change", loadFindings);
document.getElementById("scopeFilter").addEventListener("change", loadFindings);
document.getElementById("sourceFilter").addEventListener("change", loadFindings);
document.getElementById("refreshBtn").addEventListener("click", loadFindings);

document.querySelectorAll("#categoryTabs .tab-btn").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll("#categoryTabs .tab-btn").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    activeCategory = tab.dataset.category;
    loadFindings();
  });
});

document.getElementById("promoteBtn").addEventListener("click", async () => {
  try {
    const result = await fetchJSON(`${API_BASE}/promote`, { method: "POST" });
    alert(`Promoted: ${result.promoted_count ?? 0} / Evaluated: ${result.candidates_evaluated ?? 0}`);
    await loadFindings();
  } catch (error) {
    alert(`Promotion failed: ${error.message}`);
  }
});

document.getElementById("runExplorerBtn").addEventListener("click", (event) => {
  if (event.currentTarget.classList.contains("is-offline")) return; // can't reach the server to stop
  if (currentJobId) {
    stopRunningJob();
  } else {
    openRunExplorerModal();
  }
});

// React to the browser's own connectivity signal right away instead of
// waiting for the next poll to fail/succeed.
window.addEventListener("offline", () => setJobOffline(true));
window.addEventListener("online", () => {
  if (jobPollInterval && jobPollNow) jobPollNow();
});
document.getElementById("closeRunExplorerModal").addEventListener("click", () => {
  document.getElementById("runExplorerModal").classList.add("hidden");
});

(async function init() {
  initDqWindow();
  // Page 2 only makes sense for a chosen, existing client - otherwise back to page 1.
  if (!CLIENT_ID) {
    window.location.replace("./");
    return;
  }
  try {
    await loadWorkspace();
  } catch (error) {
    window.location.replace("./");
    return;
  }
  try {
    await loadDictionary();
    await loadRuns();
    await loadFindings();
    await resumeJobIfRunning();
  } catch (error) {
    console.error("Failed to initialize the review app:", error);
  }
})();
