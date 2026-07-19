// Business Context — dedicated tab: list files as cards, click one to open
// an editable modal (title/sensitivity/full body text, delete), plus a
// quick-add box (paste markdown or upload a .md file). Ingested through the
// same pipeline as any document (chunk -> embed -> pgvector); the agent
// reads it back via the `search_business_knowledge` tool (hermes/tools.py).

const ctxListEl = document.getElementById("context-list");
const ctxEmptyEl = document.getElementById("context-empty");
const ctxScopeHintEl = document.getElementById("context-scope-hint");
const ctxAddEl = document.getElementById("context-add");
const ctxFileInput = document.getElementById("context-file-input");
const ctxUploadBtn = document.getElementById("context-upload-btn");
const ctxTitleInput = document.getElementById("context-paste-title");
const ctxBodyInput = document.getElementById("context-paste-body");
const ctxSensitivitySelect = document.getElementById("context-sensitivity");
const ctxSaveBtn = document.getElementById("context-save-btn");
const ctxResultEl = document.getElementById("context-result");

// Files/Memories sub-tabs
const filesSubviewEl = document.getElementById("files-subview");
const memoriesSubviewEl = document.getElementById("memories-subview");
const subtabFilesBtn = document.getElementById("context-subtab-files");
const subtabMemoriesBtn = document.getElementById("context-subtab-memories");
const memoryListEl = document.getElementById("memory-list");
const memoryEmptyEl = document.getElementById("memory-empty");
let activeSubview = "files";
let currentMemories = [];

// Editor modal
const cxOverlay = document.getElementById("cx-overlay");
const cxTitleEl = document.getElementById("cx-edit-title");
const cxBodyEl = document.getElementById("cx-edit-body");
const cxSensitivityEl = document.getElementById("cx-edit-sensitivity");
const cxResultEl = document.getElementById("cx-edit-result");
const cxSaveBtn = document.getElementById("cx-save-btn");
const cxDeleteBtn = document.getElementById("cx-delete-btn");
const cxCloseBtn = document.getElementById("cx-close-btn");

let currentDocs = [];
let openDocId = null;
let deleteArmTimer = null;

function ctxEscapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function ctxSetResult(msg, isError) {
  if (!ctxResultEl) return;
  ctxResultEl.textContent = msg || "";
  ctxResultEl.classList.toggle("tool-err", !!isError);
}

function currentSlug() {
  const slug = window.HermesBiz ? window.HermesBiz.current : null;
  return slug && slug !== "all" ? slug : null;
}

// The scope to save a NEW file under — a specific business, or "all" for a
// global note. Unlike currentSlug(), "all" is a valid (not null) answer here.
function addScope() {
  return (window.HermesBiz && window.HermesBiz.current) || "all";
}

// Renders `items` into `container` as <li>s, grouped with a header per
// distinct `groupLabelFn(item)` when `slug` is null ("All Businesses" —
// nothing to group by when scoped to one business already).
function renderGrouped(container, items, slug, groupLabelFn, itemHtmlFn) {
  if (slug) {
    container.innerHTML = items.map(itemHtmlFn).join("");
    return;
  }
  const groups = new Map();
  for (const item of items) {
    const label = groupLabelFn(item) || "Unknown";
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(item);
  }
  const parts = [];
  for (const [label, groupItems] of groups) {
    parts.push(`<li class="context-group-header">${ctxEscapeHtml(label)}</li>`);
    parts.push(...groupItems.map(itemHtmlFn));
  }
  container.innerHTML = parts.join("");
}

function ctxFileItemHtml(d) {
  const globalBadge = d.is_global
    ? '<span class="memory-item-type" title="Visible to every business">🌐 All Businesses</span>'
    : "";
  return `
      <li class="context-item" data-id="${ctxEscapeHtml(d.document_id)}">
        <span class="context-item-title">${ctxEscapeHtml(d.title || "untitled")}</span>
        ${globalBadge}
        <span class="context-item-meta">${ctxEscapeHtml(d.sensitivity_level || "")}</span>
        <button type="button" class="context-item-delete" title="Remove" data-id="${ctxEscapeHtml(d.document_id)}">×</button>
      </li>`;
}

// ---- Files ---------------------------------------------------------------
async function loadContextList() {
  if (!ctxListEl) return;
  const slug = currentSlug();
  // Adding now works from BOTH a specific business and "All Businesses" (the
  // latter saves a global note) — the hint just explains what "all" means here.
  ctxScopeHintEl.hidden = !!slug;

  try {
    const r = await fetch(
      `/api/documents?entity_id=${encodeURIComponent(slug || "all")}&source_system=business_context`
    );
    const j = await r.json();
    currentDocs = j.documents || [];
    ctxEmptyEl.hidden = currentDocs.length !== 0;
    renderGrouped(
      ctxListEl, currentDocs, slug,
      (d) => (d.is_global ? "🌐 All Businesses" : (d.entity_name || d.entity_slug)),
      ctxFileItemHtml,
    );
  } catch (e) {
    console.error("loadContextList:", e);
    ctxSetResult("Failed to load context files.", true);
  }
}

async function quickDelete(id) {
  if (!id) return;
  try {
    const r = await fetch(`/api/documents/${encodeURIComponent(id)}`, { method: "DELETE" });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || "Delete failed");
    await loadContextList();
  } catch (err) {
    ctxSetResult(`Delete failed: ${err.message}`, true);
  }
}

if (ctxListEl) {
  ctxListEl.addEventListener("click", (e) => {
    const delBtn = e.target.closest(".context-item-delete");
    if (delBtn) {
      e.stopPropagation();
      quickDelete(delBtn.dataset.id);
      return;
    }
    const item = e.target.closest(".context-item");
    if (item) openEditor(item.dataset.id);
  });
}

// ---- Memories (read-only content, delete only — no click-to-edit) --------
function memoryItemHtml(m) {
  const scopeLabel = m.scope_type === "owner" ? "Owner"
    : m.scope_type === "project" ? (m.project_name || "Project")
    : (m.entity_name || "Business");
  const alwaysCls = m.always_inject ? " always" : "";
  return `
      <li class="context-item memory-item" data-id="${ctxEscapeHtml(m.memory_id)}">
        <span class="memory-item-text">${ctxEscapeHtml(m.text)}</span>
        <span class="memory-item-badges">
          <span class="memory-item-type${alwaysCls}" title="${ctxEscapeHtml(scopeLabel)}">${ctxEscapeHtml(m.memory_type)}</span>
          <span class="context-item-meta">${ctxEscapeHtml(m.sensitivity_level || "")}</span>
        </span>
        <button type="button" class="context-item-delete" title="Remove" data-id="${ctxEscapeHtml(m.memory_id)}">×</button>
      </li>`;
}

async function loadMemoryList() {
  if (!memoryListEl) return;
  const slug = currentSlug();
  ctxScopeHintEl.hidden = true; // the hint is about adding a new file — not relevant on this sub-tab
  try {
    const r = await fetch(`/api/memories?entity_id=${encodeURIComponent(slug || "all")}`);
    const j = await r.json();
    currentMemories = j.memories || [];
    memoryEmptyEl.hidden = currentMemories.length !== 0;
    renderGrouped(
      memoryListEl, currentMemories, slug,
      (m) => (m.scope_type === "owner" ? "Owner" : m.scope_type === "project" ? (m.project_name || "Project") : (m.entity_name || "Business")),
      memoryItemHtml,
    );
  } catch (e) {
    console.error("loadMemoryList:", e);
  }
}

if (memoryListEl) {
  memoryListEl.addEventListener("click", async (e) => {
    const delBtn = e.target.closest(".context-item-delete");
    if (!delBtn) return;
    const id = delBtn.dataset.id;
    if (!id) return;
    delBtn.disabled = true;
    try {
      const r = await fetch(`/api/memories/${encodeURIComponent(id)}`, { method: "DELETE" });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || "Delete failed");
      await loadMemoryList();
    } catch (err) {
      console.error("delete memory:", err);
      delBtn.disabled = false;
    }
  });
}

// ---- Sub-tab toggle --------------------------------------------------------
function setSubview(view) {
  activeSubview = view;
  filesSubviewEl.hidden = view !== "files";
  memoriesSubviewEl.hidden = view !== "memories";
  subtabFilesBtn.classList.toggle("active", view === "files");
  subtabMemoriesBtn.classList.toggle("active", view === "memories");
  // The quick-add box is file-specific — only ever shown on the Files sub-tab
  // (any business selection, including "all", is now valid for adding).
  ctxAddEl.hidden = view !== "files";
  if (ctxUploadBtn) ctxUploadBtn.disabled = view !== "files";
  if (view === "memories") loadMemoryList();
  else loadContextList();
}

if (subtabFilesBtn) subtabFilesBtn.addEventListener("click", () => setSubview("files"));
if (subtabMemoriesBtn) subtabMemoriesBtn.addEventListener("click", () => setSubview("memories"));

function refreshActiveSubview() {
  if (activeSubview === "memories") loadMemoryList();
  else loadContextList();
}

// ---- Editor modal --------------------------------------------------------
function resetDeleteBtn() {
  clearTimeout(deleteArmTimer);
  if (cxDeleteBtn) {
    cxDeleteBtn.textContent = "Delete";
    cxDeleteBtn.classList.remove("armed");
  }
}

function openEditor(id) {
  const doc = currentDocs.find((d) => d.document_id === id);
  if (!doc || !cxOverlay) return;
  openDocId = id;
  resetDeleteBtn();
  cxTitleEl.value = doc.title || "";
  cxSensitivityEl.value = doc.sensitivity_level || "internal";
  cxBodyEl.value = "Loading…";
  cxResultEl.textContent = "";
  cxOverlay.hidden = false;

  fetch(`/api/documents/${encodeURIComponent(id)}`)
    .then((r) => r.json())
    .then((j) => {
      if (openDocId !== id) return; // closed/switched before this resolved
      cxBodyEl.value = (j.document && j.document.body_text) || "";
    })
    .catch((e) => {
      console.error("openEditor:", e);
      cxBodyEl.value = "";
      cxResultEl.textContent = "Failed to load content.";
    });
}

function closeEditor() {
  if (cxOverlay) cxOverlay.hidden = true;
  openDocId = null;
  resetDeleteBtn();
}

if (cxCloseBtn) cxCloseBtn.addEventListener("click", closeEditor);
if (cxOverlay) {
  cxOverlay.addEventListener("click", (e) => {
    if (e.target === cxOverlay) closeEditor();
  });
}
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && cxOverlay && !cxOverlay.hidden) closeEditor();
});

if (cxSaveBtn) {
  cxSaveBtn.addEventListener("click", async () => {
    if (!openDocId) return;
    cxResultEl.textContent = "Saving…";
    try {
      const r = await fetch(`/api/documents/${encodeURIComponent(openDocId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: cxTitleEl.value.trim(),
          sensitivity_level: cxSensitivityEl.value,
          body_text: cxBodyEl.value,
        }),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || "Save failed");
      cxResultEl.textContent = "Saved.";
      await loadContextList();
    } catch (e) {
      cxResultEl.textContent = `Failed: ${e.message}`;
    }
  });
}

if (cxDeleteBtn) {
  cxDeleteBtn.addEventListener("click", async () => {
    if (!openDocId) return;
    if (!cxDeleteBtn.classList.contains("armed")) {
      cxDeleteBtn.classList.add("armed");
      cxDeleteBtn.textContent = "Confirm delete?";
      clearTimeout(deleteArmTimer);
      deleteArmTimer = setTimeout(resetDeleteBtn, 4000);
      return;
    }
    clearTimeout(deleteArmTimer);
    const id = openDocId;
    try {
      const r = await fetch(`/api/documents/${encodeURIComponent(id)}`, { method: "DELETE" });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || "Delete failed");
      closeEditor();
      await loadContextList();
    } catch (e) {
      cxResultEl.textContent = `Delete failed: ${e.message}`;
      resetDeleteBtn();
    }
  });
}

// ---- Quick-add: upload a .md file or paste markdown text -----------------
function contextFormData(slug, file) {
  const fd = new FormData();
  fd.append("entity_id", slug);
  fd.append("source_system", "business_context");
  fd.append("sensitivity_level", ctxSensitivitySelect ? ctxSensitivitySelect.value : "internal");
  fd.append("files", file);
  return fd;
}

async function uploadContext(formData) {
  ctxSetResult("Saving…", false);
  try {
    const r = await fetch("/api/documents", { method: "POST", body: formData });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || "Upload failed");
    ctxSetResult("Saved.", false);
    await loadContextList();
  } catch (e) {
    ctxSetResult(`Failed: ${e.message}`, true);
  }
}

if (ctxUploadBtn) {
  ctxUploadBtn.addEventListener("click", () => ctxFileInput && ctxFileInput.click());
}

if (ctxFileInput) {
  ctxFileInput.addEventListener("change", async () => {
    const file = ctxFileInput.files && ctxFileInput.files[0];
    ctxFileInput.value = "";
    if (!file) return;
    await uploadContext(contextFormData(addScope(), file));
  });
}

if (ctxSaveBtn) {
  ctxSaveBtn.addEventListener("click", async () => {
    const slug = addScope();
    const title = (ctxTitleInput.value || "").trim();
    const body = (ctxBodyInput.value || "").trim();
    if (!body) {
      ctxSetResult("Paste some markdown text first.", true);
      return;
    }
    const filename = title ? (title.toLowerCase().endsWith(".md") ? title : `${title}.md`) : `context-note-${Date.now()}.md`;
    const file = new File([new Blob([body], { type: "text/markdown" })], filename, { type: "text/markdown" });
    await uploadContext(contextFormData(slug, file));
    ctxTitleInput.value = "";
    ctxBodyInput.value = "";
  });
}

// ---- Hook into app.js view switch + business-selector changes -----------
window._contextInit = refreshActiveSubview;

if (window.HermesBiz) {
  window.HermesBiz.bind(
    document.getElementById("hermes-biz-select-context"),
    document.getElementById("hermes-biz-badge-context")
  );
  window.HermesBiz.onChange(refreshActiveSubview);
}
