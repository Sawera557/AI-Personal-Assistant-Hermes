// Artefacts — saved, live HTML views ("canvases") bound to a data source.
//
// Lists the business's artefacts as cards (title + source-type chip + last-synced
// time). Opening one loads /api/artefacts/{id}/view?refresh=1 in a sandboxed
// iframe, so the canvas RE-FETCHES its source and re-renders every time it's
// opened — always reflecting the current data. "Sync now" forces a refresh;
// delete removes it. Talks to /api/artefacts/*. Reuses the Scheduled-Tasks (tk-*)
// card/modal styles and mirrors tasks.js's lazy-init + HermesBiz scoping.
(function () {
  "use strict";

  let inited = false;
  let openArt = null; // artefact open in the canvas modal

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

  const spinner = (text) => `<div class="spin-box"><span class="spin" role="status" aria-label="Loading"></span>${text ? esc(text) : ""}</div>`;

  const SOURCE_META = {
    sheets: { icon: "📊", label: "Google Sheet" },
    drive: { icon: "📁", label: "Drive file" },
    docs: { icon: "📄", label: "Google Doc" },
    reviews: { icon: "⭐", label: "Reviews report" },
  };
  function sourceChip(type) {
    const m = SOURCE_META[type] || { icon: "🗂", label: type || "source" };
    return `<span class="tk-chip af-chip">${m.icon} ${esc(m.label)}</span>`;
  }
  function bizChip(slug) {
    if (!slug) return "";
    const biz = (window.HermesBiz && HermesBiz.find(slug)) || null;
    const label = biz ? `${biz.emoji || "🏢"} ${biz.name}` : slug;
    return `<span class="tk-chip">${esc(label)}</span>`;
  }

  // ── In-app confirm dialog (replaces the browser's confirm()) ───────────────
  let _dlg = null;
  function ensureDialog() {
    if (_dlg) return _dlg;
    const o = document.createElement("div");
    o.className = "appdlg-overlay";
    o.hidden = true;
    o.innerHTML =
      `<div class="appdlg" role="dialog" aria-modal="true" aria-labelledby="afdlg-title">
         <div class="appdlg-title" id="afdlg-title"></div>
         <div class="appdlg-msg" id="afdlg-msg"></div>
         <div class="appdlg-actions">
           <button class="tk-btn tk-cancel" id="afdlg-cancel" type="button">Cancel</button>
           <button class="tk-btn tk-save" id="afdlg-ok" type="button">OK</button>
         </div>
       </div>`;
    document.body.appendChild(o);
    _dlg = {
      o, title: o.querySelector("#afdlg-title"), msg: o.querySelector("#afdlg-msg"),
      cancel: o.querySelector("#afdlg-cancel"), ok: o.querySelector("#afdlg-ok"),
    };
    return _dlg;
  }
  function appConfirm(message, { title = "Please confirm", okText = "Confirm", danger = false } = {}) {
    const d = ensureDialog();
    d.title.textContent = title;
    d.msg.textContent = message || "";
    d.ok.textContent = okText;
    d.ok.classList.toggle("tk-danger", !!danger);
    d.o.hidden = false;
    return new Promise((resolve) => {
      const done = (v) => { cleanup(); d.o.hidden = true; resolve(v); };
      const onOk = () => done(true), onCancel = () => done(false);
      const onKey = (e) => { if (e.key === "Escape") done(false); else if (e.key === "Enter") done(true); };
      const onBack = (e) => { if (e.target === d.o) done(false); };
      function cleanup() {
        d.ok.removeEventListener("click", onOk); d.cancel.removeEventListener("click", onCancel);
        document.removeEventListener("keydown", onKey, true); d.o.removeEventListener("click", onBack);
      }
      d.ok.addEventListener("click", onOk); d.cancel.addEventListener("click", onCancel);
      document.addEventListener("keydown", onKey, true); d.o.addEventListener("click", onBack);
      d.ok.focus();
    });
  }

  // ── List ───────────────────────────────────────────────────────────────────
  async function loadArtefacts() {
    const listEl = $("artefacts-list");
    const emptyEl = $("artefacts-empty");
    if (!listEl) return;
    if (!listEl.dataset.loaded) listEl.innerHTML = spinner("Loading artefacts…");
    let arts = [];
    try {
      const r = await fetch(`/api/artefacts?entity_id=${encodeURIComponent(HermesBiz.current)}`);
      const j = await r.json();
      arts = j.artefacts || [];
    } catch (e) {
      listEl.innerHTML = `<p class="hint" style="padding:8px">Couldn't load artefacts: ${esc(e)}</p>`;
      return;
    }
    listEl.dataset.loaded = "1";
    if (!arts.length) {
      listEl.innerHTML = "";
      if (emptyEl) emptyEl.hidden = false;
      return;
    }
    if (emptyEl) emptyEl.hidden = true;
    listEl.innerHTML = arts.map(artefactCard).join("");
  }

  function artefactCard(a) {
    const synced = a.last_synced_at
      ? `<span class="tk-last">↻ Synced ${esc(fmtWhen(a.last_synced_at))}</span>`
      : `<span class="tk-last">Not synced yet</span>`;
    const err = a.last_error
      ? `<div class="tk-card-window af-err-chip" title="${esc(a.last_error)}">⚠ ${esc(a.last_error)}</div>`
      : "";
    return `
      <div class="tk-card" data-id="${esc(a.artefact_id)}">
        <div class="tk-card-main" data-open="${esc(a.artefact_id)}">
          <div class="tk-card-top">
            <div class="tk-card-name">${esc(a.title)}</div>
          </div>
          <div class="tk-card-chips">${sourceChip(a.source_type)}${bizChip(a.business)}</div>
          ${err}
          <div class="tk-card-foot">${synced}</div>
        </div>
        <div class="tk-card-actions">
          <button class="tk-btn" type="button" data-sync="${esc(a.artefact_id)}">↻ Sync</button>
          <button class="tk-btn tk-danger" type="button" data-delete="${esc(a.artefact_id)}">Delete</button>
        </div>
      </div>`;
  }

  // ── Canvas modal ─────────────────────────────────────────────────────────
  function viewUrl(id, refresh) {
    return `/api/artefacts/${encodeURIComponent(id)}/view${refresh ? "?refresh=1&_=" + Date.now() : ""}`;
  }

  async function openCanvas(id) {
    let art = null;
    try {
      const r = await fetch(`/api/artefacts/${encodeURIComponent(id)}`);
      if (r.ok) art = (await r.json()).artefact;
    } catch (_) { /* best-effort; the iframe still loads by id */ }
    openArt = art || { artefact_id: id, title: "Artifact" };
    $("af-detail-name").textContent = openArt.title || "Artifact";
    const meta = SOURCE_META[openArt.source_type];
    $("af-detail-sub").textContent = meta ? meta.label : "";
    $("af-detail-msg").textContent = "Loading the latest data…";
    // refresh=1 so opening always re-syncs from the source.
    $("af-canvas").src = viewUrl(id, true);
    $("af-detail-overlay").hidden = false;
  }

  function closeCanvas() {
    $("af-detail-overlay").hidden = true;
    $("af-canvas").src = "about:blank";
    openArt = null;
  }

  async function syncOpen() {
    if (!openArt) return;
    const id = openArt.artefact_id;
    $("af-detail-msg").textContent = "Syncing…";
    try {
      await fetch(`/api/artefacts/${encodeURIComponent(id)}/sync`, { method: "POST" });
    } catch (_) { /* the reload below still shows a fresh render or error canvas */ }
    $("af-canvas").src = viewUrl(id, true);
    $("af-detail-msg").textContent = "Synced " + fmtWhen(new Date().toISOString());
    loadArtefacts();
  }

  async function deleteArtefact(id) {
    const ok = await appConfirm("Delete this artefact? Its source document is not affected.", {
      title: "Delete artefact", okText: "Delete", danger: true,
    });
    if (!ok) return;
    try {
      await fetch(`/api/artefacts/${encodeURIComponent(id)}`, { method: "DELETE" });
    } catch (_) { /* best-effort; a reload resyncs */ }
    if (openArt && openArt.artefact_id === id) closeCanvas();
    loadArtefacts();
  }

  // ── Wiring ───────────────────────────────────────────────────────────────
  function bindOnce() {
    HermesBiz.bind($("hermes-biz-select-artefacts"), $("hermes-biz-badge-artefacts"));
    HermesBiz.onChange(() => { if (inited) loadArtefacts(); });

    $("artefacts-refresh-btn")?.addEventListener("click", loadArtefacts);
    $("artefacts-refresh-main")?.addEventListener("click", loadArtefacts);

    // Delegated: open canvas / sync / delete from the card.
    $("artefacts-list")?.addEventListener("click", (e) => {
      const sync = e.target.closest("[data-sync]");
      if (sync) { e.stopPropagation(); syncCard(sync.dataset.sync); return; }
      const del = e.target.closest("[data-delete]");
      if (del) { e.stopPropagation(); deleteArtefact(del.dataset.delete); return; }
      const open = e.target.closest("[data-open]");
      if (open) openCanvas(open.dataset.open);
    });

    // Canvas modal controls
    $("af-detail-close")?.addEventListener("click", closeCanvas);
    $("af-detail-overlay")?.addEventListener("click", (e) => {
      if (e.target === $("af-detail-overlay")) closeCanvas();
    });
    $("af-sync-now")?.addEventListener("click", syncOpen);
    $("af-open-tab")?.addEventListener("click", () => {
      if (openArt) window.open(viewUrl(openArt.artefact_id, true), "_blank", "noopener");
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !$("af-detail-overlay").hidden) closeCanvas();
    });
  }

  // Sync from a card (not the open modal) — refresh the row afterwards.
  async function syncCard(id) {
    try {
      await fetch(`/api/artefacts/${encodeURIComponent(id)}/sync`, { method: "POST" });
    } catch (_) { /* best-effort */ }
    loadArtefacts();
  }

  window._artefactsInit = function () {
    if (!inited) { bindOnce(); inited = true; }
    loadArtefacts();
  };

  // Shared with library.js so the Files sub-list reuses the same in-app dialog.
  window.hermesConfirm = appConfirm;
})();
