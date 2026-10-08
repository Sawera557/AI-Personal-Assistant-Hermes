/* Hermes · Multi-agents tab.
   Roster of specialist agents (GET /api/agents), each with a Mode dropdown, an
   editor (create / edit / reset / delete) and an in-tab chat drawer that runs
   turns through /api/chat/stream with agent_id + agent_mode — the server
   constrains the turn to the agent's prompt, tools and mode (hermes/agents.py). */
(function () {
  "use strict";

  let inited = false;
  let agents = [];
  let catalog = null;            // {groups, autonomies, apps}
  let editing = null;            // agent being edited (null = new)
  let draftModes = [];           // editor's working copy of modes
  let chatAgent = null;          // agent open in the drawer
  let busy = false;
  const sessions = {};           // agent_id -> session_id (one live thread per agent)
  const MODE_KEY = "hermes.ma.mode.";

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const uuid = () => (crypto.randomUUID ? crypto.randomUUID()
    : "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
        const r = (Math.random() * 16) | 0; return (c === "x" ? r : (r & 3) | 8).toString(16); }));

  const AUTONOMY = {
    readonly: { chip: "👁 Read-only", cls: "tk-chip-ro" },
    draft: { chip: "✎ Draft", cls: "tk-chip-draft" },
    full: { chip: "⚡ Acts (with approval)", cls: "tk-chip-full" },
  };

  // ---- data ---------------------------------------------------------------
  async function api(path, opts = {}) {
    const r = await fetch(path, {
      headers: opts.body ? { "Content-Type": "application/json" } : undefined, ...opts,
    });
    let j = {};
    try { j = await r.json(); } catch (_) { /* empty */ }
    if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
    return j;
  }

  async function loadAgents() {
    const msg = $("ma-page-msg");
    if (!agents.length) {
      $("ma-empty").hidden = true;
      $("ma-list").innerHTML = `<p class="hint ma-loading">Loading agents…</p>`;
    }
    try {
      const j = await api(`/api/agents?entity_id=${encodeURIComponent(HermesBiz.current)}`);
      agents = j.agents || [];
      msg.hidden = true;
      // Keep the main chat's Agent mode picker in sync (static/agent-mode.js).
      document.dispatchEvent(new CustomEvent("hermes:agents-changed"));
    } catch (e) {
      agents = [];
      msg.textContent = `Couldn't load agents: ${e.message}`;
      msg.hidden = false;
    }
    render();
  }

  async function loadCatalog() {
    if (catalog) return catalog;
    catalog = await api("/api/agents/tool-catalog");
    return catalog;
  }

  // ---- roster -------------------------------------------------------------
  function selectedMode(a) {
    const saved = localStorage.getItem(MODE_KEY + a.agent_id);
    const keys = (a.modes || []).map((m) => m.key);
    if (saved && keys.includes(saved)) return saved;
    return a.default_mode && keys.includes(a.default_mode) ? a.default_mode : keys[0];
  }

  function bizChip(slug) {
    if (!slug) return `<span class="tk-chip tk-chip-all">🏢 All businesses</span>`;
    const b = HermesBiz.find(slug);
    return `<span class="tk-chip">${b ? (b.emoji || "🏢") : "🏢"} ${esc(b ? b.name : slug)}</span>`;
  }

  function modeOptions(a, selected) {
    return (a.modes || []).map((m) =>
      `<option value="${esc(m.key)}"${m.key === selected ? " selected" : ""}>${esc(m.label)}</option>`).join("");
  }

  function card(a) {
    const mode = selectedMode(a);
    const m = (a.modes || []).find((x) => x.key === mode) || {};
    const aut = AUTONOMY[m.autonomy || "readonly"] || AUTONOMY.readonly;
    return `
      <div class="tk-card ma-card${a.enabled ? "" : " tk-card-off"}" data-id="${esc(a.agent_id)}">
        <div class="tk-card-main">
          <div class="tk-card-top">
            <div class="ma-card-title"><span class="ma-card-icon">${esc(a.icon || "🤖")}</span>
              <span class="tk-card-name">${esc(a.name)}</span></div>
            ${a.is_builtin ? `<span class="ma-builtin" title="Ships with Hermes">Built-in</span>` : ""}
          </div>
          <p class="ma-card-desc">${esc(a.description || "")}</p>
          <div class="tk-card-chips">
            ${bizChip(a.business)}
            <span class="tk-chip">🧰 ${(a.tools || []).length} tools</span>
            <span class="tk-chip ${aut.cls}">${aut.chip}</span>
          </div>
          <div class="ma-card-mode">
            <label>Mode</label>
            <select class="tk-input ma-card-select" data-act="mode" ${a.enabled ? "" : "disabled"}>${modeOptions(a, mode)}</select>
          </div>
          <div class="ma-card-actions">
            <button class="tk-btn tk-save" type="button" data-act="chat" ${a.enabled ? "" : "disabled"}>Chat</button>
            <button class="tk-btn tk-cancel" type="button" data-act="edit">Edit</button>
            <label class="ma-toggle" title="${a.enabled ? "Switch off" : "Switch on"}">
              <input type="checkbox" data-act="toggle" ${a.enabled ? "checked" : ""}/><span>${a.enabled ? "On" : "Off"}</span>
            </label>
          </div>
        </div>
      </div>`;
  }

  function render() {
    const list = $("ma-list");
    const empty = $("ma-empty");
    if (!agents.length) { list.innerHTML = ""; empty.hidden = false; return; }
    empty.hidden = true;
    if (HermesBiz.current !== "all") { list.innerHTML = agents.map(card).join(""); return; }
    const groups = new Map();
    agents.forEach((a) => {
      const k = a.business || "";
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(a);
    });
    list.innerHTML = [...groups.entries()].map(([k, group]) => {
      const b = k ? HermesBiz.find(k) : null;
      const label = k ? `${b ? (b.emoji || "🏢") : "🏢"} ${esc(b ? b.name : k)}` : "🏢 All businesses";
      return `<div class="tk-group-head">${label}<span class="tk-group-count">${group.length}</span></div>`
        + group.map(card).join("");
    }).join("");
  }

  function agentById(id) { return agents.find((a) => a.agent_id === id) || null; }

  async function onListClick(e) {
    const cardEl = e.target.closest(".ma-card");
    if (!cardEl) return;
    const a = agentById(cardEl.dataset.id);
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (!a || !act || act === "mode") return;
    if (act === "chat") openChat(a);
    else if (act === "edit") openEditor(a);
    else if (act === "toggle") {
      const on = e.target.checked;
      try {
        await api(`/api/agents/${a.agent_id}`, { method: "PATCH", body: JSON.stringify({ enabled: on }) });
        a.enabled = on;
        if (!on && chatAgent && chatAgent.agent_id === a.agent_id) closeChat();
      } catch (err) { e.target.checked = !on; alert(err.message); }
      render();
    }
  }

  function onListChange(e) {
    if (e.target.dataset.act !== "mode") return;
    const a = agentById(e.target.closest(".ma-card")?.dataset.id);
    if (!a) return;
    localStorage.setItem(MODE_KEY + a.agent_id, e.target.value);
    if (chatAgent && chatAgent.agent_id === a.agent_id) {
      $("ma-chat-mode").value = e.target.value;
      onChatModeChange();
    }
    render();
  }

  // ---- editor -------------------------------------------------------------
  function fillBusinessSelect(selected) {
    $("ma-business").innerHTML = HermesBiz.list.map((b) =>
      `<option value="${esc(b.slug)}"${b.slug === (selected || "all") ? " selected" : ""}>${b.emoji || "🏢"} ${esc(b.slug === "all" ? "All businesses (follows top bar)" : b.name)}</option>`).join("");
  }

  function renderTools(selected) {
    const set = new Set(selected || []);
    $("ma-tools").innerHTML = (catalog.groups || []).map((g) => `
      <div class="ma-tool-group">
        <div class="ma-tool-group-name">${esc(g.group)}</div>
        ${g.tools.map((t) => `
          <label class="ma-tool" title="${esc(t.description)}">
            <input type="checkbox" value="${esc(t.name)}"${set.has(t.name) ? " checked" : ""}/>
            <span class="ma-tool-name">${esc(t.name)}</span>
            <span class="ma-tool-desc">${esc(t.description)}</span>
          </label>`).join("")}
      </div>`).join("");
  }

  function selectedTools() {
    return [...document.querySelectorAll("#ma-tools input:checked")].map((c) => c.value);
  }

  function renderModes() {
    const auts = (catalog && catalog.autonomies) || ["readonly", "draft", "full"];
    $("ma-modes").innerHTML = draftModes.map((m, i) => `
      <div class="ma-mode-row" data-i="${i}">
        <div class="tk-row">
          <div class="tk-col"><input class="tk-input" data-f="label" placeholder="Mode name, e.g. Weekly brief" value="${esc(m.label)}"/></div>
          <div class="ma-col-aut"><select class="tk-input" data-f="autonomy">
            ${auts.map((a) => `<option value="${a}"${a === (m.autonomy || "readonly") ? " selected" : ""}>${esc((AUTONOMY[a] || {}).chip || a)}</option>`).join("")}
          </select></div>
          <button class="tk-close-btn ma-mode-del" type="button" data-f="del" title="Remove mode" aria-label="Remove mode">✕</button>
        </div>
        <textarea class="tk-textarea" rows="2" data-f="instructions" placeholder="Extra instructions for this mode">${esc(m.instructions || "")}</textarea>
        ${(m.tools && m.tools.length) || (m.allowed_apps && m.allowed_apps.length)
          ? `<div class="ma-mode-note">Advanced limits kept: ${m.tools && m.tools.length ? `tools ${esc(m.tools.join(", "))}` : ""}${m.allowed_apps && m.allowed_apps.length ? ` · apps ${esc(m.allowed_apps.join(", "))}` : ""}</div>` : ""}
      </div>`).join("");
    const dm = $("ma-default-mode");
    const cur = dm.value || (editing && editing.default_mode);
    dm.innerHTML = draftModes.map((m, i) =>
      `<option value="${i}">${esc(m.label || `Mode ${i + 1}`)}</option>`).join("");
    const idx = draftModes.findIndex((m) => m.key && m.key === cur);
    dm.value = String(idx >= 0 ? idx : 0);
  }

  function onModesInput(e) {
    const row = e.target.closest(".ma-mode-row");
    if (!row) return;
    const i = Number(row.dataset.i);
    const f = e.target.dataset.f;
    if (f === "label" || f === "instructions" || f === "autonomy") {
      draftModes[i][f] = e.target.value;
      if (f === "label") {
        const opt = $("ma-default-mode").options[i];
        if (opt) opt.textContent = e.target.value || `Mode ${i + 1}`;
      }
    }
  }

  function onModesClick(e) {
    if (e.target.dataset.f !== "del") return;
    const i = Number(e.target.closest(".ma-mode-row").dataset.i);
    draftModes.splice(i, 1);
    renderModes();
  }

  async function openEditor(a) {
    editing = a || null;
    try { await loadCatalog(); } catch (err) { alert(`Couldn't load the tool list: ${err.message}`); return; }
    $("ma-editor-title").textContent = a ? `Edit · ${a.name}` : "New agent";
    $("ma-icon").value = a ? (a.icon || "") : "🤖";
    $("ma-name").value = a ? a.name : "";
    $("ma-description").value = a ? (a.description || "") : "";
    $("ma-prompt").value = a ? a.system_prompt : "";
    $("ma-enabled").checked = a ? !!a.enabled : true;
    fillBusinessSelect(a ? a.business : (HermesBiz.current || "all"));
    renderTools(a ? a.tools : ["web_search", "web_fetch", "search_business_knowledge", "create_file"]);
    draftModes = a ? JSON.parse(JSON.stringify(a.modes || []))
      : [{ key: "", label: "Default", instructions: "", autonomy: "readonly" }];
    $("ma-default-mode").value = "";
    renderModes();
    $("ma-reset").hidden = !(a && a.is_builtin);
    $("ma-delete").hidden = !(a && !a.is_builtin);
    $("ma-editor-msg").textContent = "";
    $("ma-editor-overlay").hidden = false;
    setTimeout(() => $("ma-name").focus(), 30);
  }

  function closeEditor() {
    $("ma-editor-overlay").hidden = true;
    $("ma-editor-overlay").classList.remove("tk-full");
    editing = null;
  }

  function collect() {
    const name = $("ma-name").value.trim();
    const prompt = $("ma-prompt").value.trim();
    if (!name) return { error: "Give the agent a name." };
    if (!prompt) return { error: "Write the role prompt — it's what makes the agent an agent." };
    const modes = draftModes
      .map((m) => ({ ...m, label: (m.label || "").trim() }))
      .filter((m) => m.label);
    if (!modes.length) return { error: "Add at least one mode." };
    const dmIdx = Number($("ma-default-mode").value || 0);
    const dmLabel = (draftModes[dmIdx] || {}).label;
    const dmMode = modes.find((m) => m.label === (dmLabel || "").trim()) || modes[0];
    const biz = $("ma-business").value;
    return {
      body: {
        name, system_prompt: prompt,
        icon: $("ma-icon").value.trim(),
        description: $("ma-description").value.trim(),
        business: biz === "all" ? null : biz,
        tools: selectedTools(),
        modes,
        default_mode: dmMode.key || null,
        enabled: $("ma-enabled").checked,
      },
      dmLabel: dmMode.label,
    };
  }

  async function save() {
    const { body, error, dmLabel } = collect();
    const msg = $("ma-editor-msg");
    if (error) { msg.textContent = error; return; }
    msg.textContent = "Saving…";
    try {
      const path = editing ? `/api/agents/${editing.agent_id}` : "/api/agents";
      let { agent } = await api(path, { method: editing ? "PATCH" : "POST", body: JSON.stringify(body) });
      // New modes get their keys server-side; point default_mode at the right one.
      const want = (agent.modes || []).find((m) => m.label === dmLabel);
      if (want && agent.default_mode !== want.key) {
        ({ agent } = await api(`/api/agents/${agent.agent_id}`, {
          method: "PATCH", body: JSON.stringify({ default_mode: want.key }) }));
      }
      closeEditor();
      await loadAgents();
      if (chatAgent && chatAgent.agent_id === agent.agent_id) syncChatHeader(agentById(agent.agent_id) || agent);
    } catch (err) { msg.textContent = err.message; }
  }

  async function resetAgent() {
    if (!editing || !confirm(`Reset “${editing.name}” to its default prompt, tools and modes?`)) return;
    try {
      await api(`/api/agents/${editing.agent_id}/reset`, { method: "POST" });
      closeEditor();
      await loadAgents();
    } catch (err) { $("ma-editor-msg").textContent = err.message; }
  }

  async function deleteAgent() {
    if (!editing || !confirm(`Delete “${editing.name}”? This can't be undone.`)) return;
    try {
      await api(`/api/agents/${editing.agent_id}`, { method: "DELETE" });
      if (chatAgent && chatAgent.agent_id === editing.agent_id) closeChat();
      closeEditor();
      await loadAgents();
    } catch (err) { $("ma-editor-msg").textContent = err.message; }
  }

  // ---- chat drawer --------------------------------------------------------
  function syncChatHeader(a) {
    chatAgent = a;
    $("ma-chat-icon").textContent = a.icon || "🤖";
    $("ma-chat-name").textContent = a.name;
    $("ma-chat-mode").innerHTML = modeOptions(a, selectedMode(a));
    const biz = a.business ? HermesBiz.find(a.business) : HermesBiz.find(HermesBiz.current);
    $("ma-chat-biz").innerHTML = a.business
      ? `${biz ? biz.emoji || "🏢" : "🏢"} ${esc(biz ? biz.name : a.business)}`
      : `🏢 ${esc(biz ? biz.name : "All businesses")} <span class="ma-dim">(top bar)</span>`;
    describeMode();
  }

  function currentMode() {
    const key = $("ma-chat-mode").value;
    return (chatAgent.modes || []).find((m) => m.key === key) || {};
  }

  function describeMode() {
    const m = currentMode();
    const aut = AUTONOMY[m.autonomy || "readonly"] || AUTONOMY.readonly;
    $("ma-mode-desc").innerHTML = `<span class="tk-chip ${aut.cls}">${aut.chip}</span> ${esc(m.instructions || "")}`;
  }

  function openChat(a) {
    const switching = !chatAgent || chatAgent.agent_id !== a.agent_id;
    syncChatHeader(a);
    if (switching) {
      $("ma-chat-log").innerHTML = "";
      sessions[a.agent_id] = sessions[a.agent_id] || uuid();
      addSys(`Chatting with ${a.name}. Each message uses its prompt, its ${(a.tools || []).length} tools, and the mode above.`);
    }
    $("ma-drawer").hidden = false;
    $("multiagents-view").classList.add("ma-chat-open");
    setTimeout(() => $("ma-input").focus(), 30);
  }

  function closeChat() {
    $("ma-drawer").hidden = true;
    $("multiagents-view").classList.remove("ma-chat-open");
  }

  function newConversation() {
    if (!chatAgent) return;
    sessions[chatAgent.agent_id] = uuid();
    $("ma-chat-log").innerHTML = "";
    addSys("New conversation.");
  }

  function onChatModeChange() {
    if (!chatAgent) return;
    localStorage.setItem(MODE_KEY + chatAgent.agent_id, $("ma-chat-mode").value);
    describeMode();
    addSys(`Mode → ${currentMode().label || $("ma-chat-mode").value}`);
    render();
  }

  function scrollLog() { const l = $("ma-chat-log"); l.scrollTop = l.scrollHeight; }

  function addSys(text) {
    const d = document.createElement("div");
    d.className = "ma-msg ma-sys";
    d.textContent = text;
    $("ma-chat-log").appendChild(d);
    scrollLog();
  }

  function addUser(text) {
    const d = document.createElement("div");
    d.className = "ma-msg ma-user";
    d.textContent = text;
    $("ma-chat-log").appendChild(d);
    scrollLog();
  }

  function addAssistant() {
    const d = document.createElement("div");
    d.className = "ma-msg ma-bot";
    d.innerHTML = `<div class="ma-tools-used"></div><div class="ma-body"><span class="ma-typing">Thinking…</span></div><div class="ma-meta"></div>`;
    $("ma-chat-log").appendChild(d);
    scrollLog();
    return d;
  }

  function md(text) {
    if (typeof renderMarkdown === "function") return renderMarkdown(text);
    if (window.marked && window.DOMPurify) return window.DOMPurify.sanitize(window.marked.parse(text || ""));
    return esc(text).replace(/\n/g, "<br>");
  }

  async function send(e) {
    e.preventDefault();
    if (busy || !chatAgent) return;
    const text = $("ma-input").value.trim();
    if (!text) return;
    $("ma-input").value = "";
    addUser(text);
    const bubble = addAssistant();
    const body = bubble.querySelector(".ma-body");
    const toolsEl = bubble.querySelector(".ma-tools-used");
    busy = true;
    $("ma-send").disabled = true;

    const fd = new FormData();
    fd.append("session_id", sessions[chatAgent.agent_id] || (sessions[chatAgent.agent_id] = uuid()));
    fd.append("entity_id", chatAgent.business || HermesBiz.current || "all");
    fd.append("message", text);
    fd.append("effort", "auto");
    fd.append("agent_id", chatAgent.agent_id);
    fd.append("agent_mode", $("ma-chat-mode").value);

    let reply = "";
    let gotText = false;
    let thinkingChars = 0;
    try {
      const r = await fetch("/api/chat/stream", { method: "POST", body: fd });
      if (!r.ok || !r.body) {
        let msg = `HTTP ${r.status}`;
        try { msg = (await r.json()).error || msg; } catch (_) { /* not json */ }
        throw new Error(msg);
      }
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const chunk = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const line = chunk.split("\n").find((l) => l.startsWith("data: "));
          if (!line) continue;
          let ev;
          try { ev = JSON.parse(line.slice(6)); } catch (_) { continue; }
          if (ev.type === "thinking") {
            if (!gotText) {
              thinkingChars += (ev.text || "").length;
              body.innerHTML = `<span class="ma-typing">Reasoning… (${thinkingChars.toLocaleString()} chars)</span>`;
            }
          } else if (ev.type === "text") {
            if (!gotText) { body.textContent = ""; gotText = true; }
            reply += ev.text;
            body.innerHTML = md(reply);
            scrollLog();
          } else if (ev.type === "tool_call") {
            const chip = document.createElement("span");
            chip.className = "ma-tool-chip";
            chip.textContent = ev.label || ev.tool;
            chip.title = ev.tool;
            toolsEl.appendChild(chip);
            scrollLog();
          } else if (ev.type === "done") {
            reply = ev.reply || reply;
            body.innerHTML = reply ? md(reply) : `<span class="ma-dim">(no reply)</span>`;
            const bits = [];
            if (ev.model_used) bits.push(esc(ev.model_used));
            if (typeof ev.cost_usd === "number") bits.push(`$${ev.cost_usd.toFixed(4)}`);
            if (ev.tool_calls && ev.tool_calls.length) bits.push(`${ev.tool_calls.length} tool call${ev.tool_calls.length === 1 ? "" : "s"}`);
            bubble.querySelector(".ma-meta").innerHTML = bits.join(" · ");
          } else if (ev.type === "error") {
            throw new Error(ev.error || "Agent error");
          }
        }
      }
    } catch (err) {
      body.innerHTML = `<span class="ma-err">⚠ ${esc(err.message)}</span>`;
    } finally {
      busy = false;
      $("ma-send").disabled = false;
      scrollLog();
    }
  }

  // ---- wiring -------------------------------------------------------------
  function bindOnce() {
    HermesBiz.onChange(() => {
      if (!inited) return;
      loadAgents();
      if (chatAgent && !chatAgent.business) syncChatHeader(chatAgent);
    });
    ["ma-new-btn", "ma-new-btn-m", "ma-new-btn-empty"].forEach((id) =>
      $(id)?.addEventListener("click", () => openEditor(null)));
    $("ma-list").addEventListener("click", onListClick);
    $("ma-list").addEventListener("change", onListChange);
    $("ma-editor-close").addEventListener("click", closeEditor);
    $("ma-editor-cancel").addEventListener("click", closeEditor);
    $("ma-editor-save").addEventListener("click", save);
    $("ma-reset").addEventListener("click", resetAgent);
    $("ma-delete").addEventListener("click", deleteAgent);
    $("ma-editor-expand").addEventListener("click", () =>
      $("ma-editor-overlay").classList.toggle("tk-full"));
    $("ma-editor-overlay").addEventListener("click", (e) => {
      if (e.target === $("ma-editor-overlay")) closeEditor();
    });
    $("ma-add-mode").addEventListener("click", () => {
      draftModes.push({ key: "", label: "", instructions: "", autonomy: "readonly" });
      renderModes();
      const rows = document.querySelectorAll("#ma-modes .ma-mode-row input[data-f=label]");
      rows[rows.length - 1]?.focus();
    });
    $("ma-modes").addEventListener("input", onModesInput);
    $("ma-modes").addEventListener("change", onModesInput);
    $("ma-modes").addEventListener("click", onModesClick);
    $("ma-chat-close").addEventListener("click", closeChat);
    $("ma-chat-new").addEventListener("click", newConversation);
    $("ma-chat-mode").addEventListener("change", onChatModeChange);
    $("ma-composer").addEventListener("submit", send);
    $("ma-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("ma-composer").requestSubmit(); }
    });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      if (!$("ma-editor-overlay").hidden) closeEditor();
    });
  }

  window._multiAgentsInit = function () {
    if (!inited) { bindOnce(); inited = true; }
    loadAgents();
  };
})();
