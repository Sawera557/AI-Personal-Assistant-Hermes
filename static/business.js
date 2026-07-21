// Global business scope — single source of truth for which business (or "All
// Businesses") is active across the whole app. Both app.js (chat) and
// bills.js subscribe to this instead of owning their own business state.
//
// `entity_id` sent to the backend is always one of: a business slug (e.g.
// "afk"), or the sentinel "all".
const HermesBiz = (() => {
  const STORAGE_KEY = "hermesActiveBusiness";
  const LOCK_KEY = "hermesSessionBiz"; // { [sessionId]: businessSlug }

  const ALL = { slug: "all", name: "All Businesses", emoji: "🏢", color: "#9aa3b2", connected: true };

  let list = [ALL];
  let current = localStorage.getItem(STORAGE_KEY) || "all";
  const listeners = [];

  function _loadLocks() {
    try { return JSON.parse(localStorage.getItem(LOCK_KEY) || "{}"); }
    catch (_) { return {}; }
  }
  function _saveLocks(locks) {
    localStorage.setItem(LOCK_KEY, JSON.stringify(locks));
  }

  function notify() {
    listeners.forEach((cb) => {
      try { cb(current); } catch (e) { console.error("HermesBiz listener error:", e); }
    });
  }

  async function load() {
    try {
      const r = await fetch("/api/businesses");
      const j = await r.json();
      list = [ALL, ...(j.businesses || [])];
    } catch (e) {
      console.error("HermesBiz.load:", e);
      list = [ALL];
    }
    // Persisted selection may no longer exist (deleted business) — fall back.
    if (!list.find((b) => b.slug === current)) current = "all";
    notify();
    return list;
  }

  function find(slug) {
    return list.find((b) => b.slug === slug) || null;
  }

  function set(slug) {
    if (!slug || slug === current) return;
    current = slug;
    localStorage.setItem(STORAGE_KEY, slug);
    notify();
  }

  function onChange(cb) {
    listeners.push(cb);
  }

  // ---- Per-conversation lock: a chat's business is fixed at creation and
  // never changes mid-thread, even if the dropdown flips while it's open.
  // (Server-side enforcement is authoritative; this is the client mirror so
  // /api/chat can send the right entity_id immediately, no round trip.)
  function lockFor(sessionId) {
    return _loadLocks()[sessionId] || null;
  }
  function setLock(sessionId, slug) {
    const locks = _loadLocks();
    locks[sessionId] = slug;
    _saveLocks(locks);
  }
  function clearLock(sessionId) {
    const locks = _loadLocks();
    delete locks[sessionId];
    _saveLocks(locks);
  }

  // ---- Render + bind a <select> (plus optional "not connected" badge) ----
  function renderSelect(selectEl, badgeEl) {
    if (!selectEl) return;
    selectEl.innerHTML = list
      .map((b) => {
        const label = `${b.emoji || "🏢"} ${b.name}`;
        return `<option value="${b.slug}"${b.slug === current ? " selected" : ""}>${label}</option>`;
      })
      .join("");
    selectEl.value = current;
    if (badgeEl) {
      const biz = find(current);
      const notConnected = !!biz && current !== "all" && !biz.connected;
      badgeEl.hidden = !notConnected;
      badgeEl.textContent = notConnected ? "⚠ Not connected yet" : "";
    }
  }

  function bind(selectEl, badgeEl) {
    if (!selectEl) return;
    renderSelect(selectEl, badgeEl);
    selectEl.addEventListener("change", () => set(selectEl.value));
    onChange(() => renderSelect(selectEl, badgeEl));
  }

  // ---- Render + bind the topbar's global business-scope switcher --------
  // Replaces the old per-view <select> pattern above: one control (a color
  // dot + name in `dotEl`/`nameEl`, plus a dropdown of rows in `menuEl`)
  // shared by every view. Open/close state is the caller's concern (see
  // app.js) — this only renders content and reacts to selection changes.
  function _escape(s) {
    return String(s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }
  function renderTopbar(dotEl, nameEl, menuEl) {
    const biz = find(current);
    const color = (biz && biz.color) || "#9aa3b2";
    if (dotEl) {
      dotEl.style.background = color;
      dotEl.style.boxShadow = `0 0 9px ${color}`;
    }
    if (nameEl) nameEl.textContent = (biz && biz.name) || "All Businesses";
    if (menuEl) {
      menuEl.innerHTML = list.map((b) => {
        const active = b.slug === current;
        const code = b.slug === "all" ? "GLOBAL SCOPE" : b.slug.toUpperCase();
        const check = active
          ? '<svg class="hud-biz-row-check" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.6"><polyline points="20 6 9 17 4 12"/></svg>'
          : "";
        return `<button type="button" class="hud-biz-row${active ? " active" : ""}" data-slug="${b.slug}" role="menuitem">` +
          `<span class="hud-biz-row-dot" style="background:${b.color || "#9aa3b2"}"></span>` +
          `<span class="hud-biz-row-text"><span class="hud-biz-row-name">${_escape(b.name)}</span><span class="hud-biz-row-code">${code}</span></span>` +
          `${check}</button>`;
      }).join("");
    }
  }
  function bindTopbar(dotEl, nameEl, menuEl) {
    renderTopbar(dotEl, nameEl, menuEl);
    if (menuEl) {
      menuEl.addEventListener("click", (e) => {
        const btn = e.target.closest("[data-slug]");
        if (btn) set(btn.dataset.slug);
      });
    }
    onChange(() => renderTopbar(dotEl, nameEl, menuEl));
  }

  return {
    load,
    get list() { return list; },
    get current() { return current; },
    find,
    set,
    onChange,
    lockFor,
    setLock,
    clearLock,
    renderSelect,
    bind,
    renderTopbar,
    bindTopbar,
  };
})();

window.HermesBiz = HermesBiz;
