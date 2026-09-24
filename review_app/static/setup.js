// Page 1: pick (or add) a client, upload its data dictionary and tables,
// then proceed to the review dashboard (review.html) fixed to that client.

const API_BASE = "/api";
const CLIENT_STORAGE_KEY = "dq-review-client";

let clients = [];      // [{client_id, name, duplicate_decisions}]
let selected = null;   // {client_id|null, name, isNew}
let workspace = null;  // /api/clients/{id}/workspace response
let activeOption = -1; // keyboard-highlighted option in the client list

const el = (id) => document.getElementById(id);

async function fetchJSON(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let detail = await res.text();
    try { detail = JSON.parse(detail).detail || detail; } catch { /* plain text */ }
    throw new Error(detail);
  }
  return res.json();
}

function escapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}

// Mirrors client_knowledge.client_id_for() so "ACME retail" is recognised as
// the existing "Acme Retail" instead of offering to add a duplicate client.
function clientIdFor(name) {
  return name.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
}

function formatWhen(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString("en-GB", {
    timeZone: "Asia/Kolkata", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
  }) + " IST";
}

// ---------------------------------------------------------------------------
// Step 1: client combobox
// ---------------------------------------------------------------------------

function buildOptions(text) {
  const query = text.trim().toLowerCase();
  const matches = clients
    .filter((c) => !query || c.name.toLowerCase().includes(query))
    .sort((a, b) => (b.name.toLowerCase().startsWith(query) - a.name.toLowerCase().startsWith(query))
      || a.name.localeCompare(b.name));
  const options = matches.map((c) => ({ type: "existing", client: c }));
  const id = clientIdFor(text);
  if (id && !clients.some((c) => c.client_id === id)) {
    options.push({ type: "add", name: text.trim() });
  }
  return options;
}

function renderOptions() {
  const list = el("clientOptions");
  const input = el("clientInput");
  const options = buildOptions(input.value);
  list._options = options;

  if (options.length === 0) {
    list.innerHTML = `<li class="combobox-empty">No clients yet - type a name to add one</li>`;
  } else {
    list.innerHTML = options.map((o, i) => o.type === "existing"
      ? `<li role="option" id="clientOpt${i}" data-index="${i}" class="${i === activeOption ? "active" : ""}" aria-selected="${i === activeOption}">
           <span class="opt-name">${highlight(o.client.name, input.value)}</span>
           <span class="opt-meta">existing client</span>
         </li>`
      : `<li role="option" id="clientOpt${i}" data-index="${i}" class="opt-add ${i === activeOption ? "active" : ""}" aria-selected="${i === activeOption}">
           <span class="opt-name">+ Add client “${escapeHtml(o.name)}”</span>
           <span class="opt-meta">new</span>
         </li>`).join("");
  }
  list.classList.remove("hidden");
  input.setAttribute("aria-expanded", "true");
  input.setAttribute("aria-activedescendant", activeOption >= 0 ? `clientOpt${activeOption}` : "");
}

function highlight(name, query) {
  const q = query.trim();
  const i = q ? name.toLowerCase().indexOf(q.toLowerCase()) : -1;
  if (i < 0) return escapeHtml(name);
  return escapeHtml(name.slice(0, i)) + `<mark>${escapeHtml(name.slice(i, i + q.length))}</mark>` + escapeHtml(name.slice(i + q.length));
}

function closeOptions() {
  el("clientOptions").classList.add("hidden");
  el("clientInput").setAttribute("aria-expanded", "false");
  activeOption = -1;
}

async function chooseOption(option) {
  closeOptions();
  if (option.type === "existing") {
    selected = { client_id: option.client.client_id, name: option.client.name, isNew: false };
  } else {
    selected = { client_id: null, name: option.name, isNew: true };
  }
  el("clientInput").value = selected.name;
  workspace = null;
  if (selected.client_id) {
    await loadWorkspace();
  }
  render();
}

function setupCombobox() {
  const input = el("clientInput");
  const list = el("clientOptions");

  input.addEventListener("focus", () => { activeOption = -1; renderOptions(); });
  input.addEventListener("input", () => {
    // Editing the name after choosing means choosing again.
    if (selected && input.value !== selected.name) {
      selected = null;
      workspace = null;
      render();
    }
    activeOption = -1;
    renderOptions();
  });
  input.addEventListener("keydown", (event) => {
    const options = list._options || [];
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (list.classList.contains("hidden")) renderOptions();
      const step = event.key === "ArrowDown" ? 1 : -1;
      activeOption = options.length ? (activeOption + step + options.length) % options.length : -1;
      renderOptions();
      el(`clientOpt${activeOption}`)?.scrollIntoView({ block: "nearest" });
    } else if (event.key === "Enter") {
      event.preventDefault();
      // Enter picks the highlighted option, else an exact name match, else the only option.
      const exact = options.find((o) => o.type === "existing" && o.client.client_id === clientIdFor(input.value));
      const option = activeOption >= 0 ? options[activeOption] : (exact || (options.length === 1 ? options[0] : null));
      if (option) chooseOption(option);
    } else if (event.key === "Escape") {
      closeOptions();
    }
  });
  // mousedown (not click) so the choice lands before the input's blur closes the list
  list.addEventListener("mousedown", (event) => {
    const item = event.target.closest("[data-index]");
    if (!item) return;
    event.preventDefault();
    chooseOption(list._options[Number(item.dataset.index)]);
  });
  input.addEventListener("blur", () => setTimeout(closeOptions, 100));
}

// ---------------------------------------------------------------------------
// Step 2: uploads
// ---------------------------------------------------------------------------

async function loadWorkspace() {
  workspace = await fetchJSON(`${API_BASE}/clients/${encodeURIComponent(selected.client_id)}/workspace`);
}

// A new client is only created once there is something to store for it.
async function ensureClientCreated() {
  if (!selected.isNew) return;
  const created = await fetchJSON(`${API_BASE}/clients`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: selected.name }),
  });
  selected = { client_id: created.client_id, name: created.name, isNew: false };
  clients = await fetchJSON(`${API_BASE}/clients`);
  el("clientInput").value = created.name;
}

function logUpload(fileName, state, message = "") {
  const log = el("uploadLog");
  const id = `log-${fileName.replace(/[^a-z0-9]/gi, "_")}`;
  let item = document.getElementById(id);
  if (!item) {
    item = document.createElement("li");
    item.id = id;
    log.prepend(item);
  }
  item.className = `upload-${state}`;
  const icon = { uploading: "⏳", done: "✅", error: "⚠️" }[state];
  item.innerHTML = `${icon} <strong>${escapeHtml(fileName)}</strong> ${escapeHtml(message)}`;
}

async function uploadFiles(kind, files) {
  if (!selected || !files.length) return;
  const list = Array.from(files);
  try {
    await ensureClientCreated();
  } catch (error) {
    logUpload(list[0].name, "error", `- could not create client: ${error.message}`);
    return;
  }
  for (const file of list) {
    logUpload(file.name, "uploading", "uploading…");
    try {
      const url = `${API_BASE}/clients/${encodeURIComponent(selected.client_id)}/${kind}?filename=${encodeURIComponent(file.name)}`;
      workspace = await fetchJSON(url, { method: "PUT", headers: { "Content-Type": "text/csv" }, body: file });
      logUpload(file.name, "done", kind === "dictionary" ? "- data dictionary saved" : "- table saved");
    } catch (error) {
      logUpload(file.name, "error", `- ${error.message}`);
    }
    render();
  }
}

async function removeTable(table) {
  if (!confirm(`Remove table ${table} from ${selected.name}?`)) return;
  try {
    workspace = await fetchJSON(
      `${API_BASE}/clients/${encodeURIComponent(selected.client_id)}/tables/${encodeURIComponent(table)}`,
      { method: "DELETE" });
  } catch (error) {
    alert(`Could not remove ${table}: ${error.message}`);
  }
  render();
}

async function clearAllFiles() {
  const tables = workspace?.tables?.length || 0;
  const what = [workspace?.dictionary ? "the data dictionary" : null, tables ? `${tables} table(s)` : null]
    .filter(Boolean).join(" and ");
  if (!confirm(`Remove ${what} of ${selected.name}?\n\nFindings, review decisions and the client's memory `
    + "are kept - only the uploaded files are removed, so you can upload a new set.")) return;
  try {
    workspace = await fetchJSON(`${API_BASE}/clients/${encodeURIComponent(selected.client_id)}/files`,
      { method: "DELETE" });
    el("uploadLog").innerHTML = "";
    logUpload(`${workspace.removed_files} file(s)`, "done", "- removed, ready for new uploads");
  } catch (error) {
    alert(`Could not clear the files: ${error.message}`);
  }
  render();
}

function setupDropZone(zoneId, inputId, kind) {
  const zone = el(zoneId);
  const input = el(inputId);
  input.addEventListener("change", () => {
    uploadFiles(kind, input.files);
    input.value = ""; // allow re-uploading the same file name
  });
  ["dragenter", "dragover"].forEach((type) => zone.addEventListener(type, (event) => {
    event.preventDefault();
    if (!el("dataStep").classList.contains("is-locked")) zone.classList.add("drag-over");
  }));
  ["dragleave", "drop"].forEach((type) => zone.addEventListener(type, () => zone.classList.remove("drag-over")));
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    if (el("dataStep").classList.contains("is-locked")) return;
    const files = kind === "dictionary" ? [...event.dataTransfer.files].slice(0, 1) : event.dataTransfer.files;
    uploadFiles(kind, files);
  });
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function render() {
  const selection = el("clientSelection");
  const dataStep = el("dataStep");
  const locked = !selected;
  dataStep.classList.toggle("is-locked", locked);
  dataStep.querySelectorAll("input[type=file]").forEach((input) => { input.disabled = locked; });

  if (!selected) {
    selection.innerHTML = "";
    el("dataStepHint").textContent = "Choose a client first.";
  } else if (selected.isNew) {
    selection.innerHTML = `<span class="selection-chip is-new">+ New client: <strong>${escapeHtml(selected.name)}</strong></span>
      <span class="hint-text">It is created when you upload its first file.</span>`;
    el("dataStepHint").textContent = `Upload ${selected.name}'s data dictionary and tables.`;
  } else {
    const known = clients.find((c) => c.client_id === selected.client_id);
    const decisions = known?.duplicate_decisions ? ` · ${known.duplicate_decisions} remembered decision(s)` : "";
    selection.innerHTML = `<span class="selection-chip">🏢 <strong>${escapeHtml(selected.name)}</strong></span>
      <span class="hint-text">Existing client${escapeHtml(decisions)}</span>`;
    el("dataStepHint").textContent = workspace?.tables?.length
      ? "Previously uploaded files are kept - replace or add files if the data changed."
      : `Upload ${selected.name}'s data dictionary and tables.`;
  }

  const dictionary = workspace?.dictionary;
  const hasFiles = Boolean(dictionary || workspace?.tables?.length);
  el("clearFilesBtn").classList.toggle("hidden", !selected || selected.isNew || !hasFiles);
  el("dictionaryState").innerHTML = dictionary ? `
    <div class="file-row">
      <span class="file-name">📘 ${escapeHtml(dictionary.file)}</span>
      <span class="file-meta">${dictionary.rows} field definitions · ${dictionary.tables_described} table(s) · uploaded ${escapeHtml(formatWhen(dictionary.uploaded_at))}</span>
      <span class="file-hint">Upload another file to replace it</span>
    </div>` : "";

  const tables = workspace?.tables || [];
  el("tablesState").innerHTML = tables.length ? `
    <table class="files-table">
      <thead><tr><th>Table</th><th>Uploaded file</th><th>Rows</th><th>Columns</th><th>Uploaded</th><th></th></tr></thead>
      <tbody>
        ${tables.map((t) => `
          <tr>
            <td class="file-table-name">${escapeHtml(t.table)}</td>
            <td>${escapeHtml(t.original_name || t.file)}</td>
            <td>${Number(t.rows).toLocaleString("en-IN")}</td>
            <td>${t.columns}</td>
            <td>${escapeHtml(formatWhen(t.uploaded_at))}</td>
            <td><button class="btn-remove" data-remove-table="${escapeHtml(t.table)}" aria-label="Remove ${escapeHtml(t.table)}">Remove</button></td>
          </tr>`).join("")}
      </tbody>
    </table>` : "";
  el("tablesState").querySelectorAll("[data-remove-table]").forEach((btn) =>
    btn.addEventListener("click", () => removeTable(btn.dataset.removeTable)));

  const missing = [];
  if (!selected) missing.push("choose a client");
  else {
    if (!dictionary) missing.push("upload a data dictionary");
    if (!tables.length) missing.push("upload at least one table");
  }
  el("proceedBtn").disabled = missing.length > 0;
  el("proceedHint").textContent = missing.length
    ? `To continue: ${missing.join(", ")}.`
    : `${selected.name} · ${dictionary.file} · ${tables.length} table(s)`;
}

function proceed() {
  if (el("proceedBtn").disabled) return;
  try { localStorage.setItem(CLIENT_STORAGE_KEY, selected.client_id); } catch { /* convenience only */ }
  window.location.href = `review.html?client=${encodeURIComponent(selected.client_id)}`;
}

(async function init() {
  el("clearFilesBtn").addEventListener("click", clearAllFiles);
  setupCombobox();
  setupDropZone("dictionaryDrop", "dictionaryInput", "dictionary");
  setupDropZone("tablesDrop", "tablesInput", "tables");
  el("proceedBtn").addEventListener("click", proceed);

  try {
    clients = await fetchJSON(`${API_BASE}/clients`);
  } catch (error) {
    el("clientSelection").innerHTML = `<span class="run-explorer-error">Could not load clients: ${escapeHtml(error.message)}</span>`;
  }

  // Coming back from page 2 ("Change client / data") keeps that client selected.
  let preselect = new URLSearchParams(window.location.search).get("client");
  if (!preselect) {
    try { preselect = localStorage.getItem(CLIENT_STORAGE_KEY); } catch { preselect = null; }
  }
  const known = clients.find((c) => c.client_id === preselect);
  if (known) {
    await chooseOption({ type: "existing", client: known });
  } else {
    render();
  }
})();
