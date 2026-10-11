// Library tab — Files sub-list + Artefacts/Files sub-tab switching.
//
// The Artefacts sub-list is owned by artefacts.js (window._artefactsInit + the
// #artefacts-list / af-detail modal). This module adds the "Files" sub-list of
// AI-generated documents (create_file outputs, /api/generated-files/*) and the
// sub-tab toggle between the two. Binds immediately at load — its DOM exists in
// the Library view — so no lazy init hook is needed.
(function () {
  "use strict";

  let view = "artefacts";
  let loadedFiles = false;

  const $ = (id) => document.getElementById(id);
  const esc = (s) =>
    (typeof escapeHtml === "function"
      ? escapeHtml(String(s == null ? "" : s))
      : String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
          ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])));

  function fmtWhen(iso) {
    if (!iso) return "—";
    try {
      return new Date(iso).toLocaleString([], {
        month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
      });
    } catch (_) { return iso; }
  }
  function fmtBytes(n) {
    if (n == null) return "";
    if (n < 1024) return `${n} B`;
    if (n < 1048576) return `${(n / 1024).toFixed(0)} KB`;
    return `${(n / 1048576).toFixed(1)} MB`;
  }
  const spinner = (t) => `<div class="spin-box"><span class="spin" role="status" aria-label="Loading"></span>${t ? esc(t) : ""}</div>`;

  function fileIcon(ct, name) {
    const n = (name || "").toLowerCase();
    ct = ct || "";
    if (n.endsWith(".pdf") || /pdf/.test(ct)) return "📕";
    if (/\.(xlsx|xls|csv)$/.test(n) || /sheet|excel|csv/.test(ct)) return "📊";
    if (/\.(docx|doc)$/.test(n) || /word|document/.test(ct)) return "📘";
    if (/\.html?$/.test(n) || /html/.test(ct)) return "🌐";
    return "📄";
  }
  function bizChip(slug) {
    if (!slug) return "";
    const biz = (window.HermesBiz && HermesBiz.find(slug)) || null;
    const label = biz ? `${biz.emoji || "🏢"} ${biz.name}` : slug;
    return `<span class="tk-chip">${esc(label)}</span>`;
  }
  const confirmDialog = (msg, opts) =>
    (window.hermesConfirm
      ? window.hermesConfirm(msg, opts)
      : Promise.resolve(window.confirm(msg)));

  // ── Files sub-list ─────────────────────────────────────────────────────────
  async function loadFiles() {
    const listEl = $("files-list");
    const emptyEl = $("lib-files-empty");
    if (!listEl) return;
    listEl.innerHTML = spinner("Loading files…");
    let files = [];
    try {
      const biz = (window.HermesBiz && HermesBiz.current) || "all";
      const r = await fetch(`/api/generated-files?entity_id=${encodeURIComponent(biz)}`);
      files = (await r.json()).files || [];
    } catch (e) {
      listEl.innerHTML = `<p class="hint" style="padding:8px">Couldn't load files: ${esc(e)}</p>`;
      return;
    }
    loadedFiles = true;
    if (!files.length) {
      listEl.innerHTML = "";
      if (emptyEl) emptyEl.hidden = false;
      return;
    }
    if (emptyEl) emptyEl.hidden = true;
    listEl.innerHTML = files.map(fileCard).join("");
  }

  // Long filenames have no spaces to wrap at — allow breaks after _ - . so they
  // wrap at word boundaries instead of mid-word.
  const breakable = (name) => esc(name).replace(/([_.\-])/g, "$1<wbr>");

  function fileCard(f) {
    const dl = `/api/generated-files/${encodeURIComponent(f.file_id)}/download`;
    const size = f.size_bytes != null ? `<span class="tk-chip">${esc(fmtBytes(f.size_bytes))}</span>` : "";
    return `
      <div class="tk-card" data-id="${esc(f.file_id)}">
        <div class="tk-card-main">
          <div class="tk-card-top">
            <div class="tk-card-name">${fileIcon(f.content_type, f.filename)} ${breakable(f.filename)}</div>
          </div>
          <div class="tk-card-chips">${bizChip(f.business)}${size}</div>
          <div class="tk-card-foot"><span class="tk-last">Created ${esc(fmtWhen(f.created_at))}</span></div>
        </div>
        <div class="tk-card-actions">
          <a class="tk-btn" href="${dl}" target="_blank" rel="noopener" data-uri="${esc(f.s3_uri || "")}" data-name="${esc(f.filename)}">Open ↗</a>
          <button class="tk-btn tk-danger" type="button" data-delfile="${esc(f.file_id)}">Delete</button>
        </div>
      </div>`;
  }

  async function deleteFile(id) {
    const ok = await confirmDialog("Delete this file permanently? The document itself is removed from storage.", {
      title: "Delete file", okText: "Delete", danger: true,
    });
    if (!ok) return;
    try {
      await fetch(`/api/generated-files/${encodeURIComponent(id)}`, { method: "DELETE" });
    } catch (_) { /* best-effort; a reload resyncs */ }
    loadFiles();
  }

  // ── Sub-tab switching ──────────────────────────────────────────────────────
  function setLibView(v) {
    view = v;
    document.querySelectorAll(".lib-subtab").forEach((b) =>
      b.classList.toggle("active", b.dataset.libview === v));
    const artWrap = $("lib-artefacts");
    const fileWrap = $("lib-files");
    if (artWrap) artWrap.hidden = v !== "artefacts";
    if (fileWrap) fileWrap.hidden = v !== "files";
    if (v === "files") { if (!loadedFiles) loadFiles(); }
    else if (typeof window._artefactsInit === "function") window._artefactsInit();
  }

  function bindOnce() {
    document.querySelectorAll(".lib-subtab").forEach((b) =>
      b.addEventListener("click", () => setLibView(b.dataset.libview)));

    $("files-list")?.addEventListener("click", (e) => {
      const del = e.target.closest("[data-delfile]");
      if (del) { e.preventDefault(); deleteFile(del.dataset.delfile); }
    });

    // The Library refresh buttons reload whichever sub-list is showing (the
    // Artefacts case is already handled by artefacts.js).
    ["artefacts-refresh-main", "artefacts-refresh-btn"].forEach((id) =>
      $(id)?.addEventListener("click", () => { if (view === "files") loadFiles(); }));

    if (window.HermesBiz) {
      HermesBiz.onChange(() => { loadedFiles = false; if (view === "files") loadFiles(); });
    }
  }

  bindOnce();
})();
