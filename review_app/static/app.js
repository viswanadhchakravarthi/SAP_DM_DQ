const API_BASE = "/api";

let currentFindings = [];

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

  document.getElementById("statsBar").innerHTML = `
    <span>Total: <span class="count">${stats.TOTAL ?? 0}</span></span>
    <span>Pending: <span class="count">${stats.PENDING ?? 0}</span></span>
    <span>Approved: <span class="count">${stats.APPROVED ?? 0}</span></span>
    <span>Rejected: <span class="count">${stats.REJECTED ?? 0}</span></span>
  `;
}

async function loadFindings() {
  const runId = document.getElementById("runSelect").value;
  const status = document.getElementById("statusFilter").value;
  const params = new URLSearchParams();

  if (runId) params.append("run_id", runId);
  if (status) params.append("status", status);

  const container = document.getElementById("findingsContainer");
  container.innerHTML = '<p class="empty-state">Loading...</p>';

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

  return `
    <div class="finding-card" data-id="${escapeHtml(finding.id)}">
      <div class="top-row">
        <div class="table-col">${escapeHtml(finding.table_name)}.${escapeHtml(finding.column_name)}</div>
        <div style="display: flex; gap: 8px;">
          <span class="badge ${escapeHtml(finding.severity)}">${escapeHtml(finding.severity)}</span>
          <span class="badge status-${escapeHtml(finding.status)}">${escapeHtml(finding.status)}</span>
        </div>
      </div>
      <div class="summary">${escapeHtml(finding.result_summary)}</div>
      <div class="meta">
        <span>Confidence: ${escapeHtml(finding.confidence)}</span>
        <span>Reusable (LLM opinion): ${finding.reusable ? "Yes" : "No"}</span>
        <span>${finding.created_at ? new Date(finding.created_at).toLocaleString() : ""}</span>
      </div>
      <div class="action-row">
        <button class="btn-detail" data-action="detail">View Details</button>
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
    // Keep the original string when it is not JSON.
  }

  const realItems = await fetchJSON(`${API_BASE}/findings/${encodeURIComponent(id)}/items`);
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

  document.getElementById("modalBody").innerHTML = `
    <div class="detail-row detail-top-meta">
      <span class="table-col">${escapeHtml(finding.table_name)}.${escapeHtml(finding.column_name)}</span>
      <span class="badge ${escapeHtml(finding.severity)}">${escapeHtml(finding.severity)}</span>
      <span class="badge status-${escapeHtml(finding.status)}">${escapeHtml(finding.status)}</span>
    </div>
    <div class="detail-row">
      <div class="detail-label">Hypothesis</div>
      <pre>${escapeHtml(finding.hypothesis || "(not captured)")}</pre>
    </div>
    <div class="detail-row">
      <div class="detail-label">Check Code</div>
      <pre>${escapeHtml(finding.check_code || "(not captured)")}</pre>
    </div>
    <div class="detail-row">
      <div class="detail-label">Raw Result (aggregate)</div>
      <pre>${escapeHtml(rawResultPretty)}</pre>
    </div>
    ${renderItemsTable(items, usingSyntheticRow)}
    ${renderFindingDecisionRow(finding)}
  `;

  if (!usingSyntheticRow) attachItemHandlers(id);
  attachFindingDecisionHandlers(id);
  document.getElementById("detailModal").classList.remove("hidden");
}

function renderItemsTable(items, isSynthetic) {
  return `
    <div class="detail-row">
      <div class="detail-label">
        Individual Issues ${isSynthetic ? "(no row-level detail captured for this check)" : `(${items.length})`}
      </div>
      ${isSynthetic ? "" : `
        <div class="item-actions-bar">
          <button data-bulk="APPROVED">Approve Selected</button>
          <button data-bulk="REJECTED">Reject Selected</button>
        </div>
      `}
      <table class="items-table">
        <thead>
          <tr>
            <th></th><th>Row</th><th>Key Field</th><th>Mismatch/Error Details</th>
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
                <td>${escapeHtml(item.issue_detail)}</td>
                <td><input type="text" class="corrected-input" placeholder="Enter corrected value..." value="${escapeHtml(item.corrected_data || "")}" ${disabled ? "disabled" : ""}></td>
                <td><span class="badge status-${escapeHtml(item.status)}">${escapeHtml(item.status)}</span></td>
              </tr>
            `;
          }).join("")}
        </tbody>
      </table>
      ${isSynthetic ? `
        <p class="hint-text">This check did not produce row-level detail (no <code>detail_code</code> or it matched no rows). Use “Approve Finding” or “Reject Finding” below to record a decision for the finding as a whole.</p>
      ` : ""}
    </div>
  `;
}

function attachItemHandlers(findingId) {
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
      <div class="detail-label">Finding Decision</div>
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
document.getElementById("refreshBtn").addEventListener("click", loadFindings);

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
