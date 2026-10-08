// The two capability switches in Settings — what a turn KNOWS, and what it can DO.
//
//   Business Context  ->  POST /api/settings/context  (hermes/context_mode.py)
//       memory, context pack, retrieved documents, past-conversation recall.
//
//   Apps access       ->  POST /api/settings/apps     (hermes/apps_access.py)
//       the tool schemas: Gmail, Drive, Calendar, Zoom, Jira, WATI, QuickBooks,
//       web search, file read/create — 10.4–13.9k of the ~16k a greeting costs.
//       Off, Hermes keeps its knowledge but cannot act, and scheduled tasks are
//       skipped (recorded as "Apps access is off").
//
// What Business Context OFF does depends on a server flag, HERMES_SPLIT_TOGGLES
// (docs/architecture/TURN_COST_PLAN.md step 0.3), which GET /api/settings/context
// reports as `split_toggles`:
//   on  — the apps stay reachable; only the knowledge is dropped (the owner's
//         standing hard rules still ride along with the tools);
//   off — the default, and the old behaviour: chat also loses its tools and
//         answers as plain chat.
// The texts below describe whichever is live, so the control never promises a
// behaviour the server isn't running. Scheduled tasks ignore this switch.
//
// Not to be confused with static/context.js, which is the Business Context TAB
// (managing the knowledge files themselves).
//
// PLATFORM-WIDE, like the model picker: switching either one changes what every
// conversation sends on its next turn. Both checkboxes are pre-rendered by the
// server with their current state.
//
// Uses the same optimistic-rollback shape as model.js: flip immediately, put it
// back if the server refuses, so the control never lies about what is live.
(() => {
  const wire = ({ inputId, labelId, wrapId, statusId, endpoint, name, onText, offText }) => {
    const input = document.getElementById(inputId);
    const label = document.getElementById(labelId);
    const wrap = document.getElementById(wrapId);
    const status = document.getElementById(statusId);
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
        const res = await fetch(endpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || `Could not change ${name}.`);
        input.checked = !!data.enabled;
        paint(input.checked);
        // offText may be a function: its wording depends on what the server
        // reports is live (see the split flag above).
        showStatus(input.checked ? onText : (typeof offText === "function" ? offText() : offText), false);
      } catch (err) {
        input.checked = !enabled;   // roll back to what the server still has
        paint(input.checked);
        showStatus(err.message || `Could not change ${name}.`, true);
      } finally {
        input.disabled = false;
      }
    });
  };

  // Until the server says otherwise, assume the default (split OFF): it is the
  // conservative reading — it never promises apps that might not be there.
  let splitToggles = false;

  const contextOffText = () => (splitToggles
    ? "Business Context off — cheaper. Your apps still work; Hermes looks things up instead."
    : "Business Context off — cheaper plain chat: no memory or documents, and no apps until it's back on.");

  const contextHelp = () => (splitToggles
    ? "Off: Hermes stops carrying memory, the context pack and documents into each message. Cheaper; apps stay connected and your standing rules still apply. Scheduled tasks are unaffected."
    : "Off: chat becomes plain chat: no memory, context pack or documents, and no apps (email, Drive, Calendar…) until it's back on. Scheduled tasks are unaffected.");

  wire({
    inputId: "context-toggle-input",
    labelId: "context-toggle-label",
    wrapId: "context-toggle",
    statusId: "context-select-status",
    endpoint: "/api/settings/context",
    name: "Business Context",
    onText: "Business Context on — memory, context pack and documents are in every message.",
    offText: contextOffText,
  });

  wire({
    inputId: "apps-toggle-input",
    labelId: "apps-toggle-label",
    wrapId: "apps-toggle",
    statusId: "apps-select-status",
    endpoint: "/api/settings/apps",
    name: "Apps access",
    onText: "Apps access on — email, Drive, Calendar, web search and files are available.",
    offText: "Apps access off — Hermes can't reach any connected app or the web, and scheduled tasks and AI meeting notes are skipped.",
  });

  // The server-rendered hover help is static; replace it with the wording for
  // whatever is live. Best effort: if the request fails the defaults stand.
  const contextRow = document.getElementById("context-toggle")?.closest(".ab-t");
  const appsRow = document.getElementById("apps-toggle")?.closest(".ab-t");
  if (appsRow) {
    appsRow.title = "Off: Hermes can't email, reach Drive/Calendar/Jira/WhatsApp, search the web or read files, and scheduled tasks and AI meeting notes are skipped. Biggest saving.";
  }
  if (contextRow) {
    contextRow.title = contextHelp();
    fetch("/api/settings/context")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (!d) return;
        splitToggles = !!d.split_toggles;
        contextRow.title = contextHelp();
      })
      .catch(() => {});
  }
})();
