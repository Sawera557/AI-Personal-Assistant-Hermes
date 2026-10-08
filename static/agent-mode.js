/* Hermes · Agent mode for the main chat.
   A picker above the composer: "Hermes (default)" or any enabled Multi-agents
   agent + one of its modes. While an agent is picked, every message from the
   main chat carries agent_id + agent_mode (app.js submit handler), so the
   server runs the turn AS that agent — its prompt, only its tools, the mode's
   autonomy (hermes/agents.py). The choice persists across reloads. */
(function () {
  "use strict";

  const STORE = "hermes.agentMode";          // {agent_id}
  // Last roster seen. Rendered instantly on page load so the saved agent is
  // active from the first keystroke — otherwise, while /api/agents is still in
  // flight, the picker reads "Hermes (default)" and a message sent then would
  // silently go to plain Hermes. The server re-checks the agent on every turn.
  const CACHE = "hermes.agentMode.cache";
  const MODE_KEY = "hermes.ma.mode.";        // per-agent mode, shared with the Multi-agents tab
  const AUT = { readonly: "Read-only", draft: "Draft only", full: "Acts with your approval" };

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  let agents = [];
  let loaded = false;

  function selected() {
    const id = $("agent-mode-agent")?.value;
    return id ? agents.find((a) => a.agent_id === id) || null : null;
  }

  function modeFor(a) {
    const keys = (a.modes || []).map((m) => m.key);
    const saved = localStorage.getItem(MODE_KEY + a.agent_id);
    if (saved && keys.includes(saved)) return saved;
    return keys.includes(a.default_mode) ? a.default_mode : keys[0];
  }

  // "NH - Market Analyst": the business slug is already the owner's shorthand
  // (nh, gak, gif, afk, gakc, per). All-business agents get no prefix.
  const label = (a) => (a.business ? `${a.business.toUpperCase()} - ${a.name}` : a.name);

  function renderAgents() {
    const sel = $("agent-mode-agent");
    if (!sel) return;
    const keep = sel.value || (JSON.parse(localStorage.getItem(STORE) || "{}").agent_id || "");
    const list = agents.filter((a) => a.enabled)
      .sort((x, y) => label(x).localeCompare(label(y)));
    sel.innerHTML = `<option value="">⚡ Hermes (default)</option>` +
      list.map((a) => `<option value="${esc(a.agent_id)}">${esc(label(a))}</option>`).join("");
    sel.value = agents.some((a) => a.agent_id === keep && a.enabled) ? keep : "";
    renderMode();
  }

  function renderMode() {
    const a = selected();
    const modeSel = $("agent-mode-mode");
    const info = $("agent-mode-info");
    const bar = $("agent-mode-bar");
    const input = $("input");
    bar.classList.toggle("is-agent", !!a);
    if (!a) {
      modeSel.hidden = true;
      info.innerHTML = "";
      if (input) input.placeholder = "Message Hermes…";
      return;
    }
    const mk = modeFor(a);
    modeSel.innerHTML = (a.modes || []).map((m) =>
      `<option value="${esc(m.key)}"${m.key === mk ? " selected" : ""}>${esc(m.label)}</option>`).join("");
    modeSel.hidden = false;
    const m = (a.modes || []).find((x) => x.key === mk) || {};
    info.textContent = `${AUT[m.autonomy || "readonly"] || ""} · ${(a.tools || []).length} tools`;
    info.title = m.instructions || a.description || "";
    if (input) input.placeholder = `Message ${a.name}…`;
  }

  function currentSessionLock() {
    try { return HermesBiz.lockFor(sessionId); } catch (_) { return null; }  // sessionId: app.js global
  }

  function onAgentChange() {
    const a = selected();
    localStorage.setItem(STORE, JSON.stringify({ agent_id: a ? a.agent_id : "" }));
    // A business-bound agent always runs in its own business. If the open
    // conversation is already locked to a different one, continuing it would
    // split the thread across two businesses — start a fresh conversation.
    const lock = currentSessionLock();
    if (a && a.business && lock && lock !== a.business && typeof startNewConversation === "function") {
      startNewConversation();
    }
    renderMode();
    $("input")?.focus();
  }

  function onModeChange() {
    const a = selected();
    if (!a) return;
    localStorage.setItem(MODE_KEY + a.agent_id, $("agent-mode-mode").value);
    renderMode();
  }

  async function load() {
    try {
      const r = await fetch("/api/agents");
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || r.status);
      agents = j.agents || [];
      loaded = true;
      try { localStorage.setItem(CACHE, JSON.stringify(agents)); } catch (_) { /* quota */ }
      renderAgents();
    } catch (e) {
      console.warn("Agent mode: couldn't load agents", e);
    }
  }

  // Public: what the chat submit should send, or null for plain Hermes.
  window.HermesAgentMode = {
    current() {
      const a = selected();
      if (!a) return null;
      return { agent_id: a.agent_id, mode: $("agent-mode-mode").value || modeFor(a) };
    },
    reload: load,
  };

  function init() {
    if (!$("agent-mode-bar")) return;
    $("agent-mode-agent").addEventListener("change", onAgentChange);
    $("agent-mode-mode").addEventListener("change", onModeChange);
    // Pick up roster edits made in the Multi-agents tab.
    document.addEventListener("hermes:agents-changed", () => { if (loaded) load(); });
    try { agents = JSON.parse(localStorage.getItem(CACHE) || "[]"); } catch (_) { agents = []; }
    if (agents.length) renderAgents();
    load();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
