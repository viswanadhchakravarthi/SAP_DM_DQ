const API_BASE = "/api";

let currentFindings = [];
let activeCategory = "";
let DICTIONARY = { tables: {}, columns: {} };
let fullFindingCache = {};
let jobPollInterval = null;

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

// ---------------------------------------------------------------------------
// CR1/CR2: hover tooltips for TABLE.COLUMN references and pillar tabs
// ---------------------------------------------------------------------------

async function loadDictionary() {
  try {
    DICTIONARY = await fetchJSON(`${API_BASE}/dictionary`);
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

async function loadRuns() {
  const runs = await fetchJSON(`${API_BASE}/runs`);
  const select = document.getElementById("runSelect");

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

        const startedAt = run.started_at ? run.started_at.slice(0, 19) : "Unknown time";
        return `<option value="${escapeHtml(run.run_id)}">${escapeHtml(startedAt)} - ${escapeHtml(tableNames)}</option>`;
      })
      .join("");
}

async function loadStats(runId) {
  const params = runId ? `?run_id=${encodeURIComponent(runId)}` : "";
  const stats = await fetchJSON(`${API_BASE}/stats${params}`);

  const cats = stats.categories || {};
  document.getElementById("statsBar").innerHTML = `
    <span>Total: <span class="count">${stats.TOTAL ?? 0}</span></span>
    <span>Pending: <span class="count">${stats.PENDING ?? 0}</span></span>
    <span>Approved: <span class="count">${stats.APPROVED ?? 0}</span></span>
    <span>Rejected: <span class="count">${stats.REJECTED ?? 0}</span></span>
    <span style="border-left: 1px solid #2d3142; padding-left: 14px;">Activeness: <span class="count">${cats.ACTIVENESS ?? 0}</span></span>
    <span>Duplicates: <span class="count">${cats.DUPLICATE ?? 0}</span></span>
    <span>Completeness: <span class="count">${cats.COMPLETENESS ?? 0}</span></span>
    <span>Correctness: <span class="count">${cats.CORRECTNESS ?? 0}</span></span>
    ${stats.anomalies ? `<span>Anomalies: <span class="count">${stats.anomalies}</span></span>` : ""}
  `;
}

async function loadFindings() {
  const runId = document.getElementById("runSelect").value;
  const status = document.getElementById("statusFilter").value;
  const scope = document.getElementById("scopeFilter").value;
  const params = new URLSearchParams();

  if (runId) params.append("run_id", runId);
  if (status) params.append("status", status);
  if (scope) params.append("rule_scope", scope);

  if (activeCategory === "ANOMALIES") {
    params.append("is_anomaly", "true");
  } else if (activeCategory) {
    params.append("category", activeCategory);
  }

  const container = document.getElementById("findingsContainer");
  container.innerHTML = '<p class="empty-state">Loading findings...</p>';

  try {
    // Light payload only (no check_code/raw_result) - see CR3 lazy loading.
    const findings = await fetchJSON(`${API_BASE}/findings?${params.toString()}`);
    currentFindings = findings;

    if (findings.length === 0) {
      container.innerHTML = '<p class="empty-state">No findings match this filter.</p>';
      await loadStats(runId);
      return;
    }

    container.innerHTML = findings.map(renderCard).join("");
    attachCardHandlers();
    await loadStats(runId);
  } catch (error) {
    container.innerHTML = `<p class="empty-state">Unable to load findings: ${escapeHtml(error.message)}</p>`;
  }
}

function renderCard(finding) {
  const decided = finding.status !== "PENDING";
  const reviewedAt = finding.reviewed_at
    ? new Date(finding.reviewed_at).toLocaleString()
    : "";
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
    <div class="finding-card" data-id="${escapeHtml(finding.id)}">
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
        <span>${finding.created_at ? new Date(finding.created_at).toLocaleString() : ""}</span>
      </div>
      <div class="action-row">
        <button class="btn-detail" data-action="detail">
          ${finding.category === 'DUPLICATE' ? '👥 Review Duplicate Clusters' : 'View Details'}
        </button>
        ${decided ? `<span class="decided-note">Reviewed ${reviewedAt}${comment}</span>` : ""}
      </div>
    </div>
  `;
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
    <div class="detail-row lazy-section" id="lazySectionMetrics">
      <button class="btn-lazy-load" data-lazy="metrics">View Aggregate Metrics / Profile Results</button>
    </div>
    <div class="detail-row lazy-section" id="lazySectionCode">
      <button class="btn-lazy-load" data-lazy="code">View Local Pandas Check Code</button>
    </div>
    <div class="detail-row lazy-section" id="lazySectionRecords">
      <button class="btn-lazy-load" data-lazy="records">${isDuplicate ? '👥 Review Duplicate Clusters' : 'View Individual Records'}</button>
    </div>
    ${isDuplicate ? "" : renderFindingDecisionRow(finding)}
  `;

  attachLazyLoadHandlers(id, finding);
  if (!isDuplicate) {
    attachFindingDecisionHandlers(id);
  }
  document.getElementById("detailModal").classList.remove("hidden");
}

function attachLazyLoadHandlers(id, finding) {
  const metricsBtn = document.querySelector('#lazySectionMetrics [data-lazy="metrics"]');
  if (metricsBtn) {
    metricsBtn.addEventListener("click", async () => {
      const full = await fetchFullFinding(id);
      let rawResultPretty = full.raw_result || "";
      try {
        rawResultPretty = JSON.stringify(JSON.parse(rawResultPretty), null, 2);
      } catch {
        // Non-JSON string
      }
      document.getElementById("lazySectionMetrics").innerHTML = `
        <div class="detail-label">Aggregate Metric / Profile Result</div>
        <pre>${escapeHtml(rawResultPretty)}</pre>
      `;
    });
  }

  const codeBtn = document.querySelector('#lazySectionCode [data-lazy="code"]');
  if (codeBtn) {
    codeBtn.addEventListener("click", async () => {
      const full = await fetchFullFinding(id);
      document.getElementById("lazySectionCode").innerHTML = `
        <div class="detail-label">Local Pandas Check Code</div>
        <pre>${escapeHtml(full.check_code || "(not captured)")}</pre>
      `;
    });
  }

  const recordsBtn = document.querySelector('#lazySectionRecords [data-lazy="records"]');
  if (recordsBtn) {
    recordsBtn.addEventListener("click", async () => {
      document.getElementById("lazySectionRecords").innerHTML = '<p class="hint-text">Loading...</p>';
      if (finding.category === "DUPLICATE") {
        await reopenDuplicateClusters(id, finding);
      } else {
        await reopenRecordsSection(id, finding);
      }
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

async function reopenDuplicateClusters(findingId, finding) {
  const groups = await fetchJSON(`${API_BASE}/findings/${encodeURIComponent(findingId)}/duplicate-groups`);
  document.getElementById("lazySectionRecords").innerHTML = renderDuplicateClustersView(finding, groups);
  attachDuplicateClusterHandlers(findingId, finding);
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
// Duplicates: per-record + cluster-level verdicts (golden record removed)
// ---------------------------------------------------------------------------

function renderDuplicateClustersView(finding, groups) {
  if (!groups || groups.length === 0) {
    return `<p class="hint-text">No duplicate clusters identified in this run.</p>`;
  }

  return `
    <div class="detail-row">
      <div class="detail-label">Duplicate Clusters (${groups.length} Groups)</div>
      <p class="hint-text" style="margin-bottom: 12px;">
        Mark individual records as <strong>Duplicate</strong>, <strong>Unique</strong>, or <strong>To Be Confirmed</strong>,
        or flag the entire cluster as needing business review.
      </p>
      <div class="duplicate-clusters-container">
        ${groups.map((group) => {
          const simScore = group.similarity_score ?? 100;
          let simClass = "sim-similar";
          if (simScore >= 99) simClass = "sim-exact";
          else if (simScore >= 80) simClass = "sim-probable";

          return `
            <div class="duplicate-group-card" data-group-id="${escapeHtml(group.duplicate_group_id)}">
              <div class="duplicate-group-header">
                <div class="group-title-area">
                  <span class="group-id-title">${escapeHtml(group.duplicate_group_id)}</span>
                  <span class="similarity-badge ${simClass}">${simScore}% ${escapeHtml(group.match_type)}</span>
                  <span style="font-size: 12px; color: #94a3b8;">(${group.member_count} candidates)</span>
                </div>
                <button class="btn-action btn-tone-warning" data-cluster-verdict="TO_BE_CONFIRMED" data-group-id="${escapeHtml(group.duplicate_group_id)}">
                  🟡 Mark Entire Cluster as To Be Confirmed
                </button>
              </div>

              ${group.members[0]?.match_reasons ? `
                <div class="group-reasons-note">
                  <strong>Matching Criteria:</strong> ${escapeHtml(group.members[0].match_reasons)}
                </div>
              ` : ''}

              <div class="candidates-list">
                ${group.members.map((member) => {
                  const wasGolden = member.is_golden_record === 1;
                  const verdict = member.review_verdict || "PENDING";
                  return `
                    <div class="candidate-card ${wasGolden ? 'is-golden' : ''}" data-item-id="${escapeHtml(member.id)}">
                      <div class="candidate-info">
                        <div class="candidate-key-row">
                          <span class="candidate-key">${escapeHtml(member.key_field)}: ${escapeHtml(member.key_value)}</span>
                          ${wasGolden ? '<span class="golden-tag">Previously Marked Golden (legacy)</span>' : ''}
                          <span class="verdict-badge verdict-tone-${VERDICT_TONE[verdict] || 'warning'}">${escapeHtml(verdict)}</span>
                        </div>
                        <div class="candidate-details">${linkifyTableColumnRefs(escapeHtml(member.issue_detail || ""))}</div>
                      </div>

                      <div class="candidate-action-bar">
                        <button class="btn-action btn-tone-negative" data-verdict="DUPLICATE" data-item-id="${escapeHtml(member.id)}">🔴 Duplicate</button>
                        <button class="btn-action btn-tone-positive" data-verdict="UNIQUE" data-item-id="${escapeHtml(member.id)}">🟢 Unique</button>
                        <button class="btn-action btn-tone-warning" data-verdict="TO_BE_CONFIRMED" data-item-id="${escapeHtml(member.id)}">🟡 To Be Confirmed</button>
                      </div>
                    </div>
                  `;
                }).join("")}
              </div>
            </div>
          `;
        }).join("")}
      </div>
    </div>
  `;
}

function attachDuplicateClusterHandlers(findingId, finding) {
  const section = document.getElementById("lazySectionRecords");

  section.querySelectorAll("[data-cluster-verdict]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const groupId = btn.dataset.groupId;
      const verdict = btn.dataset.clusterVerdict;
      try {
        await fetchJSON(`${API_BASE}/findings/${encodeURIComponent(findingId)}/duplicate-groups/${encodeURIComponent(groupId)}/verdict`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ verdict }),
        });
        await reopenDuplicateClusters(findingId, finding);
        await loadFindings();
      } catch (err) {
        alert(`Failed to update cluster: ${err.message}`);
      }
    });
  });

  section.querySelectorAll("[data-verdict]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const itemId = btn.dataset.itemId;
      const verdict = btn.dataset.verdict;
      try {
        await fetchJSON(`${API_BASE}/finding-items/${encodeURIComponent(itemId)}/verdict`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ verdict }),
        });
        await reopenDuplicateClusters(findingId, finding);
        await loadFindings();
      } catch (err) {
        alert(`Failed to set verdict: ${err.message}`);
      }
    });
  });
}

// ---------------------------------------------------------------------------
// Overall finding decision (not shown for DUPLICATE - see openDetail)
// ---------------------------------------------------------------------------

function renderFindingDecisionRow(finding) {
  const decided = finding.status !== "PENDING";
  const reviewedAt = finding.reviewed_at
    ? new Date(finding.reviewed_at).toLocaleString()
    : "";
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

  modalBody.innerHTML = `
    <div class="form-row">
      <label for="reDataDir">Data Directory</label>
      <input type="text" id="reDataDir" value="${escapeHtml(options.default_data_dir)}">
    </div>
    <div class="form-row">
      <label>Tables (leave all unchecked to profile every table)</label>
      <div class="checkbox-group">
        ${options.tables.map((t) => `
          <label class="checkbox-inline"><input type="checkbox" class="re-table-check" value="${escapeHtml(t)}"> ${escapeHtml(t)}</label>
        `).join("")}
      </div>
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
        <label for="reDictionaryFile">Dictionary File</label>
        <input type="text" id="reDictionaryFile" value="${escapeHtml(options.default_dictionary_file)}">
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
  const dataDir = document.getElementById("reDataDir").value.trim();
  const tables = Array.from(document.querySelectorAll(".re-table-check:checked")).map((el) => el.value);
  const fallbackChecks = Array.from(document.querySelectorAll(".re-fallback-check"));
  const anyFallbackChecked = fallbackChecks.some((el) => el.checked);
  const fallbackProviders = anyFallbackChecked
    ? fallbackChecks.filter((el) => el.checked).map((el) => el.value)
    : null; // null = don't override config.yaml default
  const modelVal = document.getElementById("reModel").value.trim();
  const tempVal = document.getElementById("reTemperature").value;
  const dictVal = document.getElementById("reDictionaryFile").value.trim();
  const maxIterVal = document.getElementById("reMaxIterations").value;

  const body = {
    data_dir: dataDir,
    llm_provider: document.getElementById("reProvider").value,
    no_cache: document.getElementById("reNoCache").checked,
  };
  if (tables.length > 0) body.tables = tables;
  if (fallbackProviders !== null) body.fallback_providers = fallbackProviders;
  if (modelVal) body.model = modelVal;
  if (tempVal !== "") body.temperature = parseFloat(tempVal);
  if (dictVal) body.dictionary_file = dictVal;
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

function renderJobBanner(job) {
  const banner = document.getElementById("jobBanner");
  if (!job) {
    banner.classList.add("hidden");
    return;
  }
  banner.classList.remove("hidden");
  const statusLabel = { RUNNING: "Running...", COMPLETED: "Completed", FAILED: "Failed" }[job.status] || job.status;
  const tone = job.status === "RUNNING" ? "warning" : (job.status === "COMPLETED" ? "positive" : "negative");
  const lastLines = (job.log_tail || []).slice(-8).join("\n");
  banner.innerHTML = `
    <div class="job-banner-header">
      <span class="verdict-badge verdict-tone-${tone}">Explorer Agent: ${escapeHtml(statusLabel)}</span>
      <button id="jobBannerToggle" class="btn-lazy-load">Log</button>
    </div>
    <pre id="jobLogTail" class="job-log-pre hidden">${escapeHtml(lastLines)}</pre>
  `;
  document.getElementById("jobBannerToggle").addEventListener("click", () => {
    document.getElementById("jobLogTail").classList.toggle("hidden");
  });
}

function startJobPolling(jobId) {
  if (jobPollInterval) clearInterval(jobPollInterval);
  const poll = async () => {
    try {
      const job = await fetchJSON(`${API_BASE}/jobs/${encodeURIComponent(jobId)}`);
      renderJobBanner(job);
      if (job.status !== "RUNNING") {
        clearInterval(jobPollInterval);
        jobPollInterval = null;
        await loadRuns();
        await loadFindings();
      }
    } catch (error) {
      console.error("Job polling failed:", error);
      clearInterval(jobPollInterval);
      jobPollInterval = null;
    }
  };
  poll();
  jobPollInterval = setInterval(poll, 2000);
}

async function resumeJobIfRunning() {
  try {
    const job = await fetchJSON(`${API_BASE}/jobs/current`);
    if (job.status === "RUNNING") {
      startJobPolling(job.job_id);
    } else {
      renderJobBanner(job);
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

document.getElementById("runExplorerBtn").addEventListener("click", openRunExplorerModal);
document.getElementById("closeRunExplorerModal").addEventListener("click", () => {
  document.getElementById("runExplorerModal").classList.add("hidden");
});

(async function init() {
  try {
    await loadDictionary();
    await loadRuns();
    await loadFindings();
    await resumeJobIfRunning();
  } catch (error) {
    console.error("Failed to initialize the review app:", error);
  }
})();
