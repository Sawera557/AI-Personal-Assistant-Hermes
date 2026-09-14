// Business Context MODE switch — full context + tools, or plain chat.
//
// Not to be confused with static/context.js, which is the Business Context TAB
// (managing the knowledge files themselves). This only switches how much of
// that context each turn carries.
//
// PLATFORM-WIDE, like the model picker: switching it here changes what every
// conversation sends on its next turn, via POST /api/settings/context. The
// checkbox is pre-rendered by the server with the current state.
//
// Why it exists: a full-context turn ships long-term memory, the business
// context pack, retrieved knowledge and every tool schema — measured at ~20,300
// input tokens for "Hi", of which ~11,000 is the tool definitions alone. Off,
// the same greeting costs ~150.
//
// Uses the same optimistic-rollback shape as model.js: flip immediately, put it
// back if the server refuses, so the control never lies about what is live.
(() => {
  const input = document.getElementById("context-toggle-input");
  const label = document.getElementById("context-toggle-label");
  const wrap = document.getElementById("context-toggle");
  const status = document.getElementById("context-select-status");
  if (!input) return;

  const showStatus = (text, isError) => {
    if (!status) return;
    status.textContent = text;
    status.hidden = false;
    status.classList.toggle("error", !!isError);
    if (!isError) setTimeout(() => { status.hidden = true; }, 3500);
  };

  const paint = (enabled) => {
    if (label) label.textContent = enabled ? "On" : "Off";
    if (wrap) wrap.title = enabled ? "On" : "Off";
  };

  input.addEventListener("change", async () => {
    const enabled = input.checked;
    paint(enabled);
    input.disabled = true;
    try {
      const res = await fetch("/api/settings/context", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || "Could not change Business Context.");
      input.checked = !!data.enabled;
      paint(input.checked);
      showStatus(
        input.checked
          ? "Business Context on — memory, documents and tools are available."
          : "Business Context off — much cheaper, but no apps, memory or documents.",
        false,
      );
    } catch (err) {
      input.checked = !enabled;   // roll back to what the server still has
      paint(input.checked);
      showStatus(err.message || "Could not change Business Context.", true);
    } finally {
      input.disabled = false;
    }
  });
})();
