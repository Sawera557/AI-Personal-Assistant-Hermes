// Usage & Cost — per-chat-session token usage and what it cost us.
//
// Fetches /api/usage (scoped to the active business, like the history list),
// renders grand totals ("credits" = total tokens, plus USD) and a per-session
// table. Re-fetches every time the tab is opened. Mirrors jobs.js's lazy-init.
(function () {
  "use strict";

  let inited = false;
  // Active date window: {start, end} as ISO strings (or null = open bound).
  let range = { start: null, end: null };

  const $ = (id) => document.getElementById(id);
  const esc = (s) =>
    (typeof escapeHtml === "function"
      ? escapeHtml(String(s == null ? "" : s))
      : String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
          ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])));

  const nfmt = (n) => Number(n || 0).toLocaleString();
  const money = (n) => `$${Number(n || 0).toFixed(4)}`;

  function fmtWhen(iso) {
    if (!iso) return "—";
    try {
      return new Date(iso).toLocaleString([], {
        month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
      });
    } catch (_) { return iso; }
  }

  // The Usage tab is a global overview — total credits and cost across every
  // chat, regardless of which business is currently selected. So we always
  // request "all" rather than the active business (which would hide chats
  // scoped to other businesses, or to "All Businesses").

  // ── Date-range presets ────────────────────────────────────────────────────
  // Ranges are computed in the browser's LOCAL time (so "Today" means the
  // user's today), then sent as absolute ISO instants; Postgres filters the
  // timestamptz column by absolute time, which is correct across timezones.
  const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const iso = (d) => (d ? d.toISOString() : null);

  // preset -> {start: Date|null, end: Date|null}. `end` is exclusive.
  function presetRange(preset) {
    const now = new Date();
    const today = startOfDay(now);
    const minus = (days) => { const d = new Date(today); d.setDate(d.getDate() - days); return d; };
    switch (preset) {
      case "today":     return { start: today, end: null };
      case "yesterday": return { start: minus(1), end: today };
      case "week":      return { start: minus(7), end: null };
      case "month":     return { start: minus(30), end: null };
      case "sixmonths": { const d = new Date(today); d.setMonth(d.getMonth() - 6); return { start: d, end: null }; }
      default:          return { start: null, end: null }; // "all"
    }
  }

  // A short human label for the active window, shown above the table.
  function rangeNote() {
    if (!range.start && !range.end) return "Showing all recorded usage.";
    const fmt = (s) => new Date(s).toLocaleDateString([], { year: "numeric", month: "short", day: "numeric" });
    if (range.start && range.end) {
      // `end` is exclusive midnight — show the inclusive last day.
      const last = new Date(new Date(range.end).getTime() - 1);
      return `Showing ${fmt(range.start)} – ${last.toLocaleDateString([], { year: "numeric", month: "short", day: "numeric" })}.`;
    }
    if (range.start) return `Showing since ${fmt(range.start)}.`;
    return `Showing up to ${fmt(range.end)}.`;
  }

  function renderTotals(t) {
    const el = $("pricing-totals");
    if (!el) return;
    el.innerHTML =
      `<div class="usage-stat">
         <div class="usage-stat-label">Total credits (tokens)</div>
         <div class="usage-stat-value">${nfmt(t.tokens)}</div>
       </div>
       <div class="usage-stat">
         <div class="usage-stat-label">Total cost</div>
         <div class="usage-stat-value">${money(t.cost_usd)}</div>
       </div>
       <div class="usage-stat">
         <div class="usage-stat-label">Input / Output tokens</div>
         <div class="usage-stat-value" style="font-size:18px">${nfmt(t.input_tokens)} / ${nfmt(t.output_tokens)}</div>
       </div>`;
  }

  function renderSessions(sessions) {
    const el = $("pricing-body");
    if (!el) return;
    if (!sessions.length) {
      el.innerHTML = `<p class="hint" style="padding:8px">No usage recorded yet. Send a chat message and it'll show up here.</p>`;
      return;
    }
    const rows = sessions.map((s) => `
      <tr>
        <td>${esc(s.title)}<div class="usage-model">${esc(s.model || "")}</div></td>
        <td class="num">${nfmt(s.turns)}</td>
        <td class="num">${nfmt(s.tokens)}</td>
        <td class="num">${money(s.cost_usd)}</td>
        <td>${esc(fmtWhen(s.updated_at))}</td>
      </tr>`).join("");
    el.innerHTML = `
      <table class="usage-table">
        <thead>
          <tr>
            <th>Chat</th>
            <th class="num">Turns</th>
            <th class="num">Tokens</th>
            <th class="num">Cost</th>
            <th>Last used</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  async function load() {
    const totalsEl = $("pricing-totals");
    const bodyEl = $("pricing-body");
    const noteEl = $("pricing-range-note");
    if (noteEl) noteEl.textContent = rangeNote();
    if (totalsEl) totalsEl.innerHTML = "";
    if (bodyEl) bodyEl.innerHTML = `<p class="hint" style="padding:8px">Loading usage…</p>`;
    try {
      const params = new URLSearchParams({ entity_id: "all" });
      if (range.start) params.set("start", range.start);
      if (range.end) params.set("end", range.end);
      const r = await fetch(`/api/usage?${params.toString()}`);
      const j = await r.json();
      renderTotals(j.totals || {});
      renderSessions(j.sessions || []);
    } catch (e) {
      if (bodyEl) bodyEl.innerHTML = `<p class="hint" style="padding:8px">Couldn't load usage: ${esc(e)}</p>`;
    }
  }

  // Parse a <input type="date"> value ("YYYY-MM-DD") as a LOCAL midnight Date.
  function localDate(value) {
    if (!value) return null;
    const [y, m, d] = value.split("-").map(Number);
    if (!y || !m || !d) return null;
    return new Date(y, m - 1, d);
  }

  function applyCustom() {
    const errEl = $("pricing-custom-err");
    if (errEl) errEl.textContent = "";
    const from = localDate($("pricing-from")?.value);
    const to = localDate($("pricing-to")?.value);
    if (!from && !to) {
      if (errEl) errEl.textContent = "Pick a From and/or To date.";
      return;
    }
    if (from && to && to < from) {
      if (errEl) errEl.textContent = "“To” can't be before “From”.";
      return;
    }
    // `to` is inclusive of that whole day → exclusive end is the next midnight.
    const endExclusive = to ? new Date(to.getFullYear(), to.getMonth(), to.getDate() + 1) : null;
    range = { start: iso(from), end: iso(endExclusive) };
    load();
  }

  function selectPreset(preset) {
    document.querySelectorAll("#pricing-filter-row .usage-chip").forEach((c) =>
      c.classList.toggle("active", c.dataset.preset === preset));
    const customRow = $("pricing-custom-row");
    if (preset === "custom") {
      if (customRow) customRow.hidden = false;
      return; // wait for Apply
    }
    if (customRow) customRow.hidden = true;
    const { start, end } = presetRange(preset);
    range = { start: iso(start), end: iso(end) };
    load();
  }

  function bindOnce() {
    $("pricing-refresh-btn")?.addEventListener("click", load);
    $("pricing-refresh-main")?.addEventListener("click", load);
    $("pricing-filter-row")?.addEventListener("click", (e) => {
      const chip = e.target.closest(".usage-chip");
      if (chip) selectPreset(chip.dataset.preset);
    });
    $("pricing-apply")?.addEventListener("click", applyCustom);
  }

  window._pricingInit = function () {
    if (!inited) { bindOnce(); inited = true; }
    load();
  };
})();
