// Settings — platform controls, one option per left-nav item.
//
// Today the only option is Guardrails: safety gates rendered from
// GET /api/settings/guardrails (data-driven, so a new backend guardrail appears
// here automatically) with an on/off .tk-toggle each. Toggling POSTs to the same
// endpoint with the model.js optimistic-rollback pattern. Mirrors jobs.js's
// lazy-init (window._settingsInit) + fetch-on-open shape.
(function () {
  "use strict";

  let inited = false;

  const $ = (id) => document.getElementById(id);
  const esc = (s) =>
    (typeof escapeHtml === "function"
      ? escapeHtml(String(s == null ? "" : s))
      : String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
          ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])));

  let statusTimer = null;
  function showStatus(text, isError) {
    const el = $("guardrails-status");
    if (!el) return;
    el.textContent = text;
    el.hidden = false;
    el.classList.toggle("error", !!isError);
    if (statusTimer) clearTimeout(statusTimer);
    statusTimer = setTimeout(() => { el.hidden = true; }, 2600);
  }

  // Left-nav: switch which settings option is shown. Only "guardrails" today,
  // but wired generically so adding an option is just markup.
  function bindNav() {
    document.querySelectorAll(".settings-nav-item").forEach((btn) => {
      btn.addEventListener("click", () => {
        const key = btn.dataset.settings;
        document.querySelectorAll(".settings-nav-item").forEach((b) =>
          b.classList.toggle("active", b === btn));
        document.querySelectorAll(".settings-section").forEach((s) =>
          s.classList.toggle("visible", s.id === `settings-${key}`));
      });
    });
  }

  async function postGuardrail(key, enabled) {
    const res = await fetch("/api/settings/guardrails", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ guardrail: key, enabled }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Could not update the guardrail.");
    return data;
  }

  function rowHtml(g) {
    return `
      <div class="guardrail-row">
        <div class="guardrail-info">
          <div class="guardrail-label">${esc(g.label)}</div>
          <div class="guardrail-desc">${esc(g.description)}</div>
        </div>
        <label class="tk-toggle" title="${g.enabled ? "On" : "Off"}">
          <input type="checkbox" data-key="${esc(g.key)}" ${g.enabled ? "checked" : ""}>
          <span class="tk-toggle-track"></span>
        </label>
      </div>`;
  }

  function bindToggles() {
    document.querySelectorAll('#guardrails-list input[type="checkbox"][data-key]').forEach((input) => {
      input.addEventListener("change", async () => {
        const key = input.dataset.key;
        const desired = input.checked;
        input.disabled = true;
        try {
          await postGuardrail(key, desired);
          showStatus(desired ? "Guardrail turned on." : "Guardrail turned off.", false);
        } catch (err) {
          input.checked = !desired; // rollback on failure
          showStatus(err.message || "Could not update the guardrail.", true);
        } finally {
          input.disabled = false;
        }
      });
    });
  }

  async function loadGuardrails() {
    const list = $("guardrails-list");
    if (!list) return;
    list.innerHTML = `<p class="hint">Loading…</p>`;
    try {
      const res = await fetch("/api/settings/guardrails");
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || "Failed to load guardrails.");
      const guardrails = data.guardrails || [];
      if (!guardrails.length) {
        list.innerHTML = `<p class="hint">No guardrails available.</p>`;
        return;
      }
      list.innerHTML = guardrails.map(rowHtml).join("");
      bindToggles();
    } catch (err) {
      list.innerHTML = `<p class="hint error">${esc(err.message || "Failed to load guardrails.")}</p>`;
    }
  }

  window._settingsInit = function () {
    if (!inited) { bindNav(); inited = true; }
    loadGuardrails();
  };
})();
