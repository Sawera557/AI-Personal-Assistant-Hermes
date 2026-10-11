/* Home HUD — fills the cockpit dials from the same endpoints the tabs use.
   Read-only: every figure here is fetched, never invented. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const CIRC = (r) => 2 * Math.PI * r;
  function arc(el, pct, r) {
    if (!el) return;
    const c = CIRC(r);
    el.setAttribute("stroke-dasharray", c.toFixed(1));
    el.setAttribute("stroke-dashoffset", (c * (1 - Math.max(0, Math.min(1, pct)))).toFixed(1));
  }

  const money = (n) => "$" + Number(n || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
  const nfmt = (n) => Number(n || 0).toLocaleString();

  /* ── Clock ─────────────────────────────────────────────────────────────── */
  function tick() {
    const now = new Date();
    const mon = now.toLocaleDateString([], { month: "long" });
    if ($("hud-month")) $("hud-month").textContent = mon;
    if ($("hud-daynum")) $("hud-daynum").textContent = String(now.getDate());
    if ($("hud-weekday")) $("hud-weekday").textContent = now.toLocaleDateString([], { weekday: "long" });
    if ($("hud-time")) $("hud-time").textContent = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
    if ($("hud-zone")) {
      const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "LOCAL";
      $("hud-zone").textContent = tz.replace("_", " ");
    }
    // Day-of-month progress on the outer dial
    const days = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate();
    arc($("hud-day-arc"), now.getDate() / days, 52);
  }

  /* ── Telemetry ─────────────────────────────────────────────────────────── */
  const get = (url) => fetch(url).then((r) => r.json()).catch(() => ({}));

  // The full document list is slow (seconds); its dial fills in on its own
  // instead of holding up the rest of the HUD.
  let docsLoading = null, docCount = null, memCount = null;
  function contextCount() {
    if ($("hud-c-context")) $("hud-c-context").textContent = docCount == null || memCount == null ? "…" : docCount + memCount;
  }
  function loadDocs() {
    if (docsLoading || docCount != null) return; // all-business count: once per page load
    docsLoading = get("/api/documents?entity_id=all").then((docs) => {
      docsLoading = null;
      if (!docs.documents) return;
      docCount = docs.documents.length;
      if ($("hud-docs-val")) $("hud-docs-val").textContent = docCount;
      arc($("hud-docs-arc"), Math.min(1, docCount / 12), 32);
      contextCount();
    });
  }

  async function loadAll() {
    loadDocs();
    const [bills, tasks, jobs, conns, usage, mems] = await Promise.all([
      get("/api/bills"), get("/api/scheduled-tasks"), get("/api/jobs"),
      get("/api/connections"), get("/api/usage?entity_id=all"),
      get("/api/memories?entity_id=all"),
    ]);

    const billList = bills.bills || [];
    const overdue = billList.filter((b) => b.status === "overdue");
    const dueSoon = billList.filter((b) => b.status === "due-soon");
    const taskList = tasks.tasks || [];
    const jobList = jobs.jobs || [];
    const memList = mems.memories || [];
    const totals = usage.totals || {};

    /* Knowledge base dials */
    if ($("hud-mem-val")) $("hud-mem-val").textContent = memList.length;
    arc($("hud-mem-arc"), Math.min(1, memList.length / 12), 32);

    /* Credit load — cost against a nominal monthly ceiling */
    const CAP = 50;
    if ($("hud-cost")) $("hud-cost").textContent = money(totals.cost_usd);
    if ($("hud-cost-cap")) $("hud-cost-cap").textContent = "of " + money(CAP) + " ceiling";
    if ($("hud-cost-bar")) $("hud-cost-bar").style.width = Math.min(100, ((totals.cost_usd || 0) / CAP) * 100) + "%";
    if ($("hud-tokens")) $("hud-tokens").innerHTML =
      `<div>tokens <b>${nfmt(totals.tokens)}</b></div>
       <div>in <b>${nfmt(totals.input_tokens)}</b></div>
       <div>out <b>${nfmt(totals.output_tokens)}</b></div>`;

    /* Connections — one row per app, with its account count (5 Gmail accounts → "Gmail ×5").
       LED: green = every account active, amber = some active or pending, grey = none. */
    const conList = (conns.data && conns.data.connections) || [];
    const apps = new Map();
    conList.forEach((c) => {
      const key = c.app || "";
      const g = apps.get(key) || apps.set(key, { total: 0, active: 0, pending: 0 }).get(key);
      const st = (c.status || "").toUpperCase();
      g.total++; if (st === "ACTIVE") g.active++; else if (st === "PENDING") g.pending++;
    });
    if ($("hud-conns")) {
      $("hud-conns").innerHTML = apps.size
        ? [...apps].map(([app, g]) => {
            const cls = g.active === g.total ? "" : g.active || g.pending ? " warn" : " off";
            const tip = `${g.total} account${g.total > 1 ? "s" : ""} · ${g.active} active`;
            return `<div class="hud-conn" title="${tip}"><i class="hud-led${cls}"></i><span>${esc(app.replace(/_/g, " "))}</span>${g.total > 1 ? `<b class="hud-conn-n">×${g.total}</b>` : ""}</div>`;
          }).join("")
        : `<div class="hud-empty">no apps connected</div>`;
    }

    /* Scheduled — next runs across tasks and jobs */
    const runs = [
      ...taskList.map((t) => ({ name: t.name, when: t.next_run_at, on: t.enabled, sub: t.schedule_summary, status: t.last_status })),
      ...jobList.map((j) => ({ name: j.name, when: j.next_run_at, on: j.enabled, sub: j.schedule_summary, status: j.last_status })),
    ].filter((r) => r.on).sort((a, b) => new Date(a.when) - new Date(b.when)).slice(0, 4);
    if ($("hud-sched")) {
      $("hud-sched").innerHTML = runs.length
        ? runs.map((r) => `
            <div class="hud-row hud-row-link" data-goto="tasks" title="${esc(r.name + (r.sub ? " · " + r.sub : ""))}">
              <i class="hud-led${r.status === "error" ? " err" : ""}"></i>
              <div class="hud-row-main">
                <div class="hud-row-name">${esc(r.name)}</div>
                <div class="hud-row-sub">${esc(r.sub || "")}</div>
              </div>
              <div class="hud-row-val ok">${esc(countdown(r.when))}</div>
            </div>`).join("")
        : `<div class="hud-empty">nothing scheduled</div>`;
      $("hud-sched").querySelectorAll("[data-goto]").forEach((row) => row.addEventListener("click", () => window.setView && window.setView(row.dataset.goto)));
    }

    /* Alerts — what Hermes actually processed: health problems, then the latest task runs */
    renderAlerts(taskList);

    /* Spoke counters */
    const set = (id, v) => { if ($(id)) $(id).textContent = v; };
    set("hud-c-bills", billList.length);
    set("hud-c-tasks", taskList.length);
    set("hud-c-jobs", jobList.length);
    memCount = memList.length;
    contextCount();

    /* Ticker */
    if ($("hud-ticker")) {
      const model = (document.getElementById("model-select") || {}).value || "—";
      $("hud-ticker").innerHTML =
        `<div>model <b>${esc(model)}</b></div><i class="sep"></i>` +
        `<div>apps online <b>${conList.filter((c) => (c.status || "").toUpperCase() === "ACTIVE").length}/${conList.length}</b></div><i class="sep"></i>` +
        `<div>overdue <b>${overdue.length}</b></div><i class="sep"></i>` +
        `<div>active schedules <b>${runs.length}</b></div><i class="sep"></i>` +
        `<div>tasks armed <b>${taskList.filter((t) => t.enabled).length}</b></div>`;
    }
  }


  const ago = (iso) => {
    const ms = Date.now() - new Date(iso);
    if (!iso || isNaN(ms)) return "—";
    const m = Math.round(ms / 60000), h = Math.round(ms / 3600000), d = Math.round(ms / 86400000);
    return m < 60 ? m + "m ago" : h < 48 ? h + "h ago" : d + "d ago";
  };
  function gist(text) {
    if (!text) return "";
    const lines = String(text).replace(/\*\*|__|`/g, "").split("\n").map((l) => l.replace(/^[#>\s]+/, "").replace(/\[([^\]]+)\]\([^)]*\)/g, "$1").trim()).filter((l) => l && !/^(the user|i'll|i will|let me|okay|sure|here's|here is)\b/i.test(l));
    const item = lines.find((l) => /^(\d+\.|[-•])\s/.test(l));
    const pick = (item || lines.find((l) => l.length > 24) || lines[0] || "").replace(/^(\d+\.|[-•])\s*/, "");
    return pick.length > 110 ? pick.slice(0, 108).trimEnd() + "…" : pick;
  }
  async function renderAlerts(taskList) {
    const box = $("hud-alerts");
    if (!box) return;
    const health = await get("/api/health");
    const items = [...(health.problems || []), ...(health.degraded || [])].slice(0, 2).map((p) => {
      const [head, ...rest] = String(p).split(":");
      return { led: " err", name: head, sub: rest.join(":").trim() || "System check", val: "check" };
    });
    const ran = taskList.filter((t) => t.last_run_at).sort((a, b) => new Date(b.last_run_at) - new Date(a.last_run_at)).slice(0, 4 - items.length);
    const runs = await Promise.all(ran.map((t) => get("/api/scheduled-tasks/" + encodeURIComponent(t.task_id) + "/runs")));
    ran.forEach((t, i) => {
      const r = ((runs[i] && runs[i].runs) || []).slice().sort((a, b) => new Date(b.started_at) - new Date(a.started_at))[0] || {};
      const st = r.status || t.last_status;
      items.push({ led: st === "error" ? " err" : st === "running" ? " warn" : "", name: t.name, sub: gist(r.error || r.reply) || (st === "error" ? "Run failed" : "Run finished"), val: ago(r.started_at || t.last_run_at), go: "tasks" });
    });
    box.innerHTML = items.length
      ? items.map((a) => '<div class="hud-row' + (a.go ? ' hud-row-link" data-goto="' + a.go : "") + '" title="' + esc(a.name + " · " + a.sub) + '">' +
          '<i class="hud-led' + a.led + '"></i><div class="hud-row-main"><div class="hud-row-name">' + esc(a.name) + '</div><div class="hud-row-sub">' + esc(a.sub) + "</div></div>" +
          '<div class="hud-row-val">' + esc(a.val) + "</div></div>").join("")
      : '<div class="hud-empty">Hermes hasn\'t processed any runs yet</div>';
    box.querySelectorAll("[data-goto]").forEach((row) => row.addEventListener("click", () => window.setView && window.setView(row.dataset.goto)));
  }

  function countdown(iso) {
    if (!iso) return "—";
    const ms = new Date(iso) - Date.now();
    if (isNaN(ms)) return "—";
    if (ms < 0) return "due";
    const h = Math.floor(ms / 3600000), m = Math.floor((ms % 3600000) / 60000);
    if (h >= 24) return "T-" + Math.floor(h / 24) + "d";
    return "T-" + (h ? h + "h" : m + "m");
  }

  /* Core face follows the active business */
  function syncBiz() {
    const el = $("hud-core-biz");
    if (!el) return;
    const biz = window.HermesBiz && HermesBiz.find && HermesBiz.find(HermesBiz.current);
    el.textContent = biz && biz.name ? biz.name : "All businesses";
  }

  function bind() {
    document.querySelectorAll(".hud-spoke[data-goto], .hud-more[data-goto]").forEach((b) =>
      b.addEventListener("click", () => {
        if (typeof window.setView === "function") window.setView(b.dataset.goto);
        else document.querySelector(`.strip-tab[data-view="${b.dataset.goto}"]`)?.click();
      }));
    document.querySelector('.strip-tab[data-view="home"]')?.addEventListener("click", () => { loadAll(); syncBiz(); });
    if (window.HermesBiz && HermesBiz.onChange) HermesBiz.onChange(() => { syncBiz(); loadAll(); });
  }

  function init() {
    tick();
    setInterval(tick, 1000);
    bind();
    syncBiz();
    loadAll();
    setInterval(loadAll, 60000);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
