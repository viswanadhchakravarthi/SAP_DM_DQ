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
  const findings = await fetchJSON(`${API_BASE}/findings?${params.toString()}`);
  if (seq !== countsRequestSeq) return; // a newer filter change already won

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

async function loadFindings() {
  const runId = document.getElementById("runSelect").value;
  const status = document.getElementById("statusFilter").value;
  const scope = document.getElementById("scopeFilter").value;
  const params = new URLSearchParams();
  const container = document.getElementById("findingsContainer");

  if (runId) params.append("run_id", runId);
  params.append("client_id", selectedClientId());
  if (status) params.append("status", status);
  if (scope) params.append("rule_scope", scope);

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

async function reopenRecordsSection(findingId, finding) {
  const items = await fetchJSON(`${API_BASE}/findings/${encodeURIComponent(findingId)}/items`);
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
      <table class="items-table">
        <thead>
          <tr>
            ${cfg.mode === "decision" ? "<th></th>" : ""}
            <th>Row</th><th>Key Field</th><th>Details</th>
            ${cfg.showCorrectedInput ? `<th>${escapeHtml(cfg.correctedLabel || "Corrected Data")}</th>` : ""}
            <th>${cfg.mode === "decision" ? "Status" : "Disposition"}</th>
          </tr>
        </thead>
        <tbody>
          ${items.map((item) => renderWorkflowRow(finding, item, isSynthetic, cfg, isAutoFixable)).join("")}
        </tbody>
      </table>
      ${isSynthetic ? `<p class="hint-text">This check did not produce row-level detail. Use "Approve Finding" / "Reject Finding" below.</p>` : ""}
    </div>
  `;
}

function renderWorkflowRow(finding, item, isSynthetic, cfg, isAutoFixable) {
  const disabled = isSynthetic || item.status !== "PENDING";
  const keyCell = item.key_field ? wrapTableColumnRef(finding.table_name, item.key_field, "") : "";
  const detailsCell = linkifyTableColumnRefs(escapeHtml(item.issue_detail || "")) +
    (isAutoFixable && item.status === "PENDING" ? `
      <button class="btn-autofill" data-autofill-item="${escapeHtml(item.id)}" data-autofill-val="${escapeHtml(finding.auto_fix_value)}">
        ⚡ Autofill '${escapeHtml(finding.auto_fix_value)}'
      </button>` : "");

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
      <td>${detailsCell}</td>
      ${cfg.showCorrectedInput ? `<td><input type="text" class="corrected-input" placeholder="Enter value..." value="${escapeHtml(item.corrected_data || "")}" ${disabled ? "disabled" : ""}></td>` : ""}
      <td>${dispositionCell}</td>
    </tr>
  `;
}

function attachPillarWorkflowHandlers(findingId, finding, isSynthetic) {
  if (isSynthetic) return;
  const section = document.getElementById("lazySectionRecords");

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
// Duplicates: groups shown immediately, one Duplicate / Unique / To Be
// Confirmed decision per record (plus whole-group shortcuts)
// ---------------------------------------------------------------------------

const DUP_VERDICTS = [
  { verdict: "DUPLICATE", label: "Duplicate", tone: "negative" },
  { verdict: "UNIQUE", label: "Unique", tone: "positive" },
  { verdict: "TO_BE_CONFIRMED", label: "To Confirm", tone: "warning" },
];

const DUP_FILTERS = [
  { key: "all", label: "All groups", test: () => true },
  { key: "open", label: "Needs review", test: (g) => g.members.some((m) => isOpenVerdict(m.review_verdict)) },
  { key: "EXACT", label: "Exact", test: (g) => g.match_type === "EXACT" },
  { key: "PROBABLE", label: "Probable", test: (g) => g.match_type === "PROBABLE" },
  { key: "SIMILAR", label: "Similar", test: (g) => g.match_type === "SIMILAR" },
];

let dupReview = null; // { findingId, groups, filter }
let findingsDirty = false; // reload the card list when the modal closes

function isOpenVerdict(verdict) {
  return !verdict || verdict === "PENDING" || verdict === "TO_BE_CONFIRMED";
}

async function loadDuplicateReview(findingId) {
  const container = document.getElementById("duplicateReview");
  try {
    const groups = await fetchJSON(`${API_BASE}/findings/${encodeURIComponent(findingId)}/duplicate-groups`);
    dupReview = { findingId, groups, filter: dupReview?.findingId === findingId ? dupReview.filter : "all" };
    renderDuplicateReview();
  } catch (error) {
    container.innerHTML = `<p class="empty-state">Unable to load duplicate groups: ${escapeHtml(error.message)}</p>`;
  }
}

function renderDuplicateReview() {
  const container = document.getElementById("duplicateReview");
  if (!container || !dupReview) return;
  const { groups } = dupReview;

  if (groups.length === 0) {
    container.innerHTML = `
      <div class="dup-empty">
        <strong>No duplicate records were captured for this finding.</strong>
        <p class="hint-text">This finding was produced by an older, LLM-generated check that did not save row-level
        matches. Re-run the explorer (or use <em>Duplicates only</em> in Run) to get reviewable duplicate groups.</p>
      </div>`;
    return;
  }

  const members = groups.flatMap((g) => g.members);
  const counts = { DUPLICATE: 0, UNIQUE: 0, TO_BE_CONFIRMED: 0, PENDING: 0 };
  members.forEach((m) => { counts[m.review_verdict in counts ? m.review_verdict : "PENDING"] += 1; });
  const decided = counts.DUPLICATE + counts.UNIQUE;
  const pct = (n) => (members.length ? (n / members.length) * 100 : 0);

  const filter = DUP_FILTERS.find((f) => f.key === dupReview.filter) || DUP_FILTERS[0];
  const visible = groups.filter(filter.test);

  container.innerHTML = `
    <div class="dup-summary">
      <div class="dup-summary-top">
        <div class="dup-summary-title">
          <strong>${groups.length}</strong> duplicate group${groups.length === 1 ? "" : "s"} ·
          <strong>${members.length}</strong> records ·
          <strong>${decided}</strong> decided
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
      </div>
    </div>
    ${renderClientMemoryNote()}
    <p class="hint-text dup-hint">Highlighted cells are the values these records share. Decide each record, or use the group buttons.
      Click a selected decision again to clear it.</p>
    <div class="dup-groups">
      ${visible.length ? visible.map(renderDuplicateGroup).join("") : `<p class="empty-state">No groups match this filter.</p>`}
    </div>
  `;
  attachDuplicateReviewHandlers();
}

function renderDuplicateGroup(group) {
  const keyField = group.members[0]?.key_field || "Key";
  const columns = [];
  group.members.forEach((m) => Object.keys(m.record || {}).forEach((c) => { if (!columns.includes(c)) columns.push(c); }));
  const legacy = columns.length === 0; // rows saved before record_data existed

  // A value is highlighted when another record in the same group has it too.
  const valueCounts = {};
  columns.forEach((c) => {
    valueCounts[c] = {};
    group.members.forEach((m) => {
      const v = String(m.record?.[c] ?? "").trim().toLowerCase();
      if (v) valueCounts[c][v] = (valueCounts[c][v] || 0) + 1;
    });
  });
  // match_reasons is "vs <KEY> <value>: <reason>; vs ..." per record - the same
  // pair shows up once from each side, so strip the "vs ..." prefix and dedupe.
  const reasons = [...new Set(group.members.flatMap((m) =>
    (m.match_reasons || "").split(/(?:^|;\s)vs [^:]+:\s/).map((r) => r.trim()).filter(Boolean)))];
  const typeClass = { EXACT: "sim-exact", PROBABLE: "sim-probable", SIMILAR: "sim-similar" }[group.match_type] || "sim-similar";
  const open = group.members.filter((m) => isOpenVerdict(m.review_verdict)).length;

  return `
    <section class="dup-group ${open === 0 ? "is-done" : ""}" data-group-id="${escapeHtml(group.duplicate_group_id)}">
      <div class="dup-group-header">
        <div class="dup-group-title">
          <span class="group-id-title">${escapeHtml(group.duplicate_group_id)}</span>
          <span class="similarity-badge ${typeClass}">${escapeHtml(group.match_type)} · ${Number(group.similarity_score ?? 0)}%</span>
          <span class="dup-group-count">${group.member_count} records${open === 0 ? " · ✓ reviewed" : ` · ${open} open`}</span>
        </div>
        <div class="dup-group-actions">
          <span class="hint-text">Whole group:</span>
          ${DUP_VERDICTS.map((d) => `
            <button class="btn-action btn-tone-${d.tone}" data-group-verdict="${d.verdict}">${d.label}</button>`).join("")}
        </div>
      </div>
      ${reasons.length ? `<div class="group-reasons-note"><strong>Why matched:</strong> ${reasons.map((r) => escapeHtml(r)).join("<br>")}</div>` : ""}
      <div class="dup-table-wrap">
        <table class="dup-table">
          <thead>
            <tr>
              <th>${escapeHtml(keyField)}</th>
              ${legacy ? "<th>Details</th>" : columns.map((c) => `
                <th><span class="sap-ref" data-table="${escapeHtml(currentDupTable())}" data-column="${escapeHtml(c)}" data-context="">${escapeHtml(c)}</span></th>`).join("")}
              <th class="dup-decision-col">Decision</th>
            </tr>
          </thead>
          <tbody>
            ${group.members.map((m) => {
              const verdict = m.review_verdict || "PENDING";
              return `
                <tr class="dup-row verdict-${escapeHtml(verdict)}" data-item-id="${escapeHtml(m.id)}">
                  <td class="dup-key">${escapeHtml(m.key_value)}${m.decision_source === "REMEMBERED" && verdict !== "PENDING"
                    ? `<span class="remembered-tag" aria-label="Remembered decision" data-tooltip="${escapeHtml(m.reviewer_comment || "Remembered from an earlier review")}">↺</span>`
                    : ""}</td>
                  ${legacy
                    ? `<td>${escapeHtml(m.issue_detail || "")}</td>`
                    : columns.map((c) => {
                        const raw = String(m.record?.[c] ?? "");
                        const shared = raw.trim() && valueCounts[c][raw.trim().toLowerCase()] > 1;
                        return `<td class="${shared ? "match-cell" : ""}">${raw ? escapeHtml(raw) : '<span class="blank-cell">—</span>'}</td>`;
                      }).join("")}
                  <td class="dup-decision-col">
                    <div class="verdict-seg" role="group" aria-label="Decision for ${escapeHtml(m.key_value)}">
                      ${DUP_VERDICTS.map((d) => `
                        <button class="seg-btn tone-${d.tone} ${verdict === d.verdict ? "active" : ""}"
                          data-verdict="${d.verdict}" aria-pressed="${verdict === d.verdict}">${d.label}</button>`).join("")}
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
  return `
    <div class="client-memory-note">
      🧠 Decisions are remembered for <strong>${escapeHtml(run.client_name)}</strong>: records that are all marked
      <em>Unique</em> won't be grouped again; every other decision (e.g. <em>Duplicate</em> + <em>Unique</em> for
      the duplicate and its original) is pre-filled in future runs while the records stay unchanged.
    </div>`;
}

function currentDupTable() {
  return currentFindings.find((f) => f.id === dupReview?.findingId)?.table_name || "";
}

function attachDuplicateReviewHandlers() {
  const container = document.getElementById("duplicateReview");

  container.querySelectorAll("[data-dup-filter]").forEach((btn) => {
    btn.addEventListener("click", () => {
      dupReview.filter = btn.dataset.dupFilter;
      renderDuplicateReview();
    });
  });

  container.querySelectorAll(".dup-row .seg-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const row = btn.closest(".dup-row");
      const itemId = row.dataset.itemId;
      // Clicking the selected decision again clears it back to PENDING.
      const verdict = btn.classList.contains("active") ? "PENDING" : btn.dataset.verdict;
      await saveDuplicateVerdicts(
        () => fetchJSON(`${API_BASE}/finding-items/${encodeURIComponent(itemId)}/verdict`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ verdict }),
        }),
        (m) => m.id === itemId,
        verdict,
      );
    });
  });

  container.querySelectorAll("[data-group-verdict]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const groupId = btn.closest(".dup-group").dataset.groupId;
      const verdict = btn.dataset.groupVerdict;
      await saveDuplicateVerdicts(
        () => fetchJSON(
          `${API_BASE}/findings/${encodeURIComponent(dupReview.findingId)}/duplicate-groups/${encodeURIComponent(groupId)}/verdict`,
          { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ verdict }) },
        ),
        (m) => m.duplicate_group_id === groupId,
        verdict,
      );
    });
  });
}

// Saves, then updates local state and re-renders in place (keeping the
// modal's scroll position) instead of re-fetching every group.
async function saveDuplicateVerdicts(request, matchesMember, verdict) {
  const scroller = document.querySelector("#detailModal .modal-content");
  const scrollTop = scroller.scrollTop;
  try {
    await request();
  } catch (error) {
    alert(`Failed to save decision: ${error.message}`);
    await loadDuplicateReview(dupReview.findingId);
    return;
  }
  dupReview.groups.forEach((g) => g.members.forEach((m) => {
    if (matchesMember(m)) {
      m.review_verdict = verdict;
      m.decision_source = "HUMAN";
    }
  }));
  findingsDirty = true;
  renderDuplicateReview();
  scroller.scrollTop = scrollTop;
}

// ---------------------------------------------------------------------------
// Overall finding decision (not shown for DUPLICATE - see openDetail)
// ---------------------------------------------------------------------------

function renderFindingDecisionRow(finding) {
  const decided = finding.status !== "PENDING";
  const reviewedAt = formatIST(finding.reviewed_at);
  const comment = finding.reviewer_comment
    ? ` - ${escapeHtml(finding.reviewer_comment)}`
    : "";

  return `
    <div class="detail-row finding-decision-row">
      <div class="detail-label">Overall Finding Decision</div>
      ${decided
        ? `<span class="decided-note">Reviewed ${reviewedAt}${comment}</span>`
        : `
          <div class="decision-buttons">
            <button class="btn-approve" data-finding-action="approve">Approve Finding</button>
            <button class="btn-reject" data-finding-action="reject">Reject Finding</button>
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
    <div class="form-row">
      <label>Fallback Providers (leave all unchecked to keep config.yaml defaults)</label>
      <div class="checkbox-group">
        ${options.llm_providers.map((p) => `
          <label class="checkbox-inline"><input type="checkbox" class="re-fallback-check" value="${escapeHtml(p)}" ${options.default_fallback_providers.includes(p) ? "checked" : ""}> ${escapeHtml(p)}</label>
        `).join("")}
      </div>
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
        <label for="reMaxIterations">Max Iterations</label>
        <input type="number" id="reMaxIterations" value="${options.default_max_iterations}">
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
  const fallbackChecks = Array.from(document.querySelectorAll(".re-fallback-check"));
  const anyFallbackChecked = fallbackChecks.some((el) => el.checked);
  const fallbackProviders = anyFallbackChecked
    ? fallbackChecks.filter((el) => el.checked).map((el) => el.value)
    : null; // null = don't override config.yaml default
  const modelVal = document.getElementById("reModel").value.trim();
  const tempVal = document.getElementById("reTemperature").value;
  const maxIterVal = document.getElementById("reMaxIterations").value;

  const body = {
    client_id: CLIENT_ID, // the server resolves data folder, dictionary and tables from this client's workspace
    llm_provider: document.getElementById("reProvider").value,
    no_cache: document.getElementById("reNoCache").checked,
    duplicates_only: document.getElementById("reDuplicatesOnly").checked,
  };
  if (fallbackProviders !== null) body.fallback_providers = fallbackProviders;
  if (modelVal) body.model = modelVal;
  if (tempVal !== "") body.temperature = parseFloat(tempVal);
  if (maxIterVal !== "") body.max_iterations = parseInt(maxIterVal, 10);

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

document.getElementById("runSelect").addEventListener("change", loadFindings);
document.getElementById("statusFilter").addEventListener("change", loadFindings);
document.getElementById("scopeFilter").addEventListener("change", loadFindings);
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
