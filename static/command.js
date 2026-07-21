// Command Center — a real, source-labeled status board. Every metric either
// comes from a real endpoint or renders an explicit "SOURCE NOT CONNECTED"
// state; nothing here is ever a guessed number.
(() => {
  const scopeSubEl = document.getElementById("command-scope-sub");
  const entityChipsEl = document.getElementById("command-entity-chips");
  const metricsEl = document.getElementById("command-metrics");
  const appsGridEl = document.getElementById("command-apps-grid");
  const appsToggleBtn = document.getElementById("command-apps-toggle");

  async function fetchJson(url) {
    try {
      const r = await fetch(url);
      if (!r.ok) return null;
      return await r.json();
    } catch (_) {
      return null;
    }
  }

  // The Maton connections payload is nested (`{status, ok, data}`, where
  // `data` is whatever the upstream Maton API returned) — try the shapes
  // that API is plausibly using and fall back to "unknown" rather than guess.
  function extractConnections(payload) {
    if (!payload) return null;
    const data = payload.data;
    if (Array.isArray(data)) return data;
    if (data && Array.isArray(data.connections)) return data.connections;
    if (data && Array.isArray(data.data)) return data.data;
    if (Array.isArray(payload.connections)) return payload.connections;
    return null;
  }

  function renderEntityChips() {
    if (!entityChipsEl) return;
    entityChipsEl.innerHTML = HermesBiz.list.map((b) => {
      const active = b.slug === HermesBiz.current;
      const code = b.slug === "all" ? "ALL" : b.slug.toUpperCase();
      return (
        `<button type="button" class="command-chip${active ? " active" : ""}" data-slug="${b.slug}" style="--chip-color:${b.color || "#9aa3b2"}">` +
          `<span class="command-chip-dot"></span>${code}` +
        `</button>`
      );
    }).join("");
    entityChipsEl.querySelectorAll("[data-slug]").forEach((btn) => {
      btn.addEventListener("click", () => HermesBiz.set(btn.dataset.slug));
    });
    const biz = HermesBiz.find(HermesBiz.current);
    if (scopeSubEl) {
      scopeSubEl.textContent = `MULTI-ENTITY OPERATIONS · ${((biz && biz.name) || "All Businesses").toUpperCase()}`;
    }
  }

  function metricRow(label, source, value) {
    const known = value !== null && value !== undefined;
    const statusCls = known ? "ok" : "muted";
    const statusText = known ? String(value) : "SOURCE NOT CONNECTED";
    return (
      `<div class="command-metric-row">` +
        `<div class="command-metric-label">${label}<span class="command-metric-source">via ${source}</span></div>` +
        `<div class="command-metric-status command-metric-status-${statusCls}">${statusText}</div>` +
      `</div>`
    );
  }

  async function renderMetrics() {
    if (!metricsEl) return;
    metricsEl.innerHTML = '<div class="command-metric-loading">Loading…</div>';

    const [insights, connections, tasks, jobs, convos] = await Promise.all([
      fetchJson("/api/bills/insights"),
      fetchJson("/api/connections"),
      fetchJson("/api/scheduled-tasks"),
      fetchJson("/api/jobs"),
      fetchJson("/api/conversations?entity_id=all"),
    ]);

    const overdue = insights ? (insights.overdue || []).length : null;
    const dueSoon = insights ? (insights.upcoming || []).length : null;
    const connList = extractConnections(connections);
    const appsReady = connList
      ? connList.filter((c) => String(c.status || c.state || "").toUpperCase() === "ACTIVE").length
      : null;
    const taskCount = tasks ? (tasks.tasks || []).length : null;
    const jobCount = jobs ? (jobs.jobs || []).length : null;
    const convoCount = convos ? (convos.conversations || []).length : null;

    metricsEl.innerHTML = [
      metricRow("Overdue bills", "Bills", overdue),
      metricRow("Bills due this week", "Bills", dueSoon),
      metricRow("Connected apps ready", "Maton", appsReady),
      metricRow("Scheduled tasks", "Scheduler", taskCount),
      metricRow("Jobs configured", "Jobs", jobCount),
      metricRow("Conversations (all-time)", "History", convoCount),
    ].join("");
  }

  async function renderApps() {
    if (!appsGridEl) return;
    appsGridEl.innerHTML = '<div class="command-apps-empty">Loading…</div>';
    const connList = extractConnections(await fetchJson("/api/connections"));
    if (!connList || !connList.length) {
      appsGridEl.innerHTML = '<div class="command-apps-empty">No connections reported yet.</div>';
      return;
    }
    appsGridEl.innerHTML = connList.map((c) => {
      const status = String(c.status || c.state || "UNKNOWN").toUpperCase();
      const ok = status === "ACTIVE";
      const name = escapeHtml(String(c.app || c.app_slug || c.slug || c.name || "app"));
      return (
        `<div class="command-app-card">` +
          `<div class="command-app-name">${name}</div>` +
          `<div class="command-app-status command-app-status-${ok ? "ok" : "muted"}">${status}</div>` +
        `</div>`
      );
    }).join("");
  }

  let appsOpen = false;
  appsToggleBtn?.addEventListener("click", () => {
    appsOpen = !appsOpen;
    appsGridEl.hidden = !appsOpen;
    appsToggleBtn.textContent = appsOpen ? "HIDE" : "VIEW ALL";
    if (appsOpen) renderApps();
  });

  let subscribed = false;
  window._commandInit = function () {
    renderEntityChips();
    renderMetrics();
    if (!subscribed) {
      subscribed = true;
      HermesBiz.onChange(renderEntityChips);
    }
  };
})();
