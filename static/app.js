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

function renderToolTrace(tools) {
  if (!tools || !tools.length) return "";
  const items = tools.map((t) => {
    const cls = t.ok ? "tool-ok" : "tool-err";
    const dot = t.ok ? "✓" : "✗";
    return `<details><summary><span class="${cls}">${dot}</span> ${escapeHtml(t.tool)}</summary>` +
      `<pre>${escapeHtml(JSON.stringify(t.input, null, 2))}</pre></details>`;
  });
  return `<div class="tools">${items.join("")}</div>`;
}

function renderThinking(thinking) {
  if (!thinking) return "";
  return `<div class="thinking"><details><summary>Reasoning</summary>` +
    `<pre>${escapeHtml(thinking)}</pre></details></div>`;
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
    renderMarkdown(turn.text) + renderThinking(turn.thinking) + renderToolTrace(turn.tool_calls);
  highlightCode(bubble);
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
    const url = convoQuery
      ? `/api/conversations?q=${encodeURIComponent(convoQuery)}`
      : "/api/conversations";
    const res = await fetch(url);
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
  closeDrawer();
  try {
    const res = await fetch(`/api/conversations/${encodeURIComponent(id)}`);
    if (res.ok) {
      const data = await res.json();
      renderTranscript(data.transcript);
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
  closeDrawer();
  loadConversations();
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
  body.append("message", text);
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
    const res = await fetch("/api/chat", { method: "POST", body });
    const data = await res.json();
    if (data.error) {
      thinkingEl.querySelector(".bubble").innerHTML =
        `<span class="tool-err">${escapeHtml(data.error)}</span>`;
    } else {
      const bubble = thinkingEl.querySelector(".bubble");
      bubble.innerHTML =
        renderMarkdown(data.reply) + renderThinking(data.thinking) + renderToolTrace(data.tool_calls);
      highlightCode(bubble);
      // A new turn was recorded — refresh the history list (title / new entry).
      loadConversations();
    }
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

// ---- Mobile drawer ----
const appEl = document.getElementById("app");
const menuBtn = document.getElementById("menu-btn");
const overlay = document.getElementById("overlay");
const drawerClose = document.getElementById("drawer-close");
const resetTop = document.getElementById("reset-top");

const openDrawer = () => appEl.classList.add("drawer-open");
const closeDrawer = () => appEl.classList.remove("drawer-open");

menuBtn?.addEventListener("click", openDrawer);
overlay?.addEventListener("click", closeDrawer);
drawerClose?.addEventListener("click", closeDrawer);
resetTop?.addEventListener("click", startNewConversation);

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
  await loadConversations();
})();
