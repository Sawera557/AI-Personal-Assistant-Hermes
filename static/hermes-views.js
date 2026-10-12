/* Hermes · Views — live architecture diagram + Billing overview.
   Reads the same /api endpoints as the tabs; acts through the app's own
   functions (setView, setBillingMode, openBillViewer, decideProposal…). */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const get = (u) => fetch(u).then((r) => r.json()).catch(() => ({}));
  const usd = (n, d = 0) => "$" + Number(n || 0).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
  const go = (v) => (typeof window.setView === "function" ? window.setView(v) : null);
  const ask = (t) => (window.HermesAsk ? window.HermesAsk(t) : null);
  const toast = (o) => window.HermesToast && window.HermesToast(o);
  const view = () => ($("app") || {}).dataset?.view;
  const DAY = 864e5;
  const daysTo = (d) => Math.round((new Date(d + "T12:00:00") - Date.now()) / DAY);

  /* ═════════════════════════ ARCHITECTURE ═════════════════════════════ */
  const P = 'fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"';
  const IC = {
    web: '<rect x="3" y="4.5" width="18" height="14" rx="1.5"/><path d="M3 8.5h18M6 6.5h.01M8.5 6.5h.01"/>',
    voice: '<path d="M12 3v12M8 7v6M16 7v6M4 10v2M20 10v2"/>',
    meet: '<rect x="3" y="6" width="13" height="12" rx="1"/><path d="M16 10l5-3v10l-5-3"/>',
    api: '<path d="M8 6l-5 6 5 6M16 6l5 6-5 6M13.5 4l-3 16"/>',
    core: '<rect x="6" y="6" width="12" height="12" rx="1"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>',
    guard: '<path d="M12 3l7 3v5c0 4.5-3 8.2-7 10-4-1.8-7-5.5-7-10V6z"/><path d="M9 12l2 2 4-4"/>',
    ctx: '<circle cx="6" cy="7" r="2"/><circle cx="18" cy="6" r="2"/><circle cx="12" cy="17" r="2"/><path d="M7.7 8.2l3.2 7M16.3 7l-3.4 8.4M8 7l8-.8"/>',
    pg: '<ellipse cx="12" cy="6" rx="7" ry="2.6"/><path d="M5 6v12c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6V6M5 12c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6"/>',
    s3: '<path d="M4 7h16l-1.6 12.2a2 2 0 0 1-2 1.8H7.6a2 2 0 0 1-2-1.8z"/><ellipse cx="12" cy="7" rx="8" ry="2.5"/>',
    pine: '<path d="M12 2l5 7h-3l4 6h-4l3 5H7l3-5H6l4-6H7z"/>',
    maton: '<rect x="3" y="9" width="18" height="6"/><path d="M7 9V5M12 9V3M17 9V5M7 15v4M12 15v6M17 15v4"/>',
    llm: '<circle cx="12" cy="12" r="3"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4M5 5l2.8 2.8M16.2 16.2L19 19M5 19l2.8-2.8M16.2 7.8L19 5"/>',
    tools: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.6 2.4 3.8 5.3 3.8 8.5s-1.2 6.1-3.8 8.5c-2.6-2.4-3.8-5.3-3.8-8.5s1.2-6.1 3.8-8.5z"/>',
    sched: '<circle cx="12" cy="13" r="7.5"/><path d="M12 9v4l2.5 1.5M9 2.5h6"/>',
    jobs: '<path d="M4 6h16M4 12h10M4 18h13"/><circle cx="19" cy="12" r="2"/>',
    ingest: '<path d="M12 3v12M7 10l5 5 5-5M4 19h16"/>',
    bills: '<path d="M6 3h12v18l-3-2-3 2-3-2-3 2z"/><path d="M9 8h6M9 12h6"/>',
    notify: '<path d="M6 16V11a6 6 0 0 1 12 0v5l2 2H4z"/><path d="M10 20a2 2 0 0 0 4 0"/>',
    agents: '<circle cx="12" cy="5" r="2.5"/><circle cx="5" cy="18" r="2.5"/><circle cx="19" cy="18" r="2.5"/><path d="M10.7 7.2L6.3 15.8M13.3 7.2l4.4 8.6M7.5 18h9"/>',
    model: '<path d="M4 7h11M4 17h7"/><circle cx="18" cy="7" r="2.5"/><circle cx="14" cy="17" r="2.5"/><path d="M20.5 7H21M16.5 17H21"/>',
    mem: '<path d="M9 4a3 3 0 0 0-3 3 3 3 0 0 0-2 5 3 3 0 0 0 2 5 3 3 0 0 0 6 1V5a3 3 0 0 0-3-1zM15 4a3 3 0 0 1 3 3 3 3 0 0 1 2 5 3 3 0 0 1-2 5 3 3 0 0 1-6 1"/>',
    skills: '<path d="M5 4h10l4 4v12H5z"/><path d="M15 4v4h4M8.5 13l2 2 4-4.5"/>',
  };
  const icon = (k) => '<span class="ax-ic"><svg viewBox="0 0 24 24" width="15" height="15" ' + P + '>' + IC[k] + '</svg></span>';
  const ENTITY = { afk: "afro_farm", gak: "gak_properties", gif: "gif_properties", gakc: "gak_commodities", nh: "nour_hammam", per: "personal" };
  // [from, to, tag]  tag: ctx = needs Business context · apps = needs Apps access · gate = passes guardrails · v = vertical route
  //                  · side = vertical route that bows out along the left edge, past the nodes stacked between
  const EDGES = [
    ["web", "api"], ["voice", "api"], ["meet", "api"],
    ["api", "core"], ["api", "pg"],
    ["agents", "core", "v"], ["core", "model", "v"], ["core", "guard", "gate side"],
    ["core", "mem", "ctx"], ["core", "skills", "ctx"], ["core", "ctx", "ctx"],
    ["mem", "pg", "ctx"], ["skills", "pg", "ctx"], ["ctx", "pg", "ctx"], ["ctx", "pine", "ctx"],
    ["core", "s3"], ["model", "llm"],
    ["guard", "maton", "apps gate"], ["guard", "tools", "apps gate"],
    ["sched", "core", "v"], ["jobs", "pg", "v"], ["ingest", "pine", "v"], ["bills", "s3", "v"], ["notify", "maton", "v"],
  ];
  const SX = { ctx: true, apps: true, d: {}, wires: {} };

  function sxState() {
    const c = $("context-toggle-input"), a = $("apps-toggle-input");
    SX.ctx = c ? c.checked : true; SX.apps = a ? a.checked : true;
  }
  // The full document list is slow (seconds): render without it, then re-render
  // the schematic once the count lands ("…" until then).
  let docsLoading = null;
  function sxDocs() {
    if (docsLoading || SX.docs) return; // all-business count: once per page load
    docsLoading = get("/api/documents?entity_id=all").then((r) => {
      docsLoading = null;
      if (!r.documents) return;
      SX.docs = r.documents;
      if (SX.d.loaded) { SX.d.docs = SX.docs; if (view() === "schema") sxRender(); }
    });
  }
  async function sxData() {
    sxDocs();
    const [mems, conns, usage, tasks, jobs, gr, biz, agents, models, skills] = await Promise.all([
      get("/api/memories?entity_id=all"), get("/api/connections"), get("/api/usage?entity_id=all"),
      get("/api/scheduled-tasks"), get("/api/jobs"), get("/api/settings/guardrails"), get("/api/businesses"),
      get("/api/agents"), get("/api/settings/model"), get("/api/documents?entity_id=all&source_system=business_skill"),
    ]);
    if (!SX.paths) get("/openapi.json").then((o) => { if (!o.paths) return; SX.paths = o.paths; if (view() === "schema") sxRender(); });
    // A failed or timed-out request is null, not [] — the node then shows its
    // descriptive text instead of a false "0" (e.g. Maton slow → "0 apps").
    const arr = (v) => (Array.isArray(v) ? v : null);
    SX.d = {
      loaded: true, docs: SX.docs || null, mems: arr(mems.memories), conns: arr(conns.data && conns.data.connections),
      usage: usage.totals || null, tasks: arr(tasks.tasks), jobs: arr(jobs.jobs), gr: arr(gr.guardrails), biz: arr(biz.businesses),
      agents: arr(agents.agents), models: arr(models.options), active: models.active || "", skills: arr(skills.documents),
    };
  }
  // "Claude · Opus 5 (most capable)" -> provider "Claude", name "Opus 5".
  const modelParts = (label) => {
    const [prov, rest = ""] = String(label || "").split(" · ");
    return { prov: rest ? prov : "", name: (rest || prov).replace(/\s*\(.*\)\s*$/, "") };
  };
  const optValue = (o) => (o && (o.value || o.id || o.model)) || "";
  function activeModel(d) {
    const o = (d.models || []).find((m) => optValue(m) === d.active);
    return o ? modelParts(o.label) : { prov: "", name: d.active || selText("model-select") };
  }
  const countBy = (arr, key) => arr.reduce((m, x) => { const k = key(x); if (k) m.set(k, (m.get(k) || 0) + 1); return m; }, new Map());
  const PROVIDER = { Claude: "Anthropic", OpenAI: "OpenAI", Google: "Google", NVIDIA: "NVIDIA NIM" };
  // Endpoint count per /api area, read from FastAPI's own OpenAPI schema.
  function routeRows() {
    const AREAS = ["/chat", "/conversations", "/agents", "/memories", "/documents", "/settings", "/scheduled-tasks", "/jobs",
      "/meetings", "/voice", "/bills", "/register", "/artefacts", "/generated-files", "/connections"];
    const ops = new Map();
    Object.entries(SX.paths || {}).forEach(([p, v]) => {
      const area = AREAS.find((a) => p === "/api" + a || p.startsWith("/api" + a + "/"));
      if (area) ops.set(area, (ops.get(area) || 0) + Object.keys(v).length);
    });
    return AREAS.map((a) => '<span class="ax-route"><code>/api' + a + "</code>" + (ops.get(a) ? "<i>" + ops.get(a) + "</i>" : "") + "</span>").join("");
  }
  const selText = (id) => { const s = $(id); return s && s.options[s.selectedIndex] ? s.options[s.selectedIndex].textContent : "—"; };
  const chip = (t, led) => '<span class="ax-chip">' + (led ? '<i class="ax-led ' + led + '"></i>' : "") + esc(t) + "</span>";

  function node(n) {
    return '<button type="button" class="ax-node' + (n.cls ? " " + n.cls : "") + (n.off ? " off" : "") + '" data-n="' + n.id + '"' + (n.goto ? ' data-goto="' + n.goto + '"' : "") + ">" +
      '<div class="ax-n-top">' + icon(n.id) + '<div class="ax-n-tt"><div class="ax-n-t">' + n.t + '</div><div class="ax-n-f">' + n.f + "</div></div>" +
      (n.off ? '<span class="ax-off">OFF</span>' : "") + "</div>" +
      (n.s ? '<div class="ax-n-s">' + n.s + "</div>" : "") + (n.x || "") +
      (n.chips ? '<div class="ax-chips">' + n.chips + "</div>" : "") + "</button>";
  }
  const col = (num, label, inner) => '<div class="ax-col"><div class="ax-lbl"><b>' + num + "</b>" + label + '</div><div class="ax-stack">' + inner + "</div></div>";

  // One entry per app, not per account: several Gmail / Calendar accounts → "google mail ×5".
  // Dot: on = every account active, warn = some active or pending, off = none.
  function appGroups(conns) {
    const by = new Map();
    conns.forEach((c) => {
      const key = c.app || c.name;
      const g = by.get(key) || by.set(key, { label: (c.name || c.app).replace(/[-_]/g, " "), total: 0, active: 0, pending: 0 }).get(key);
      const st = (c.status || "").toUpperCase();
      g.total++; if (st === "ACTIVE") g.active++; else if (st === "PENDING") g.pending++;
    });
    return [...by.values()];
  }

  function sxRender() {
    const el = $("schema-view");
    if (!el) return;
    const d = SX.d, ctxOff = !SX.ctx, appsOff = !SX.apps;
    const L = (a) => a || [];  // null = request failed; count from [] only where text is guarded
    const live = L(d.conns).filter((c) => (c.status || "").toUpperCase() === "ACTIVE").length;
    const armed = L(d.tasks).filter((t) => t.enabled).length;
    const ns = L(d.biz).map((b) => ENTITY[b.slug] ? "entity__" + ENTITY[b.slug] : null).filter(Boolean);
    const am = activeModel(d);
    const provs = countBy(L(d.models), (m) => modelParts(m.label).prov);
    const nEndpoints = SX.paths ? Object.values(SX.paths).reduce((s, v) => s + Object.keys(v).length, 0) : null;
    const liveAgents = L(d.agents).filter((a) => a.enabled !== false);
    const memScopes = countBy(L(d.mems), (m) => m.scope_type);
    const kbDocs = d.docs ? d.docs.filter((x) => x.source_system !== "business_skill").length : null;
    const N = {
      web: node({ id: "web", t: "Web app", f: "templates/index.html · static/*.js", s: "Jinja page and view scripts, served by the API.", chips: d.biz ? chip(d.biz.length + " businesses") : "", goto: "agent" }),
      voice: node({ id: "voice", t: "Voice", f: "voice.py · gemini_live.py · /api/voice", s: "Speech-to-text in, spoken replies out, live voice sessions." }),
      meet: node({ id: "meet", t: "Meeting Copilot", f: "meeting_copilot.py · fathom.py", s: "Live guidance and notes during calls.", goto: "meeting" }),
      api: node({ id: "api", cls: "tall", t: "FastAPI", f: "app.py · Swagger at /docs", s: "One API for every tab, worker and integration." + (nEndpoints ? " <b>" + nEndpoints + "</b> endpoints." : ""),
        x: '<div class="ax-routes">' + routeRows() + "</div>" }),
      agents: node({ id: "agents", t: "Multi-agents", f: "agents.py · agent_tools.py · /api/agents", goto: "multiagents",
        s: d.agents ? "<b>" + liveAgents.length + "</b> of " + d.agents.length + " agents on · each has its own role prompt, tool whitelist and autonomy."
          : "Specialist agents, each with its own role prompt, tool whitelist and autonomy.",
        chips: L(d.agents).slice(0, 6).map((a) => chip(a.name, a.enabled === false ? "off" : "on")).join("") + (L(d.agents).length > 6 ? chip("+" + (d.agents.length - 6)) : "") }),
      core: node({ id: "core", cls: "core", t: "Hermes agent", f: "agent.py · tools.py · turn_router.py", goto: "agent",
        s: "Tool-use loop · routes each turn to <b>CHAT</b> or <b>FULL</b>",
        x: '<div class="ax-stats"><div>Reasoning<b id="sx-effort">' + esc(selText("effort-select").split(" — ")[0]) + "</b></div><div>Tokens<b>" + (d.usage ? Number(d.usage.tokens || 0).toLocaleString() : "—") + "</b></div><div>Spend<b>" + (d.usage ? usd(d.usage.cost_usd, 2) : "—") + "</b></div></div>" }),
      model: node({ id: "model", t: "Model switch", f: "models.py · /api/settings/model", goto: "agent",
        s: 'Active: <b id="sx-model">' + esc(am.name) + "</b>" + (am.prov ? " · " + esc(am.prov) : "") + "<br>" + (d.models ? d.models.length + " models · " : "") + "change it from the model picker, next turn uses it",
        chips: [...provs].map(([p, n]) => chip(p + " ×" + n, p === am.prov ? "on" : "")).join("") }),
      guard: node({ id: "guard", t: "Guardrails", f: "guardrails.py", goto: "settings",
        s: "Checked on every tool call, before it runs.",
        chips: L(d.gr).length ? d.gr.map((g) => chip(g.key, g.enabled ? "on" : "off")).join("") : chip("confirm_writes") + chip("business_isolation") }),
      mem: node({ id: "mem", t: "Memory", f: "memory_retrieve.py · conversation_retrieve.py", goto: "context", off: ctxOff,
        s: (d.mems ? "<b>" + d.mems.length + "</b> memories · s" : "S") + "tanding rules always load, the rest by relevance to your message.",
        chips: [...memScopes].map(([s, n]) => chip(s + " " + n)).join("") }),
      skills: node({ id: "skills", t: "Skills", f: "Context › Skills · rag.py", goto: "context", off: ctxOff,
        s: d.skills ? "<b>" + d.skills.length + "</b> playbooks · pulled into the turn when your request matches one."
          : "Playbooks pulled into the turn when your request matches one.",
        chips: L(d.skills).slice(0, 4).map((k) => chip(k.title || "untitled")).join("") + (L(d.skills).length > 4 ? chip("+" + (d.skills.length - 4)) : "") }),
      ctx: node({ id: "ctx", t: "Knowledge retrieval", f: "rag.py · context_packs.py", goto: "context", off: ctxOff,
        s: "<b>" + (kbDocs == null ? "…" : kbDocs) + "</b> documents · pgvector + Pinecone, ranked per message." }),
      pg: node({ id: "pg", t: "Postgres 17", f: "AWS RDS · hermes/db", s: "Chats, memories, documents, tasks, bills, usage.", chips: chip("pgvector") + chip("24 tables") }),
      s3: node({ id: "s3", t: "S3", f: "AWS S3 · storage.py", goto: "files", s: "Original files and everything Hermes generates.", chips: ["bills/", "generated/", "drive-files/", "wati-media/"].map((p) => chip(p)).join("") }),
      pine: node({ id: "pine", t: "Pinecone", f: "pinecone_store.py · serverless", off: ctxOff, s: "One search namespace per business.", chips: (ns.length ? ns : ["entity__<slug>"]).map((x) => chip(x)).join("") }),
      maton: node({ id: "maton", t: "Maton gateway", f: "maton.py · OAuth connectors", off: appsOff,
        s: d.conns ? "<b>" + live + "</b> of " + d.conns.length + " accounts connected · <b>" + appGroups(d.conns).length + "</b> apps"
          : "One OAuth gateway for every business app. Couldn't reach Maton just now; reopen System to retry.",
        chips: appGroups(L(d.conns)).map((g) => chip(g.label + (g.total > 1 ? " ×" + g.total : ""), g.active === g.total ? "on" : g.active || g.pending ? "warn" : "off")).join("") }),
      llm: node({ id: "llm", t: "Model providers", f: "Anthropic · OpenAI · Gemini · NIM APIs", s: d.models ? "Whichever model the switch picks answers the turn." : "Switchable per conversation.",
        chips: d.models ? [...provs.keys()].map((p) => chip(PROVIDER[p] || p, p === am.prov ? "on" : "")).join("") : chip("Anthropic") + chip("OpenAI") + chip("NVIDIA NIM") }),
      tools: node({ id: "tools", t: "Direct integrations", f: "wati.py · worldbank.py · extract.py", off: appsOff, chips: chip("WhatsApp · WATI") + chip("World Bank") + chip("Web search") + chip("NuExtract OCR") }),
      sched: node({ id: "sched", t: "Scheduler", f: "scheduler.py", goto: "tasks", s: d.tasks ? "<b>" + armed + "</b> of " + d.tasks.length + " tasks armed" : "Runs scheduled tasks on their cadence." }),
      jobs: node({ id: "jobs", t: "Job runner", f: "job_runner.py", goto: "jobs", s: (d.jobs ? "<b>" + d.jobs.length + "</b> jobs · r" : "R") + "eviews, World Bank, Drive" }),
      ingest: node({ id: "ingest", t: "Ingestion service", f: "ingestion-service/", s: "Gmail and Drive into pgvector and Pinecone." }),
      bills: node({ id: "bills", t: "Bills pipeline", f: "email_attachments.py → register.py", goto: "bills", s: "Inbox PDFs to the bills register." }),
      notify: node({ id: "notify", t: "Notify", f: "notify.py", s: "Task results back to you." }),
    };
    el.innerHTML =
      '<header class="ax-head"><div>' +
        '<div class="vx-eyebrow">Architecture · read live from your API</div>' +
        '<h1 class="vx-h1">How Hermes is built</h1>' +
      '</div><div class="sx-ctrl">' +
        '<span class="vx-switch' + (SX.ctx ? " on" : "") + '" data-sw="context-toggle-input" role="switch" aria-checked="' + SX.ctx + '" tabindex="0"><i></i>Business context</span>' +
        '<span class="vx-switch' + (SX.apps ? " on" : "") + '" data-sw="apps-toggle-input" role="switch" aria-checked="' + SX.apps + '" tabindex="0"><i></i>Apps access</span>' +
      "</div></header>" +
      '<div class="ax-board" id="sx-board"><svg class="sx-wires" id="sx-wires"></svg>' +
        '<div class="ax-grid">' +
          col("01", "Clients", N.web + N.voice + N.meet) +
          '<div class="ax-zone ax-vercel"><div class="ax-tag">Vercel · serverless</div>' +
            col("02", "API", N.api) + col("03", "Agent runtime", N.agents + N.core + N.model + N.guard) +
            col("04", "Context", N.mem + N.skills + N.ctx) +
          "</div>" +
          col("05", "Data", '<div class="ax-sub"><div class="ax-tag">AWS · us-east-1</div>' + N.pg + N.s3 + "</div>" + N.pine) +
          col("06", "External services", N.maton + N.llm + N.tools) +
        "</div>" +
        '<div class="ax-band"><div class="ax-lbl"><b>07</b>Background workers</div><div class="ax-band-row">' + N.sched + N.jobs + N.ingest + N.bills + N.notify + "</div></div>" +
      "</div>" +
      '<div class="ax-legend"><span><i></i>Request path</span><span><i class="gate"></i>Checked by guardrails</span><span><i class="off"></i>Switched off</span></div>';
    el.querySelectorAll(".ax-node").forEach((n, i) => {
      n.style.animationDelay = i * 40 + "ms";
      n.addEventListener("click", () => n.dataset.goto && go(n.dataset.goto));
      n.addEventListener("mouseenter", () => sxHover(n.dataset.n, true));
      n.addEventListener("mouseleave", () => sxHover(n.dataset.n, false));
    });
    el.querySelectorAll("[data-sw]").forEach((s) => {
      const flip = () => { const i = $(s.dataset.sw); if (i) i.click(); };
      s.addEventListener("click", flip);
      s.addEventListener("keydown", (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); flip(); } });
    });
    requestAnimationFrame(sxWires);
    setTimeout(sxWires, 900);
    const board = $("sx-board");
    if (board && window.ResizeObserver) {
      if (SX.ro) SX.ro.disconnect();
      SX.ro = new ResizeObserver(() => sxWires());
      SX.ro.observe(board);
    }
  }

  function sxWires() {
    const board = $("sx-board"), svg = $("sx-wires");
    if (!board || !svg) return;
    const box = board.getBoundingClientRect();
    if (!box.width) { setTimeout(sxWires, 200); return; }
    svg.setAttribute("viewBox", "0 0 " + box.width + " " + box.height);
    const n = (id) => board.querySelector('[data-n="' + id + '"]');
    SX.wires = {};
    let ends = "";
    svg.innerHTML = EDGES.map(([a, b, tag = ""]) => {
      const A = n(a), B = n(b);
      if (!A || !B) return "";
      const ra = A.getBoundingClientRect(), rb = B.getBoundingClientRect();
      const L = (r) => r.left - box.left, T = (r) => r.top - box.top;
      let d, ex, ey;
      if (tag.includes("side")) {
        const x1 = L(ra), y1 = T(ra) + ra.height / 2, x2 = L(rb), y2 = T(rb) + rb.height / 2, gx = Math.min(x1, x2) - 16;
        d = "M" + x1 + "," + y1 + " C" + gx + "," + y1 + " " + gx + "," + y2 + " " + x2 + "," + y2; ex = x2; ey = y2;
      } else if (!tag.includes("v") && rb.left >= ra.right - 4) {
        const x1 = L(ra) + ra.width, y1 = T(ra) + ra.height / 2, x2 = L(rb), y2 = T(rb) + rb.height / 2, mx = (x1 + x2) / 2;
        d = "M" + x1 + "," + y1 + " C" + mx + "," + y1 + " " + mx + "," + y2 + " " + x2 + "," + y2; ex = x2; ey = y2;
      } else if (rb.top >= ra.bottom - 4) {
        const x1 = L(ra) + ra.width / 2, y1 = T(ra) + ra.height, x2 = L(rb) + rb.width / 2, y2 = T(rb), my = (y1 + y2) / 2;
        d = "M" + x1 + "," + y1 + " C" + x1 + "," + my + " " + x2 + "," + my + " " + x2 + "," + y2; ex = x2; ey = y2;
      } else {
        const x1 = L(ra) + ra.width / 2, y1 = T(ra), x2 = L(rb) + rb.width / 2, y2 = T(rb) + rb.height, my = (y1 + y2) / 2;
        d = "M" + x1 + "," + y1 + " C" + x1 + "," + my + " " + x2 + "," + my + " " + x2 + "," + y2; ex = x2; ey = y2;
      }
      const off = (tag.includes("ctx") && !SX.ctx) || (tag.includes("apps") && !SX.apps);
      ends += '<circle class="ax-end' + (off ? " off" : tag.includes("gate") ? " gate" : "") + '" cx="' + ex + '" cy="' + ey + '" r="2.4"/>';
      return '<path class="sx-wire' + (tag.includes("gate") ? " gate" : "") + (off ? " off" : "") + '" data-e="' + a + "-" + b + '" d="' + d + '"/>';
    }).join("") + ends;
    svg.querySelectorAll("path").forEach((p) => { SX.wires[p.dataset.e] = p; });
  }
  function packet(a, b, opt = {}) {
    const p = SX.wires[a + "-" + b], svg = $("sx-wires");
    if (!p || !svg || p.classList.contains("off")) return;
    const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    c.setAttribute("r", 2.4);
    c.setAttribute("class", "sx-pkt" + (p.classList.contains("gate") ? " amber" : ""));
    svg.appendChild(c);
    const len = p.getTotalLength(), D = opt.dur || 1300, t0 = performance.now();
    (function f(t) {
      const k = Math.min(1, (t - t0) / D), e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
      const pt = p.getPointAtLength(len * (opt.rev ? 1 - e : e));
      c.setAttribute("cx", pt.x); c.setAttribute("cy", pt.y);
      if (k < 1) requestAnimationFrame(f); else c.remove();
    })(t0);
  }
  function sxHover(id, on) {
    Object.entries(SX.wires).forEach(([k, p]) => { if (k.split("-").includes(id)) p.classList.toggle("hot", on); });
  }
  setInterval(() => {
    if (view() !== "schema" || document.hidden) return;
    const live = EDGES.filter(([, , t = ""]) => !((t.includes("ctx") && !SX.ctx) || (t.includes("apps") && !SX.apps)));
    const [a, b] = live[Math.floor(Math.random() * live.length)];
    packet(a, b, { rev: Math.random() < 0.35 });
  }, 600);

  async function sxOpen() {
    sxState();
    await sxData();
    sxRender();
  }
  ["context-toggle-input", "apps-toggle-input"].forEach((id) => {
    const i = $(id);
    i && i.addEventListener("change", () => { sxState(); if (view() === "schema") { sxRender(); toast({ title: id.startsWith("context") ? "Business context" : "Apps access", body: "Turned <b>" + (i.checked ? "on" : "off") + "</b>. The architecture shows the new path." }); } });
  });
  ["model-select", "effort-select"].forEach((id) => $(id) && $(id).addEventListener("change", () => {
    if (id === "model-select" && view() === "schema") { SX.d.active = $(id).value; sxRender(); return; }
    if ($("sx-effort")) $("sx-effort").textContent = selText("effort-select").split(" — ")[0];
  }));
  addEventListener("resize", () => view() === "schema" && sxWires());

  /* ═════════════════════════ BILLING OVERVIEW ═════════════════════════ */
  const BO = { bills: [], reg: { lines: [] }, biz: [], posted: 0, held: 0, rejected: 0, decided: {}, scan: 0 };
  async function boData() {
    const [b, r, z] = await Promise.all([get("/api/bills"), get("/api/register"), get("/api/businesses")]);
    BO.allBills = b.bills || []; BO.reg = r || { lines: [] }; BO.biz = z.businesses || [];
    const cur = window.HermesBiz ? HermesBiz.current : "all";
    const bz = BO.biz.find((x) => x.slug === cur);
    BO.scope = bz || null;
    BO.bills = bz ? BO.allBills.filter((x) => x.business_id === bz.business_id) : BO.allBills;
    BO.lines = (BO.reg.lines || []).filter((l) => !bz || l.entity === bz.name);
  }
  const bizOf = (b) => BO.biz.find((x) => x.business_id === b.business_id) || {};
  const shape = (st, x, y, r) => {
    if (st === "overdue") return `<path class="hd" d="M${x},${y - r - 1} L${x + r + 1},${y + r} L${x - r - 1},${y + r} Z"/>`;
    if (st === "paid") return `<rect class="hd" x="${x - r + 1}" y="${y - r + 1}" width="${2 * r - 2}" height="${2 * r - 2}"/>`;
    return `<circle class="hd" cx="${x}" cy="${y}" r="${r}"/>`;
  };
  const GLYPH = { overdue: "▲", "due-soon": "●", pending: "○", paid: "■", flag: "◆" };

  function needsList() {
    const out = [];
    BO.bills.filter((b) => b.status === "overdue").forEach((b) => out.push({ g: "▲", t: `${b.vendor} — ${-daysTo(b.due_date)} days past due`, d: `${usd(b.amount, 2)} · invoice ${b.invoice_number}`, e: bizOf(b).name, acts: [["Open", () => window.openBillViewer && openBillViewer(b.bill_id)], ["Ask", () => ask(`What's the situation with the overdue ${b.vendor} bill (${b.invoice_number})?`)]] }));
    BO.lines.filter((l) => /PROBLEM|DISPUTED/i.test(l.status)).forEach((l) => out.push({ g: "◆", t: `${l.vendor} — ${l.status.replace("ACTIVE — ", "").toLowerCase()}`, d: `${l.what_it_is} · ${l.billing_basis}`, e: l.entity, acts: [["Register", () => bm("register")], ["Ask", () => ask(`Explain the ${l.status.toLowerCase()} status on ${l.vendor} (${l.ref}).`)]] }));
    BO.bills.filter((b) => b.status !== "paid" && b.confidence < 0.8).forEach((b) => out.push({ g: "◇", c: 1, t: `${b.vendor} — business match uncertain`, d: `${Math.round(b.confidence * 100)}% confidence on the assigned business · ${usd(b.amount, 2)}`, e: bizOf(b).name, acts: [["Assign", () => window.showConfirmFor && showConfirmFor(b.bill_id)]] }));
    const unv = BO.lines.filter((l) => l.amount_unverified && !l.proposal).length;
    if (unv) out.push({ g: "○", c: 1, t: `${unv} register amount${unv > 1 ? "s" : ""} unverified, no document yet`, d: "Hermes reads the original PDFs to propose an amount", e: "Register", acts: [["Verify", () => { bm("register"); window.runVerification && runVerification(); }]] });
    return out;
  }

  function boRender(animate) {
    const el = $("bill-overview");
    if (!el) return;
    const bills = BO.bills, open = bills.filter((b) => b.status !== "paid");
    const sum = (a) => a.reduce((s, b) => s + (+b.amount || 0), 0);
    const od = bills.filter((b) => b.status === "overdue");
    const in7 = open.filter((b) => { const d = daysTo(b.due_date); return d >= 0 && d <= 7; });
    const in30 = open.filter((b) => { const d = daysTo(b.due_date); return d >= 0 && d <= 30; });
    const in45 = open.filter((b) => { const d = daysTo(b.due_date); return d >= 0 && d <= 45; });
    const needs = needsList();
    const props = BO.lines.filter((l) => l.proposal && !BO.decided[l.proposal.proposal_id]);
    const watch = bills.filter((b) => b.status !== "paid" && b.confidence < 0.8).length + BO.lines.filter((l) => /DISPUTED/i.test(l.status)).length + BO.lines.filter((l) => l.amount_unverified && !l.proposal).length;
    const nowCount = od.length + BO.lines.filter((l) => /PROBLEM/i.test(l.status)).length;
    const scope = BO.scope ? BO.scope.name : "all businesses";
    el.innerHTML = `
      <div class="bo-triage">
        <button type="button" class="bo-tri warn" data-jump="bo-need"><span class="g">▲</span><b class="bo-val" style="margin:0">${nowCount}</b><span>Needs you now</span></button>
        <button type="button" class="bo-tri" data-jump="bo-queue"><span class="g">●</span><b class="bo-val" style="margin:0">${props.length}</b><span>Prepared for approval</span></button>
        <button type="button" class="bo-tri" data-jump="bo-need"><span class="g">○</span><b class="bo-val" style="margin:0">${watch}</b><span>Watch-risk</span></button>
      </div>

      <section class="bo-sec">
        <div class="bo-sec-h"><div><div class="vx-eyebrow">01 · Horizon</div><h3>What lands, and when</h3>
          <p>Every bill for ${esc(scope)} on one axis from three weeks back to six weeks out. Position is the due date, height is the amount, and the marker shape is the state.</p></div></div>
        <div class="bo-card">
          <div class="bo-tiles">
            <div class="bo-tile warn"><div class="k">▲ Overdue</div><div class="bo-val">${usd(sum(od))}</div><div class="s">${od.length} bill${od.length === 1 ? "" : "s"}</div></div>
            <div class="bo-tile"><div class="k">● Due ≤ 7 days</div><div class="bo-val">${usd(sum(in7))}</div><div class="s">${in7.length} bill${in7.length === 1 ? "" : "s"}</div></div>
            <div class="bo-tile"><div class="k">○ Committed 30d</div><div class="bo-val">${usd(sum(in30))}</div><div class="s">${in30.length} bill${in30.length === 1 ? "" : "s"}</div></div>
            <div class="bo-tile"><div class="k">○ Committed 45d</div><div class="bo-val">${usd(sum(in45))}</div><div class="s">${in45.length} bill${in45.length === 1 ? "" : "s"}</div></div>
            <div class="bo-tile warn"><div class="k">◆ Needs you</div><div class="bo-val">${needs.length}</div><div class="s">listed below</div></div>
          </div>
          <div class="bo-hz" id="bo-hz"></div>
          <div class="bo-legend"><span>▲ Overdue</span><span>● Due ≤ 7d</span><span>○ Scheduled</span><span>■ Paid</span><span style="color:#ffb64d">◇ Low-confidence match</span><span class="note">Stem height = amount · position = due date</span></div>
        </div>
      </section>

      <section class="bo-sec">
        <div class="bo-two">
          <div id="bo-need">
            <div class="bo-sec-h"><div><div class="vx-eyebrow">02 · Triage</div><h3>Needs you now</h3></div></div>
            <div class="bo-card bo-need">${needs.length ? needs.map((n, i) => `
              <div class="bo-need-row" style="animation-delay:${i * 60}ms"><span class="g${n.c ? " c" : ""}">${n.g}</span>
                <div><div class="t">${esc(n.t)}</div><div class="d">${esc(n.d)}</div>${n.e ? `<div class="e">${esc(n.e)}</div>` : ""}</div>
                <div class="a">${n.acts.map((a, j) => `<button type="button" class="bo-mini" data-need="${i}:${j}">${a[0]}</button>`).join("")}</div></div>`).join("")
              : `<div class="bo-col-empty" style="padding:20px 0">Nothing needs you</div>`}</div>
          </div>
          <div id="bo-queue">
            <div class="bo-sec-h"><div><div class="vx-eyebrow">03 · Approval queue</div><h3>Amounts waiting for your GO</h3></div></div>
            <div class="bo-card">
              <div class="bo-q-counts"><div><b class="bo-val" style="margin:0 0 6px">${props.length}</b>Waiting</div><div><b>${BO.posted}</b>Posted</div><div><b>${BO.held}</b>Held</div><div><b>${BO.rejected}</b>Rejected</div></div>
              <div class="bo-q">${props.length ? props.map((l) => qCard(l)).join("") : `<div class="bo-col-empty" style="padding:20px 0">Queue is clear</div>`}</div>
              <div class="bo-keys">GO is press &amp; hold, not a tap · Focus a card: ← reject · → hold · hold Space to GO</div>
            </div>
          </div>
        </div>
      </section>

      <section class="bo-sec">
        <div class="bo-sec-h"><div><div class="vx-eyebrow">04 · Extraction</div><h3>Watch it read the bill</h3>
          <p>Each field lights up on the document where it was found, so a wrong value is easy to spot. Line items are added up and checked against the total.</p></div>
          <div class="bo-pick">${(BO.lines.filter((l) => l.proposal)).map((l, i) => `<button type="button" class="bo-mini${i === BO.scan ? " on" : ""}" data-scan="${i}">${esc(l.vendor)}</button>`).join("")}<button type="button" class="bo-mini" id="bo-rescan">↺ Rescan</button></div></div>
        <div class="bo-scan" id="bo-scan"></div>
      </section>

      <section class="bo-sec">
        <div class="bo-sec-h"><div><div class="vx-eyebrow">05 · Books</div><h3>The books do not touch</h3>
          <p>One column per business. A bill lands in its own book and stays there; Hermes scopes every lookup to the business it belongs to.</p></div></div>
        <div class="bo-books">${booksHTML()}</div>
        <div class="bo-membrane">Entity separation · enforced per business</div>
      </section>`;

    el.querySelectorAll("[data-jump]").forEach((b) => b.addEventListener("click", () => {
      const t = $(b.dataset.jump), sc = el;
      if (t && sc) sc.scrollTo({ top: t.offsetTop - 20, behavior: "smooth" });
    }));
    el.querySelectorAll("[data-need]").forEach((b) => b.addEventListener("click", () => { const [i, j] = b.dataset.need.split(":").map(Number); needs[i].acts[j][1](); }));
    el.querySelectorAll("[data-scan]").forEach((b) => b.addEventListener("click", () => { BO.scan = +b.dataset.scan; el.querySelectorAll("[data-scan]").forEach((x) => x.classList.toggle("on", x === b)); scanRender(); }));
    $("bo-rescan")?.addEventListener("click", scanRender);
    el.querySelectorAll(".bo-chip[data-bill]").forEach((c) => c.addEventListener("click", () => window.openBillViewer && openBillViewer(c.dataset.bill)));
    bindQueue(el);
    horizon(animate);
    scanRender();
  }

  function qCard(l) {
    const p = l.proposal, rp = /DISPUTED/i.test(l.status);
    return `<div class="bo-qc${rp ? " rp" : ""}" tabindex="0" data-pid="${esc(p.proposal_id)}" data-ms="${rp ? 2000 : 1000}">
      <div class="bo-qc-top"><div class="bo-qc-v">${esc(l.vendor)}</div><div class="bo-qc-amt">${usd(p.proposed_amount, 2)}</div></div>
      <div class="bo-qc-meta"><span>${esc(l.ref)}</span><span>${esc(p.invoice_number || "")}</span><span>match ${Math.round((p.match_score || 0) * 100)}%</span><span>${esc(l.entity)}</span>${rp ? '<span class="rp">Disputed · 2s hold</span>' : ""}</div>
      <div class="bo-qc-why">${esc(p.extractor || "")}</div>
      <div class="bo-qc-acts">
        ${p.evidence_bill_id ? `<button type="button" class="bo-mini" data-doc="${esc(p.evidence_bill_id)}">Document</button>` : ""}
        <button type="button" class="bo-mini" data-act="hold">Hold</button>
        <button type="button" class="bo-mini" data-act="reject">Reject</button>
        <button type="button" class="bo-hold" data-act="go"><i></i><span>Hold to GO</span></button>
      </div></div>`;
  }
  function bindQueue(el) {
    el.querySelectorAll(".bo-qc").forEach((card) => {
      const pid = card.dataset.pid, need = +card.dataset.ms, btn = card.querySelector(".bo-hold"), bar = btn.querySelector("i");
      let raf = 0, t0 = 0;
      const done = (kind) => {
        BO.decided[pid] = kind;
        if (kind === "posted") BO.posted++; else if (kind === "held") BO.held++; else BO.rejected++;
        card.querySelector(".bo-qc-acts").insertAdjacentHTML("beforeend", `<span class="bo-qc-state">${kind === "posted" ? "Posted · written to Sheet" : kind}</span>`);
        setTimeout(() => { card.classList.add("gone"); setTimeout(() => boRender(false), 650); }, 650);
      };
      const reset = () => { cancelAnimationFrame(raf); raf = 0; bar.style.transition = "width .3s"; bar.style.width = "0"; setTimeout(() => (bar.style.transition = ""), 300); };
      const start = () => {
        if (raf || BO.decided[pid]) return;
        t0 = performance.now();
        const step = (t) => {
          const k = Math.min(1, (t - t0) / need);
          bar.style.width = k * 100 + "%";
          if (k < 1) { raf = requestAnimationFrame(step); return; }
          raf = 0; btn.classList.add("held"); btn.querySelector("span").textContent = "GO ✓";
          const c = window.confirm; window.confirm = () => true;
          Promise.resolve(window.decideProposal ? decideProposal(pid, true) : null).catch(() => {}).finally(() => { window.confirm = c; });
          toast({ title: "Posted", body: `Amount written to the register for <b>${esc(card.querySelector(".bo-qc-v").textContent)}</b>. Audit line logged.` });
          done("posted");
        };
        raf = requestAnimationFrame(step);
      };
      btn.addEventListener("pointerdown", (e) => { e.preventDefault(); start(); });
      ["pointerup", "pointerleave", "pointercancel"].forEach((ev) => btn.addEventListener(ev, () => raf && reset()));
      card.querySelector('[data-act="hold"]').addEventListener("click", () => done("held"));
      card.querySelector('[data-act="reject"]').addEventListener("click", () => { window.decideProposal && decideProposal(pid, false); done("rejected"); });
      card.querySelector("[data-doc]")?.addEventListener("click", (e) => window.openBillViewer && openBillViewer(e.currentTarget.dataset.doc));
      card.addEventListener("keydown", (e) => {
        if (e.target !== card) return;
        if (e.key === "ArrowLeft") { card.querySelector('[data-act="reject"]').click(); }
        else if (e.key === "ArrowRight") { done("held"); }
        else if (e.key === " " && !e.repeat) { e.preventDefault(); start(); }
      });
      card.addEventListener("keyup", (e) => { if (e.key === " " && raf) reset(); });
    });
  }

  function horizon(animate) {
    const box = $("bo-hz");
    if (!box) return;
    const W = Math.max(480, box.clientWidth), H = 280, L = 8, R = 8, T = 44, B = 28;
    const d0 = -21, d1 = 45, x = (d) => L + ((d - d0) / (d1 - d0)) * (W - L - R), base = H - B;
    const bills = BO.bills.filter((b) => b.due_date).map((b) => ({ ...b, dd: daysTo(b.due_date) })).filter((b) => b.dd >= d0 && b.dd <= d1).sort((a, b) => a.dd - b.dd);
    const max = Math.max(1, ...bills.map((b) => +b.amount || 0));
    let ticks = "";
    for (let d = d0; d <= d1; d += 7) {
      const dt = new Date(Date.now() + d * DAY);
      ticks += `<line class="grid" x1="${x(d)}" x2="${x(d)}" y1="${T - 10}" y2="${base}"/><text class="tk" x="${x(d)}" y="${base + 18}" text-anchor="middle">${dt.toLocaleDateString([], { day: "numeric", month: "short" }).toUpperCase()}</text>`;
    }
    let lastX = -999, lane = 0;
    const stems = bills.map((b, i) => {
      const st = b.status === "overdue" ? "overdue" : b.status === "paid" ? "paid" : b.dd <= 7 && b.dd >= 0 ? "due-soon" : "pending";
      const h = 14 + Math.sqrt((+b.amount || 0) / max) * (base - T - 30), cx = x(b.dd), cy = base - h;
      lane = cx - lastX < 120 ? lane + 1 : 0; lastX = cx;
      const ly = cy - 12 - (lane % 2) * 0;
      const anchorEnd = cx > W - 130;
      const delay = animate ? i * 90 : 0;
      const flag = b.status !== "paid" && b.confidence < 0.8 ? `<path class="flag" d="M${cx},${cy - 10} L${cx + 10},${cy} L${cx},${cy + 10} L${cx - 10},${cy} Z"/>` : "";
      return `<g class="bo-stem st-${st}" data-bill="${esc(b.bill_id)}" data-i="${i}">
        <line x1="${cx}" x2="${cx}" y1="${base}" y2="${cy}" style="animation-delay:${delay}ms"/>
        <rect x="${cx - 9}" y="${cy - 10}" width="18" height="${h + 10}" fill="transparent"/>
        ${shape(st, cx, cy, 4.5).replace('class="hd"', `class="hd" style="animation-delay:${delay + 500}ms"`)}${flag}
        <text class="lb" x="${anchorEnd ? cx - 9 : cx + 9}" y="${ly - (lane % 2) * 13}" text-anchor="${anchorEnd ? "end" : "start"}" style="animation-delay:${delay + 600}ms">${esc(b.vendor.length > 20 ? b.vendor.slice(0, 19) + "…" : b.vendor)}</text>
        <text class="lb2" x="${anchorEnd ? cx - 9 : cx + 9}" y="${ly + 11 - (lane % 2) * 13}" text-anchor="${anchorEnd ? "end" : "start"}" style="animation-delay:${delay + 650}ms">${usd(b.amount)}</text>
      </g>`;
    }).join("");
    box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="height:${H}px">
      <defs>
        <pattern id="bo-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="6" stroke="rgba(255,182,77,0.09)" stroke-width="3"/></pattern>
        <linearGradient id="bo-sweep" x1="0" x2="1"><stop offset="0" stop-color="rgba(127,220,255,0)"/><stop offset="1" stop-color="rgba(127,220,255,0.12)"/></linearGradient>
      </defs>
      <rect class="past" x="${x(d0)}" y="${T - 10}" width="${x(0) - x(d0)}" height="${base - T + 10}"/>
      ${ticks}
      <rect class="sweep" x="0" y="${T - 10}" width="${W * 0.12}" height="${base - T + 10}"/>
      <line class="ax" x1="${L}" x2="${W - R}" y1="${base}" y2="${base}"/>
      <line class="today" x1="${x(0)}" x2="${x(0)}" y1="${T - 26}" y2="${base}"/>
      <text class="today-l" x="${x(0) + 6}" y="${T - 18}">TODAY</text>
      <text class="today-l" x="${x(0) - 6}" y="${T - 18}" text-anchor="end" style="fill:rgba(255,182,77,.6)">PAST DUE ZONE</text>
      ${stems}
    </svg><div class="bo-tip" id="bo-tip"></div>`;
    const tip = $("bo-tip");
    box.querySelectorAll(".bo-stem").forEach((g) => {
      const b = bills[+g.dataset.i];
      g.addEventListener("mouseenter", () => {
        const dd = b.dd;
        tip.innerHTML = `<b>${esc(b.vendor)}</b><div class="amt">${usd(b.amount, 2)}</div>${esc(b.invoice_number || "")} · ${esc(bizOf(b).name || "")}<br>${dd < 0 ? `${-dd} days past due` : dd === 0 ? "due today" : `due in ${dd} days`} · ${esc(b.status)}<br>match ${Math.round((b.confidence || 0) * 100)}% · via ${esc(b.source || "")}`;
        tip.classList.add("on");
      });
      g.addEventListener("mousemove", (e) => {
        const r = box.getBoundingClientRect();
        let lx = e.clientX - r.left + 14; if (lx > r.width - 220) lx -= 240;
        tip.style.left = lx + "px"; tip.style.top = Math.max(0, e.clientY - r.top - 40) + "px";
      });
      g.addEventListener("mouseleave", () => tip.classList.remove("on"));
      g.addEventListener("click", () => window.openBillViewer && openBillViewer(b.bill_id));
    });
  }

  function scanRender() {
    const host = $("bo-scan");
    if (!host) return;
    const lines = BO.lines.filter((l) => l.proposal);
    const l = lines[BO.scan] || lines[0];
    if (!l) { host.innerHTML = `<div class="bo-col-empty">No documents to read</div>`; return; }
    const p = l.proposal, bill = BO.allBills.find((b) => b.bill_id === p.evidence_bill_id) || {};
    const items = p.line_items || [];
    const sum = items.reduce((s, i) => s + parseFloat(String(i.total).replace(/,/g, "")), 0);
    const ok = Math.abs(sum - p.proposed_amount) < 0.01;
    const score = p.match_score || 0;
    host.innerHTML = `
      <div class="bo-doc" id="bo-doc">
        <div class="row"><div><div class="v" data-f="vendor">${esc(l.vendor.toUpperCase())}</div><div class="ad">${esc(l.what_it_is)}</div></div>
          <div style="text-align:right"><div class="lbl">Invoice</div><div data-f="invoice" style="font-weight:700">${esc(p.invoice_number || "—")}</div>
          <div class="lbl" style="margin-top:6px">Due</div><div data-f="due">${esc(bill.due_date || "—")}</div></div></div>
        <div class="blk"><div class="lbl">Bill to</div><div data-f="bill to">${esc(l.entity)}</div></div>
        <div class="blk">${items.map((i, k) => `<div class="li" data-f="line ${k + 1}"><span>${esc(i.description)}</span><span>${esc(i.total)}</span></div>`).join("")}</div>
        <div class="tot" data-f="total"><span>TOTAL ${esc(p.proposed_currency || "USD")}</span><span>${Number(p.proposed_amount).toLocaleString(undefined, { minimumFractionDigits: 2 })}</span></div>
        <div class="bo-beam" id="bo-beam"></div>
      </div>
      <div class="bo-card bo-ext">
        <div class="bo-ext-top">
          <div class="bo-ring${score < 0.9 ? " low" : ""}"><svg viewBox="0 0 84 84"><circle class="trk" cx="42" cy="42" r="36"/><circle class="arc" id="bo-ring-arc" cx="42" cy="42" r="36" stroke-dasharray="226.2" stroke-dashoffset="226.2"/></svg><b>${Math.round(score * 100)}%</b></div>
          <div><div class="vx-eyebrow">What it found</div><div style="margin-top:8px;font:600 15px/1.3 'Chakra Petch',sans-serif;color:#f2fbff">Match confidence ${score < 0.9 ? "· CHECK" : "· clear"}</div>
          <div style="margin-top:4px;font-size:12px;color:rgba(190,215,230,.8)">Under 90% gets a written CHECK label.</div></div>
        </div>
        <div class="bo-ext-row" data-k="vendor"><span class="k">Vendor</span><span class="v">${esc(l.vendor)}</span><span class="c">FOUND</span></div>
        <div class="bo-ext-row" data-k="invoice"><span class="k">Invoice #</span><span class="v">${esc(p.invoice_number || "—")}</span><span class="c">FOUND</span></div>
        <div class="bo-ext-row" data-k="due"><span class="k">Due date</span><span class="v">${esc(bill.due_date || "—")}</span><span class="c">FOUND</span></div>
        <div class="bo-ext-row" data-k="bill to"><span class="k">Business</span><span class="v">${esc(l.entity)}</span><span class="c">FOUND</span></div>
        <div class="bo-ext-row" data-k="line"><span class="k">Line items</span><span class="v">${items.length} · sum ${sum.toLocaleString(undefined, { minimumFractionDigits: 2 })}</span><span class="c">FOUND</span></div>
        <div class="bo-ext-row" data-k="total"><span class="k">Total</span><span class="v">${usd(p.proposed_amount, 2)}</span><span class="c${ok ? "" : " chk"}">${ok ? "SUMS ✓" : "CHECK"}</span></div>
        <div style="margin-top:14px;display:flex;gap:8px;flex-wrap:wrap">
          ${p.evidence_bill_id ? `<button type="button" class="vx-btn" id="bo-open-doc">Open original</button>` : ""}
          <button type="button" class="vx-btn" id="bo-to-queue">Review in queue ↑</button>
        </div>
      </div>`;
    $("bo-open-doc")?.addEventListener("click", () => window.openBillViewer && openBillViewer(p.evidence_bill_id));
    $("bo-to-queue")?.addEventListener("click", () => { const q = $("bo-queue"), sc = $("bill-overview"); sc && sc.scrollTo({ top: q.offsetTop - 20, behavior: "smooth" }); });
    const doc = $("bo-doc"), beam = $("bo-beam"), fields = [...doc.querySelectorAll("[data-f]")];
    const rows = [...host.querySelectorAll(".bo-ext-row")];
    const Hd = doc.offsetHeight, D = 2600, t0 = performance.now();
    (function f(t) {
      const k = Math.min(1, (t - t0) / D), y = -60 + k * (Hd + 60);
      beam.style.transform = `translateY(${y}px)`;
      fields.forEach((fl) => {
        if (!fl.classList.contains("hit") && y + 60 > fl.offsetTop + fl.offsetHeight / 2) {
          fl.classList.add("hit");
          const key = fl.dataset.f.startsWith("line") ? "line" : fl.dataset.f;
          rows.find((r) => r.dataset.k === key)?.classList.add("in");
        }
      });
      if (k < 1) requestAnimationFrame(f);
      else { beam.style.opacity = 0; const a = $("bo-ring-arc"); if (a) a.style.strokeDashoffset = (226.2 * (1 - score)).toFixed(1); }
    })(t0);
  }

  function booksHTML() {
    const cols = BO.scope ? [BO.scope] : BO.biz;
    return cols.map((z, ci) => {
      const bs = BO.allBills.filter((b) => b.business_id === z.business_id).sort((a, b) => (a.due_date < b.due_date ? -1 : 1));
      const openSum = bs.filter((b) => b.status !== "paid").reduce((s, b) => s + (+b.amount || 0), 0);
      const code = (z.business_id || z.slug || "").replace(/^b-/, "").toUpperCase();
      return `<div class="bo-col"><div class="bo-col-h"><div class="code"><i style="background:${esc(z.color || "#7fdcff")}"></i>${esc(code)}${z.connected === false ? '<span style="flex-basis:100%;font-size:8.5px;color:#ffb64d;letter-spacing:.14em">NOT LINKED</span>' : ""}</div><div class="nm">${esc(z.name)}</div></div>
        <div class="bo-col-b">${bs.length ? bs.map((b, i) => `<div class="bo-chip${b.status === "overdue" ? " od" : b.status === "paid" ? " pd" : ""}" data-bill="${esc(b.bill_id)}" style="animation-delay:${ci * 120 + i * 140}ms"><span class="a">${b.status === "overdue" ? "▲ " : b.status === "paid" ? "■ " : ""}${usd(b.amount, 2)}</span>${esc(b.vendor)}</div>`).join("") : `<div class="bo-col-empty">No bills</div>`}</div>
        <div class="bo-col-f"><small>Open</small>${usd(openSum, 2)}</div></div>`;
    }).join("");
  }

  async function boOpen(animate = true) {
    await boData();
    boRender(animate);
  }

  let bmUser = false;
  const bm = (mode) => { bmUser = true; setBillingMode(mode); };
  function hookBilling() {
    const orig = window.setBillingMode;
    if (typeof orig !== "function") return setTimeout(hookBilling, 200);
    let cur = "overview";
    document.addEventListener("click", (e) => { if (e.target.closest && e.target.closest(".reg-tab")) bmUser = true; }, true);
    const showOverview = () => {
      document.querySelectorAll(".reg-tab").forEach((b) => b.classList.toggle("is-active", b.dataset.mode === "overview"));
      ["register-pane", "documents-pane"].forEach((id) => { const e = $(id); if (e) e.hidden = true; });
    };
    window.setBillingMode = function (mode) {
      const user = bmUser; bmUser = false;
      if (mode !== "overview" && !user && cur === "overview") { orig(mode); showOverview(); }
      else { cur = mode; if (mode !== "overview") orig(mode); else showOverview(); }
      const bv = $("bills-view"), ov = $("bill-overview");
      if (bv) bv.dataset.bmode = cur;
      if (ov) { const was = ov.hidden; ov.hidden = cur !== "overview"; if (cur === "overview" && (was || mode === "overview")) boOpen(true); }
    };
  }

  function init() {
    hookBilling();
    const app = $("app");
    let lastView = view();
    new MutationObserver(() => {
      const v = view();
      if (v === lastView) return;
      lastView = v;
      if (v === "schema") sxOpen();
    }).observe(app, { attributes: true, attributeFilter: ["data-view"] });
    if (view() === "schema") sxOpen();
    if (window.HermesBiz && HermesBiz.onChange) HermesBiz.onChange(() => {
      const ov = $("bill-overview");
      if (ov && !ov.hidden && view() === "bills") boOpen(false);
    });
    let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { const ov = $("bill-overview"); if (ov && !ov.hidden) horizon(false); }, 150); });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
