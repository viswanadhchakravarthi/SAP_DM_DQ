const API_BASE = "/api";

let currentFindings = [];
let activeCategory = "";

async function fetchJSON(url, options) {
  const res = await fetch(url, options);

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Request failed (${res.status}): ${text}`);
  }

  return res.json();
}

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
        <div class="table-col">${escapeHtml(finding.table_name)}.${escapeHtml(finding.column_name)}</div>
        <div style="display: flex; gap: 8px; flex-wrap: wrap;">
          <span class="badge badge-cat-${escapeHtml(finding.category)}">${escapeHtml(categoryLabel)}</span>
          <span class="badge ${scopeClass}">${escapeHtml(scopeLabel)}</span>
          ${finding.is_anomaly ? '<span class="badge badge-anomaly">⚡ Anomaly</span>' : ''}
          ${finding.fix_type === 'AUTO_FIXABLE' ? `<span class="badge badge-fix-AUTO">⚡ Auto-fixable: ${escapeHtml(finding.auto_fix_value || 'Default')}</span>` : ''}
          <span class="badge ${escapeHtml(finding.severity)}">${escapeHtml(finding.severity)}</span>
          <span class="badge status-${escapeHtml(finding.status)}">${escapeHtml(finding.status)}</span>
        </div>
      </div>
      <div class="summary">${escapeHtml(finding.result_summary)}</div>
      <div class="meta">
        <span>Confidence: ${escapeHtml(finding.confidence)}</span>
        <span>Scope: ${escapeHtml(finding.rule_scope)}</span>
        <span>Reusable Skill: ${finding.reusable ? "Yes" : "No"}</span>
        <span>${finding.created_at ? new Date(finding.created_at).toLocaleString() : ""}</span>
      </div>
      <div class="action-row">
        <button class="btn-detail" data-action="detail">
          ${finding.category === 'DUPLICATE' ? '👥 Review Duplicate Clusters & Golden Records' : 'View Details'}
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

async function openDetail(id) {
  const finding = currentFindings.find((item) => item.id === id);
  if (!finding) return;

  let rawResultPretty = finding.raw_result || "";
  try {
    rawResultPretty = JSON.stringify(JSON.parse(rawResultPretty), null, 2);
  } catch {
    // Non-JSON string
  }

  // Fetch items & duplicate groups
  const [realItems, duplicateGroups] = await Promise.all([
    fetchJSON(`${API_BASE}/findings/${encodeURIComponent(id)}/items`),
    fetchJSON(`${API_BASE}/findings/${encodeURIComponent(id)}/duplicate-groups`),
  ]);

  const isDuplicateCheck = finding.category === "DUPLICATE" || (duplicateGroups && duplicateGroups.length > 0 && duplicateGroups[0].duplicate_group_id !== "UNGROUPED");
  const usingSyntheticRow = realItems.length === 0;

  const items = usingSyntheticRow
    ? [{
        id: null,
        row_index: null,
        key_field: `${finding.table_name}.${finding.column_name}`,
        key_value: "",
        issue_detail: finding.result_summary,
        corrected_data: "",
        status: finding.status,
      }]
    : realItems;

  const categoryLabel = {
    ACTIVENESS: "🕒 Activeness Check",
    DUPLICATE: "👥 Duplicate Analysis & Golden Record",
    COMPLETENESS: "📋 Completeness Check",
    CORRECTNESS: "✅ Correctness Check",
  }[finding.category] || finding.category;

  document.getElementById("modalBody").innerHTML = `
    <div class="detail-row detail-top-meta">
      <span class="table-col">${escapeHtml(finding.table_name)}.${escapeHtml(finding.column_name)}</span>
      <span class="badge badge-cat-${escapeHtml(finding.category)}">${escapeHtml(categoryLabel)}</span>
      <span class="badge ${escapeHtml(finding.severity)}">${escapeHtml(finding.severity)}</span>
      <span class="badge status-${escapeHtml(finding.status)}">${escapeHtml(finding.status)}</span>
    </div>
    <div class="detail-row">
      <div class="detail-label">Hypothesis & Rule Context</div>
      <pre>${escapeHtml(finding.hypothesis || "(not captured)")}</pre>
    </div>
    <div class="detail-row">
      <div class="detail-label">Local Pandas Check Code</div>
      <pre>${escapeHtml(finding.check_code || "(not captured)")}</pre>
    </div>
    <div class="detail-row">
      <div class="detail-label">Aggregate Metric / Profile Result</div>
      <pre>${escapeHtml(rawResultPretty)}</pre>
    </div>
    ${isDuplicateCheck ? renderDuplicateClustersView(finding, duplicateGroups) : renderItemsTable(finding, items, usingSyntheticRow)}
    ${renderFindingDecisionRow(finding)}
  `;

  if (isDuplicateCheck) {
    attachDuplicateClusterHandlers(id);
  } else if (!usingSyntheticRow) {
    attachItemHandlers(id, finding);
  }

  attachFindingDecisionHandlers(id);
  document.getElementById("detailModal").classList.remove("hidden");
}

function renderDuplicateClustersView(finding, groups) {
  if (!groups || groups.length === 0) {
    return `<p class="hint-text">No duplicate clusters identified in this run.</p>`;
  }

  return `
    <div class="detail-row">
      <div class="detail-label" style="display: flex; justify-content: space-between; align-items: center;">
        <span>Duplicate Clusters & Golden Record Governance (${groups.length} Groups)</span>
      </div>
      <p class="hint-text" style="margin-bottom: 12px;">
        Select the most trustworthy record as the <strong>Golden Record</strong> for each cluster, or mark records as <strong>Duplicate</strong>, <strong>Unique</strong>, or <strong>To Be Confirmed</strong>.
      </p>
      <div class="duplicate-clusters-container">
        ${groups.map((group) => {
          const simScore = group.similarity_score ?? 100;
          let simClass = "sim-similar";
          if (simScore >= 99) simClass = "sim-exact";
          else if (simScore >= 80) simClass = "sim-probable";

          const goldenRecord = group.members.find(m => m.is_golden_record === 1);

          return `
            <div class="duplicate-group-card" data-group-id="${escapeHtml(group.duplicate_group_id)}">
              <div class="duplicate-group-header">
                <div class="group-title-area">
                  <span class="group-id-title">${escapeHtml(group.duplicate_group_id)}</span>
                  <span class="similarity-badge ${simClass}">${simScore}% ${escapeHtml(group.match_type)}</span>
                  <span style="font-size: 12px; color: #94a3b8;">(${group.member_count} candidates)</span>
                </div>
              </div>

              ${group.members[0]?.match_reasons ? `
                <div class="group-reasons-note">
                  <strong>Matching Criteria:</strong> ${escapeHtml(group.members[0].match_reasons)}
                </div>
              ` : ''}

              <div class="candidates-list">
                ${group.members.map((member) => {
                  const isGolden = member.is_golden_record === 1;
                  const verdict = member.review_verdict || "PENDING";
                  return `
                    <div class="candidate-card ${isGolden ? 'is-golden' : ''}" data-item-id="${escapeHtml(member.id)}">
                      <div class="candidate-info">
                        <div class="candidate-key-row">
                          <span class="candidate-key">${escapeHtml(member.key_field)}: ${escapeHtml(member.key_value)}</span>
                          ${isGolden ? '<span class="golden-tag">👑 GOLDEN RECORD</span>' : ''}
                          <span class="verdict-badge verdict-${escapeHtml(verdict)}">${escapeHtml(verdict)}</span>
                        </div>
                        <div class="candidate-details">${escapeHtml(member.issue_detail)}</div>
                      </div>

                      <div class="candidate-action-bar">
                        ${!isGolden ? `
                          <button class="btn-action btn-mark-golden" data-group-action="golden" data-group-id="${escapeHtml(group.duplicate_group_id)}" data-item-id="${escapeHtml(member.id)}">
                            ⭐ Mark as Golden Record
                          </button>
                        ` : ''}
                        <button class="btn-action btn-verdict-duplicate" data-verdict="DUPLICATE" data-item-id="${escapeHtml(member.id)}">
                          🔴 Duplicate
                        </button>
                        <button class="btn-action btn-verdict-unique" data-verdict="UNIQUE" data-item-id="${escapeHtml(member.id)}">
                          🟢 Unique
                        </button>
                        <button class="btn-action btn-verdict-tbc" data-verdict="TO_BE_CONFIRMED" data-item-id="${escapeHtml(member.id)}">
                          🟡 To Be Confirmed
                        </button>
                      </div>
                    </div>
                  `;
                }).join("")}
              </div>

              ${goldenRecord ? `
                <div class="merge-recommendation-box">
                  <strong>Merge Recommendation:</strong> Retain record <code>${escapeHtml(goldenRecord.key_value)}</code> as master. Merge non-golden duplicate records into this master, consolidating missing transaction references and contact details.
                </div>
              ` : ''}
            </div>
          `;
        }).join("")}
      </div>
    </div>
  `;
}

function attachDuplicateClusterHandlers(findingId) {
  // Golden record buttons
  document.querySelectorAll("[data-group-action='golden']").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const groupId = btn.dataset.groupId;
      const itemId = btn.dataset.itemId;
      try {
        await fetchJSON(`${API_BASE}/findings/${encodeURIComponent(findingId)}/duplicate-groups/${encodeURIComponent(groupId)}/golden-record`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ golden_item_id: itemId }),
        });
        await openDetail(findingId);
        await loadFindings();
      } catch (err) {
        alert(`Failed to set golden record: ${err.message}`);
      }
    });
  });

  // 3-action verdict buttons
  document.querySelectorAll("[data-verdict]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const itemId = btn.dataset.itemId;
      const verdict = btn.dataset.verdict;
      try {
        await fetchJSON(`${API_BASE}/finding-items/${encodeURIComponent(itemId)}/verdict`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ verdict }),
        });
        await openDetail(findingId);
        await loadFindings();
      } catch (err) {
        alert(`Failed to set verdict: ${err.message}`);
      }
    });
  });
}

function renderItemsTable(finding, items, isSynthetic) {
  const isAutoFixable = finding.fix_type === "AUTO_FIXABLE" && finding.auto_fix_value;

  return `
    <div class="detail-row">
      <div class="detail-label">
        Individual Issues ${isSynthetic ? "(no row-level detail captured)" : `(${items.length})`}
      </div>
      ${isSynthetic ? "" : `
        <div class="item-actions-bar">
          <button data-bulk="APPROVED">Approve Selected</button>
          <button data-bulk="REJECTED">Reject Selected</button>
          ${isAutoFixable ? `<button id="bulkAutofillBtn" class="btn-autofill">⚡ Apply Recommended Default (${escapeHtml(finding.auto_fix_value)}) to All Pending</button>` : ''}
        </div>
      `}
      <table class="items-table">
        <thead>
          <tr>
            <th></th><th>Row</th><th>Key Field</th><th>Mismatch / Details</th>
            <th>Corrected Data</th><th>Status</th>
          </tr>
        </thead>
        <tbody>
          ${items.map((item) => {
            const disabled = isSynthetic || item.status !== "PENDING";
            return `
              <tr data-item-id="${escapeHtml(item.id || "")}" class="item-row status-${escapeHtml(item.status)}${isSynthetic ? " synthetic-row" : ""}">
                <td><input type="checkbox" class="item-checkbox" ${disabled ? "disabled" : ""}></td>
                <td>${escapeHtml(item.row_index ?? "-")}</td>
                <td>${escapeHtml(item.key_field)}${item.key_value ? `: ${escapeHtml(item.key_value)}` : ""}</td>
                <td>
                  ${escapeHtml(item.issue_detail)}
                  ${isAutoFixable && item.status === 'PENDING' ? `
                    <button class="btn-autofill" data-autofill-item="${escapeHtml(item.id)}" data-autofill-val="${escapeHtml(finding.auto_fix_value)}">
                      ⚡ Autofill '${escapeHtml(finding.auto_fix_value)}'
                    </button>
                  ` : ''}
                </td>
                <td><input type="text" class="corrected-input" placeholder="Enter corrected value..." value="${escapeHtml(item.corrected_data || "")}" ${disabled ? "disabled" : ""}></td>
                <td><span class="badge status-${escapeHtml(item.status)}">${escapeHtml(item.status)}</span></td>
              </tr>
            `;
          }).join("")}
        </tbody>
      </table>
      ${isSynthetic ? `
        <p class="hint-text">This check did not produce row-level detail. Use “Approve Finding” or “Reject Finding” below.</p>
      ` : ""}
    </div>
  `;
}

function attachItemHandlers(findingId, finding) {
  document.querySelectorAll("[data-bulk]").forEach((button) => {
    button.addEventListener("click", async () => {
      const status = button.dataset.bulk;
      const checkedRows = document.querySelectorAll(".item-checkbox:checked");

      if (checkedRows.length === 0) {
        alert("Select at least one pending item first.");
        return;
      }

      for (const checkbox of checkedRows) {
        const row = checkbox.closest("tr");
        const itemId = row.dataset.itemId;
        const correctedData = row.querySelector(".corrected-input").value;

        await fetchJSON(`${API_BASE}/finding-items/${encodeURIComponent(itemId)}/decision`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status, corrected_data: correctedData }),
        });
      }

      await loadFindings();
      await openDetail(findingId);
    });
  });

  // Single autofill buttons
  document.querySelectorAll("[data-autofill-item]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const itemId = btn.dataset.autofillItem;
      const fixVal = btn.dataset.autofillVal;
      await fetchJSON(`${API_BASE}/finding-items/${encodeURIComponent(itemId)}/autofill`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ fix_value: fixVal }),
      });
      await loadFindings();
      await openDetail(findingId);
    });
  });

  // Bulk autofill button
  const bulkAutofillBtn = document.getElementById("bulkAutofillBtn");
  if (bulkAutofillBtn && finding.auto_fix_value) {
    bulkAutofillBtn.addEventListener("click", async () => {
      const pendingRows = document.querySelectorAll(".item-row.status-PENDING");
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
      await openDetail(findingId);
    });
  }
}

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

function escapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

document.getElementById("closeModal").addEventListener("click", () => {
  document.getElementById("detailModal").classList.add("hidden");
});

document.getElementById("runSelect").addEventListener("change", loadFindings);
document.getElementById("statusFilter").addEventListener("change", loadFindings);
document.getElementById("scopeFilter").addEventListener("change", loadFindings);
document.getElementById("refreshBtn").addEventListener("click", loadFindings);

// Category Tabs Click Handlers
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

(async function init() {
  try {
    await loadRuns();
    await loadFindings();
  } catch (error) {
    console.error("Failed to initialize the review app:", error);
  }
})();
