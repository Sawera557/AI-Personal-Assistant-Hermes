// Hermes chat client
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

// Stable session id so the agent remembers the conversation.
let sessionId = localStorage.getItem("hermes_session");
if (!sessionId) {
  sessionId = crypto.randomUUID();
  localStorage.setItem("hermes_session", sessionId);
}

let pendingFiles = [];

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

// Minimal, safe markdown-ish rendering for assistant text.
function renderText(text) {
  let html = escapeHtml(text);
  html = html.replace(/```([\s\S]*?)```/g, (_, code) => `<pre>${code}</pre>`);
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return html;
}

function addMessage(role, html) {
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  wrap.innerHTML = `<div class="bubble">${html}</div>`;
  messagesEl.appendChild(wrap);
  messagesEl.scrollTop = messagesEl.scrollHeight;
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
  addMessage("user", renderText(text) + fileNote);

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
      thinkingEl.querySelector(".bubble").innerHTML =
        renderText(data.reply) + renderThinking(data.thinking) + renderToolTrace(data.tool_calls);
    }
  } catch (err) {
    thinkingEl.querySelector(".bubble").innerHTML =
      `<span class="tool-err">Network error: ${escapeHtml(String(err))}</span>`;
  } finally {
    sendBtn.disabled = false;
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }
});

resetBtn.addEventListener("click", async () => {
  await fetch("/api/reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  });
  sessionId = crypto.randomUUID();
  localStorage.setItem("hermes_session", sessionId);
  messagesEl.innerHTML = "";
  addMessage("assistant", "Started a fresh conversation. What can I do for you?");
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
