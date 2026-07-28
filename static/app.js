// Hermes chat client — chat + conversation history sidebar.
const messagesEl = document.getElementById("messages");
const form = document.getElementById("composer");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send-btn");
const fileInput = document.getElementById("file-input");
const attachmentsEl = document.getElementById("attachments");
const resetBtn = document.getElementById("reset-btn");
const connectBtn = document.getElementById("connect-btn");
const connectApp = document.getElementById("connect-app");
const connectResult = document.getElementById("connect-result");
const convosEl = document.getElementById("convos");
const convosEmptyEl = document.getElementById("convos-empty");
const convoSearchInput = document.getElementById("convo-search-input");
const chatLockNoteEl = document.getElementById("chat-lock-note");

// Current history search query (empty = show all conversations).
let convoQuery = "";

// ---- Scroll behavior -----------------------------------------------------
// Auto-scroll to the newest message ONLY while the user is already at the
// bottom. The moment they scroll up to read, we stop following — and we don't
// pull them back down until they return to the bottom themselves.
let stickToBottom = true;
function isAtBottom() {
  return messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 80;
}
function scrollToBottom() {
  stickToBottom = true;
  messagesEl.scrollTop = messagesEl.scrollHeight;
}
// The user's own scrolling is the single source of truth for stickToBottom.
messagesEl.addEventListener("scroll", () => {
  stickToBottom = isAtBottom();
});

const GREETING = "Hi! I'm connected to your business apps. How can I help you?";

// Each page load starts a fresh, blank conversation. Past chats live in the
// History sidebar and are opened only when the user clicks one.
let sessionId = crypto.randomUUID();
localStorage.setItem("hermes_session", sessionId);

let pendingFiles = [];

// ---- Business scope for chat --------------------------------------------
// A conversation's business is locked at its first message and never changes
// mid-thread, even if the global dropdown is flipped while it's open. The
// server enforces this too (authoritative) — this is the client mirror so
// /api/chat sends the right value immediately, and the sidebar can show which
// business each past chat belongs to. "all" is stored (never a bare null) to
// mean "no restriction", so a thread that started open stays open forever.
function entityIdForSend() {
  return HermesBiz.lockFor(sessionId) || HermesBiz.current;
}

// Surfaces the mismatch above the composer: without this, switching the
// dropdown mid-conversation looks like it re-scopes the chat, but it silently
// doesn't — messages keep saving under whatever business locked the thread at
// its first message. Only shown when there IS a mismatch, so it stays quiet
// the rest of the time.
function updateLockNote() {
  if (!chatLockNoteEl) return;
  const lock = HermesBiz.lockFor(sessionId);
  if (!lock || lock === HermesBiz.current) {
    chatLockNoteEl.hidden = true;
    return;
  }
  const lockedBiz = HermesBiz.find(lock);
  const lockedName = lock === "all" ? "All Businesses" : (lockedBiz ? lockedBiz.name : lock);
  chatLockNoteEl.hidden = false;
  chatLockNoteEl.textContent =
    `This chat is scoped to ${lockedName} — switching the dropdown won't change it. ` +
    `Click "+ New conversation" to start a fresh chat for your new selection.`;
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

// Make every rendered link open safely in a new tab.
if (window.DOMPurify) {
  window.DOMPurify.addHook("afterSanitizeAttributes", (node) => {
    if (node.tagName === "A") {
      node.setAttribute("target", "_blank");
      node.setAttribute("rel", "noopener noreferrer");
    }
  });
}

// Full markdown -> sanitized HTML for assistant messages.
function renderMarkdown(text) {
  const src = String(text || "");
  if (window.marked && window.DOMPurify) {
    return window.DOMPurify.sanitize(
      window.marked.parse(src, { gfm: true, breaks: true })
    );
  }
  return renderBasic(src); // fallback if the CDN didn't load
}

// Minimal fallback renderer (headings, bold, code, lists, links).
function renderBasic(text) {
  let html = escapeHtml(String(text));
  html = html.replace(/```([\s\S]*?)```/g, (_, c) => `<pre>${c}</pre>`);
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  html = html.replace(/^#{1,6}\s+(.*)$/gm, "<strong>$1</strong>");
  html = html.replace(/^\s*[-*]\s+(.*)$/gm, "• $1");
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return html.replace(/\n/g, "<br>");
}

// User text: escape + keep line breaks (no markdown needed, and safer).
function renderUserText(text) {
  return escapeHtml(String(text)).replace(/\n/g, "<br>");
}

// Apply syntax highlighting to any code blocks inside a container.
function highlightCode(container) {
  if (!window.hljs || !container) return;
  container.querySelectorAll("pre code").forEach((el) => {
    try { window.hljs.highlightElement(el); } catch (_) { /* ignore */ }
  });
}

function addMessage(role, html) {
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  wrap.innerHTML = `<div class="bubble">${html}</div>`;
  messagesEl.appendChild(wrap);
  if (stickToBottom) messagesEl.scrollTop = messagesEl.scrollHeight;
  return wrap;
}

// Renders a media reference (image/file) a tool call produced — e.g. WATI
// media saved to S3 by wati_get_media. The actual URL is resolved lazily by
// hydrateMedia() since only the durable s3:// URI is persisted in history.
function renderToolMedia(media) {
  if (!media || !media.s3_uri) return "";
  const label = escapeHtml(media.filename || "file");
  const uri = escapeHtml(media.s3_uri);
  if ((media.content_type || "").startsWith("image/")) {
    return `<div class="tool-media"><img class="tool-media-img" data-s3-uri="${uri}" alt="${label}" title="${label}"></div>`;
  }
  return `<div class="tool-media"><a class="tool-media-file" data-s3-uri="${uri}" href="#" title="${label}">📎 ${label}</a></div>`;
}

function renderToolTrace(tools) {
  if (!tools || !tools.length) return "";
  const items = tools.map((t) => {
    const cls = t.ok ? "tool-ok" : "tool-err";
    const dot = t.ok ? "✓" : "✗";
    const label = t.label || `⚙️ ${t.tool}`;
    return `<details><summary><span class="${cls}">${dot}</span> ${escapeHtml(label)}</summary>` +
      `<pre title="${escapeHtml(t.tool)}">${escapeHtml(JSON.stringify(t.input, null, 2))}</pre></details>${renderToolMedia(t.media)}`;
  });
  return `<div class="tools">${items.join("")}</div>`;
}

// Resolves every [data-s3-uri] placeholder inside `container` to a fresh
// presigned URL. Called after any innerHTML that may include renderToolMedia()
// output — both the live streaming "done" handler and history replay.
async function hydrateMedia(container) {
  const nodes = container.querySelectorAll("[data-s3-uri]");
  await Promise.all(Array.from(nodes).map(async (el) => {
    const uri = el.getAttribute("data-s3-uri");
    if (!uri) return;
    try {
      const res = await fetch(`/api/media/presign?uri=${encodeURIComponent(uri)}`);
      if (!res.ok) return;
      const data = await res.json();
      if (!data.url) return;
      if (el.tagName === "IMG") el.src = data.url;
      else el.href = data.url;
    } catch (_) { /* leave placeholder as-is */ }
  }));
}

// ---- Image lightbox — click a chat image (e.g. WATI media) to view it full-size ----
const lightboxEl = document.createElement("div");
lightboxEl.className = "lightbox-overlay";
lightboxEl.innerHTML = `<img class="lightbox-img" alt="">`;
document.body.appendChild(lightboxEl);
const lightboxImg = lightboxEl.querySelector(".lightbox-img");

function openLightbox(src, alt) {
  lightboxImg.src = src;
  lightboxImg.alt = alt || "";
  lightboxEl.classList.add("open");
}
function closeLightbox() {
  lightboxEl.classList.remove("open");
  lightboxImg.src = "";
}
lightboxEl.addEventListener("click", closeLightbox);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeLightbox();
});

// Event delegation: images render asynchronously (streaming + history replay),
// so listen on the whole message list instead of binding per-<img>.
messagesEl.addEventListener("click", (e) => {
  const img = e.target.closest(".tool-media-img");
  if (img && img.src) openLightbox(img.src, img.alt);
});

function renderThinking(thinking) {
  if (!thinking) return "";
  return `<div class="thinking"><details><summary>Reasoning</summary>` +
    `<pre>${escapeHtml(thinking)}</pre></details></div>`;
}

// Per-response token/cost footer, shown under each assistant reply.
// `usage` = {input_tokens, output_tokens, ...}; `cost` = USD number.
function renderUsage(usage, cost, model) {
  if (!usage) return "";
  const tokens = (usage.input_tokens || 0) + (usage.output_tokens || 0);
  if (!tokens && !cost) return "";
  const money = typeof cost === "number" ? ` · $${cost.toFixed(4)}` : "";
  const modelTag = model ? ` · ${escapeHtml(model)}` : "";
  return `<div class="usage-line" title="${(usage.input_tokens || 0).toLocaleString()} in / ` +
    `${(usage.output_tokens || 0).toLocaleString()} out">` +
    `🎫 ${tokens.toLocaleString()} tokens${money}${modelTag}</div>`;
}

// Live, uncollapsed "Reasoning" view shown WHILE a turn is still fetching its
// response. `entries` is an ordered mix of two kinds, in the order they
// actually happened:
//   {kind: "thought", text}  — Claude's REAL streamed reasoning (only on
//                              models/turns where it chose to think out loud)
//   {kind: "tool", label}    — a tool call in flight/completed
// The most recent entry pulses (still happening); earlier ones settle down
// (✓ for tools, dimmed for thoughts). Replaced by the final reply + collapsed
// renderThinking()/renderToolTrace() once the turn completes, so this is only
// ever visible before the response lands.
function renderLiveReasoning(entries) {
  if (!entries.length) return "";
  const items = entries
    .map((e, i) => {
      const active = i === entries.length - 1;
      if (e.kind === "tool") {
        return `<div class="reasoning-step${active ? " active" : " done"}">` +
          `${active ? "" : "✓ "}${escapeHtml(e.label)}</div>`;
      }
      return `<div class="reasoning-thought${active ? " active" : " done"}">${escapeHtml(e.text)}</div>`;
    })
    .join("");
  return `<div class="thinking-live"><div class="thinking-live-title">🧠 Reasoning</div>${items}</div>`;
}

function refreshAttachments() {
  attachmentsEl.innerHTML = "";
  pendingFiles.forEach((file, i) => {
    const chip = document.createElement("div");
    chip.className = "chip";
    chip.innerHTML = `📄 ${escapeHtml(file.name)} <button data-i="${i}">×</button>`;
    chip.querySelector("button").onclick = () => {
      pendingFiles.splice(i, 1);
      refreshAttachments();
    };
    attachmentsEl.appendChild(chip);
  });
}

// ---- Conversation history ----------------------------------------------
// The active conversation is `sessionId`. The sidebar lists every past
// conversation for this client and lets you switch to or delete them.

function showGreeting() {
  messagesEl.innerHTML = "";
  addMessage("assistant", GREETING);
}

// Re-render an assistant turn from a stored transcript entry.
function renderAssistantTurn(turn) {
  const wrap = addMessage("assistant", "");
  const bubble = wrap.querySelector(".bubble");
  bubble.innerHTML =
    renderMarkdown(turn.text) + renderThinking(turn.thinking) +
    renderToolTrace(turn.tool_calls) + renderUsage(turn.usage, turn.cost_usd, turn.model_used);
  highlightCode(bubble);
  hydrateMedia(bubble);
}

function renderTranscript(transcript) {
  messagesEl.innerHTML = "";
  if (!transcript || !transcript.length) {
    showGreeting();
    return;
  }
  transcript.forEach((turn) => {
    if (turn.role === "user") {
      addMessage("user", renderUserText(turn.text));
    } else {
      renderAssistantTurn(turn);
    }
  });
  // Open at the bottom (newest message), like ChatGPT. From here, the user's
  // own scrolling controls whether we keep following.
  scrollToBottom();
}

// Wrap occurrences of the query term in <mark> (input already escaped).
function highlightTerm(escapedText, query) {
  const q = (query || "").trim();
  if (!q) return escapedText;
  const safe = q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return escapedText.replace(new RegExp(safe, "gi"), (m) => `<mark>${m}</mark>`);
}

async function loadConversations() {
  let convos = [];
  try {
    const params = new URLSearchParams({ entity_id: HermesBiz.current });
    if (convoQuery) params.set("q", convoQuery);
    const res = await fetch(`/api/conversations?${params.toString()}`);
    const data = await res.json();
    convos = data.conversations || [];
  } catch (_) { /* offline / first run — just show an empty list */ }

  convosEl.innerHTML = "";
  convosEmptyEl.style.display = convos.length ? "none" : "block";
  convosEmptyEl.textContent = convoQuery
    ? "No chats match your search."
    : "No conversations yet.";

  convos.forEach((c) => {
    const li = document.createElement("li");
    li.className = "convo" + (c.session_id === sessionId ? " active" : "");
    li.dataset.id = c.session_id;

    const openBtn = document.createElement("button");
    openBtn.className = "convo-open";
    openBtn.title = c.title;
    const titleHtml = highlightTerm(escapeHtml(c.title), convoQuery);
    const snippetHtml = c.snippet
      ? `<span class="convo-snippet">${highlightTerm(escapeHtml(c.snippet), convoQuery)}</span>`
      : "";
    openBtn.innerHTML = `<span class="convo-title">${titleHtml}</span>${snippetHtml}`;
    openBtn.onclick = () => openConversation(c.session_id);

    const delBtn = document.createElement("button");
    delBtn.className = "convo-del";
    delBtn.title = "Delete conversation";
    delBtn.innerHTML =
      '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>';
    delBtn.onclick = (e) => {
      e.stopPropagation();
      deleteConversation(c.session_id);
    };

    li.appendChild(openBtn);
    li.appendChild(delBtn);
    convosEl.appendChild(li);
  });
}
// Let voice.js refresh the list after a spoken turn.
window.hermesReloadConversations = loadConversations;
window.updateLockNote = updateLockNote;

// ---- History search ----------------------------------------------------
// Debounce so we query on a pause, not on every keystroke.
let convoSearchTimer = null;
if (convoSearchInput) {
  convoSearchInput.addEventListener("input", () => {
    clearTimeout(convoSearchTimer);
    convoSearchTimer = setTimeout(() => {
      convoQuery = convoSearchInput.value.trim();
      loadConversations();
    }, 220);
  });
  // Esc clears the search and restores the full list.
  convoSearchInput.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      convoSearchInput.value = "";
      convoQuery = "";
      loadConversations();
    }
  });
}

async function openConversation(id) {
  if (!id) return;
  sessionId = id;
  localStorage.setItem("hermes_session", sessionId);
  closeMobileNav();
  try {
    const res = await fetch(`/api/conversations/${encodeURIComponent(id)}?entity_id=${encodeURIComponent(HermesBiz.current)}`);
    if (res.ok) {
      const data = await res.json();
      renderTranscript(data.transcript);
      // Reseed the lock from the server's authoritative value (never a bare
      // null — "all" means "no restriction", so it stays open forever).
      HermesBiz.setLock(sessionId, data.entity_id || "all");
      updateLockNote();
    } else {
      showGreeting();
    }
  } catch (_) {
    showGreeting();
  }
  await loadConversations();
}

function startNewConversation() {
  sessionId = crypto.randomUUID();
  localStorage.setItem("hermes_session", sessionId);
  showGreeting();
  closeMobileNav();
  loadConversations();
  updateLockNote();
}

async function deleteConversation(id) {
  try {
    await fetch(`/api/conversations/${encodeURIComponent(id)}`, { method: "DELETE" });
  } catch (_) { /* ignore */ }
  if (id === sessionId) {
    // Deleted the open conversation — fall back to a fresh one.
    sessionId = crypto.randomUUID();
    localStorage.setItem("hermes_session", sessionId);
    showGreeting();
  }
  await loadConversations();
}

fileInput.addEventListener("change", () => {
  pendingFiles.push(...fileInput.files);
  fileInput.value = "";
  refreshAttachments();
});

input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 200) + "px";
});

input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
});

// Reads /api/chat/stream's Server-Sent Events and renders the reply as it's
// generated, instead of showing nothing until the whole turn (every tool
// round-trip included) finishes. Falls back to a plain error bubble on a
// failed request or a mid-stream "error" event — voice mode is untouched and
// keeps using plain /api/chat, since it needs the complete text before TTS.
async function sendStreaming(body, bubble) {
  const res = await fetch("/api/chat/stream", { method: "POST", body });
  if (!res.ok || !res.body) {
    let msg = `Request failed (${res.status})`;
    try { const j = await res.json(); if (j.error) msg = j.error; } catch (_) {}
    bubble.innerHTML = `<span class="tool-err">${escapeHtml(msg)}</span>`;
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let text = "";
  let final = null;
  let sawError = null;
  // Every reasoning step this turn, in order — real streamed thinking text
  // (when Claude chose to think out loud) interleaved with tool calls — so
  // the live view shows the whole process unfolding, not just the latest
  // step overwriting the last.
  const reasoningEntries = [];
  // The currently-open thought entry, so consecutive "thinking" deltas
  // append to one entry instead of each delta becoming its own line. Reset
  // to null on anything else (a tool call, or real reply text starting) so
  // the next thought (if any) starts a fresh entry.
  let openThought = null;

  const renderLive = () => {
    bubble.innerHTML = renderMarkdown(text) + renderLiveReasoning(reasoningEntries);
    if (stickToBottom) messagesEl.scrollTop = messagesEl.scrollHeight;
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      const chunk = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const dataLine = chunk.split("\n").find((l) => l.startsWith("data: "));
      if (!dataLine) continue;
      let evt;
      try { evt = JSON.parse(dataLine.slice(6)); } catch (_) { continue; }

      if (evt.type === "thinking") {
        if (!openThought) {
          openThought = { kind: "thought", text: "" };
          reasoningEntries.push(openThought);
        }
        openThought.text += evt.text;
        renderLive();
      } else if (evt.type === "text") {
        openThought = null;
        text += evt.text;
        renderLive();
      } else if (evt.type === "tool_call") {
        openThought = null;
        reasoningEntries.push({ kind: "tool", label: evt.label || `⚙️ Working on it (${evt.tool})…` });
        renderLive();
      } else if (evt.type === "done") {
        final = evt;
      } else if (evt.type === "error") {
        sawError = evt.error || "Agent error";
      }
    }
  }

  if (sawError && !final) {
    bubble.innerHTML = `<span class="tool-err">${escapeHtml(sawError)}</span>`;
    return;
  }

  if (final) {
    bubble.innerHTML =
      renderMarkdown(final.reply) + renderThinking(final.thinking) +
      renderToolTrace(final.tool_calls) + renderUsage(final.usage, final.cost_usd, final.model_used);
    highlightCode(bubble);
    hydrateMedia(bubble);
    // Lock (or reaffirm) this session's business from the server's
    // authoritative resolved value — never a bare null ("all" = open).
    HermesBiz.setLock(sessionId, final.entity_id || "all");
    updateLockNote();
    // A new turn was recorded — refresh the history list (title / new entry).
    loadConversations();
  } else {
    // Stream ended without a "done" event — show whatever text did arrive.
    bubble.innerHTML = renderMarkdown(text || "(no response)");
  }
}

// Reasoning-effort picker (low | medium | high). A per-browser preference sent
// with every message; the backend falls back to its default for anything else.
const effortSelect = document.getElementById("effort-select");
const EFFORT_KEY = "hermes_effort";
function currentEffort() {
  if (!effortSelect) return "";
  return effortSelect.value || "";
}
if (effortSelect) {
  const saved = localStorage.getItem(EFFORT_KEY);
  if (saved && [...effortSelect.options].some((o) => o.value === saved)) {
    effortSelect.value = saved;
  }
  effortSelect.addEventListener("change", () => {
    localStorage.setItem(EFFORT_KEY, effortSelect.value);
  });
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text && pendingFiles.length === 0) return;

  const fileNote = pendingFiles.length
    ? `<div class="hint">📎 ${pendingFiles.map((f) => escapeHtml(f.name)).join(", ")}</div>`
    : "";
  addMessage("user", renderUserText(text) + fileNote);
  scrollToBottom();  // sending always jumps to the newest message

  const body = new FormData();
  body.append("session_id", sessionId);
  body.append("entity_id", entityIdForSend());
  body.append("message", text);
  body.append("effort", currentEffort());
  pendingFiles.forEach((f) => body.append("files", f));

  input.value = "";
  input.style.height = "auto";
  pendingFiles = [];
  refreshAttachments();
  sendBtn.disabled = true;

  const thinkingEl = addMessage(
    "assistant",
    '<span class="typing"><span></span><span></span><span></span></span>'
  );

  try {
    await sendStreaming(body, thinkingEl.querySelector(".bubble"));
  } catch (err) {
    thinkingEl.querySelector(".bubble").innerHTML =
      `<span class="tool-err">Network error: ${escapeHtml(String(err))}</span>`;
  } finally {
    sendBtn.disabled = false;
    if (stickToBottom) messagesEl.scrollTop = messagesEl.scrollHeight;
  }
});

// "New conversation" starts a fresh chat WITHOUT deleting the current one —
// past chats stay in the history sidebar.
resetBtn.addEventListener("click", startNewConversation);

// ---- Pin the app to the visible viewport (normal mobile chat behavior) ----
// Anchors the layout to the area NOT covered by the keyboard, so the input
// stays just above the keyboard and the message list scrolls above it.
function syncViewport() {
  const vv = window.visualViewport;
  const appBox = document.getElementById("app");
  if (vv && appBox) {
    // Height of the on-screen keyboard (0 when it is hidden).
    const kb = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
    appBox.style.bottom = kb + "px";
  }
  // Only re-pin to the bottom if the user is already there (not reading history).
  if (stickToBottom) {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }
}
if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", syncViewport);
  window.visualViewport.addEventListener("scroll", syncViewport);
}
window.addEventListener("resize", syncViewport);
window.addEventListener("orientationchange", syncViewport);
// Some browsers report the final size slightly late — re-sync after focus changes.
input.addEventListener("focus", () => setTimeout(syncViewport, 150));
input.addEventListener("blur", () => setTimeout(syncViewport, 150));
syncViewport();

// Tap anywhere outside the message box to dismiss the keyboard.
document.addEventListener("pointerdown", (e) => {
  if (document.activeElement === input && !e.target.closest(".composer")) {
    input.blur();
  }
});

// ---- Navigation --------------------------------------------------------
const appEl   = document.getElementById("app");
const overlay = document.getElementById("overlay");
const resetTop = document.getElementById("reset-top");

// Mobile: open/close the leftnav drawer
const openMobileNav  = () => appEl.classList.add("mobile-open");
const closeMobileNav = () => appEl.classList.remove("mobile-open");

// Wire up every hamburger button (chat topbar + bills topbar)
["menu-btn", "menu-btn-bills", "menu-btn-meeting", "menu-btn-context", "menu-btn-tasks", "menu-btn-files", "menu-btn-jobs"].forEach((id) => {
  document.getElementById(id)?.addEventListener("click", openMobileNav);
});

// Overlay closes the mobile drawer
overlay?.addEventListener("click", closeMobileNav);

resetTop?.addEventListener("click", startNewConversation);

// ---- Section tabs (icon strip) ----
const stripTabs = document.querySelectorAll(".strip-tab");

function setView(view) {
  appEl.setAttribute("data-view", view);

  // Update active tab in icon strip
  stripTabs.forEach((t) => t.classList.toggle("active", t.dataset.view === view));

  // Show the matching panel-section, hide the rest
  document.querySelectorAll(".panel-section").forEach((s) => {
    s.classList.toggle("visible", s.id === `panel-${view}`);
  });

  // Close mobile drawer after switching
  closeMobileNav();

  // Lazy-init billing page on first open
  if (view === "bills" && typeof window._billsInit === "function") {
    window._billsInit();
  }
  // Lazy-init meeting copilot on first open
  if (view === "meeting" && typeof window._meetingInit === "function") {
    window._meetingInit();
  }
  // Lazy-init business context on first open
  if (view === "context" && typeof window._contextInit === "function") {
    window._contextInit();
  }
  // Lazy-init scheduled tasks on first open
  if (view === "tasks" && typeof window._tasksInit === "function") {
    window._tasksInit();
  }
  // Lazy-init file explorer on first open
  if (view === "files" && typeof window._filesInit === "function") {
    window._filesInit();
  }
  // Lazy-init jobs on first open
  if (view === "jobs" && typeof window._jobsInit === "function") {
    window._jobsInit();
  }
  // (Re)load the usage report every time the Usage tab is opened
  if (view === "pricing" && typeof window._pricingInit === "function") {
    window._pricingInit();
  }
}

stripTabs.forEach((t) => t.addEventListener("click", () => setView(t.dataset.view)));

// ---- Content panel collapse (desktop only) ----
document.getElementById("strip-toggle")?.addEventListener("click", () => {
  appEl.classList.toggle("panel-collapsed");
});

connectBtn.addEventListener("click", async () => {
  const app = connectApp.value.trim();
  if (!app) return;
  connectResult.textContent = "Requesting authorization link…";
  try {
    const res = await fetch("/api/connections", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ app }),
    });
    const data = await res.json();
    const url = data?.data?.url || data?.url;
    if (url) {
      connectResult.innerHTML =
        `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">Click to authorize ${escapeHtml(app)} →</a>`;
    } else {
      connectResult.textContent = data.error || JSON.stringify(data);
    }
  } catch (err) {
    connectResult.textContent = "Error: " + String(err);
  }
});

// ---- On load: start a blank conversation, just populate the history list ----
// We do NOT auto-open a past conversation — the page opens fresh every time.
// Past chats are reachable from the History sidebar.
(async function init() {
  showGreeting();
  await HermesBiz.load(); // populate the business list once, up front
  HermesBiz.bind(
    document.getElementById("hermes-biz-select-agent"),
    document.getElementById("hermes-biz-badge-agent")
  );
  // Switching the global business re-filters which past chats are shown, and
  // may reveal a mismatch with the open chat's actual lock.
  HermesBiz.onChange(() => { loadConversations(); updateLockNote(); });
  await loadConversations();
  updateLockNote();
})();
