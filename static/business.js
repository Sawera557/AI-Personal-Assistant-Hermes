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
  };
})();

window.HermesBiz = HermesBiz;
