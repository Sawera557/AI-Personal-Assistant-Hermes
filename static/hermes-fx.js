/* Hermes · FX — presentation layer for the pro skin.
   Every figure shown is read from the same /api endpoints the tabs use;
   every action drives the app's own controls (setView, HermesBiz, buttons). */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const root = document.documentElement;
  const holo = () => root.classList.contains("holo");
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const get = (u) => fetch(u).then((r) => r.json()).catch(() => ({}));
  const money = (n) => "$" + Number(n || 0).toLocaleString(undefined, { maximumFractionDigits: 0 });
  const go = (v) => (typeof window.setView === "function" ? window.setView(v) : document.querySelector(`.strip-tab[data-view="${v}"]`)?.click());
  const pointer = { x: innerWidth / 2, y: innerHeight / 2, nx: 0, ny: 0 };
  addEventListener("pointermove", (e) => {
    pointer.x = e.clientX; pointer.y = e.clientY;
    pointer.nx = e.clientX / innerWidth - 0.5; pointer.ny = e.clientY / innerHeight - 0.5;
  }, { passive: true });

  let DATA = {};
  // The full document list is slow (seconds), so it never blocks first paint:
  // docs stays null ("…" on screen) until it lands, then brief() re-renders.
  let docsLoading = null;
  function loadDocs() {
    if (!docsLoading) docsLoading = get("/api/documents?entity_id=all").then((r) => {
      docsLoading = null;
      if (r.documents) { DATA.docs = r.documents; if (DATA.mems) brief(); }
    });
    return docsLoading;
  }
  async function loadData() {
    if (!DATA.docs) loadDocs(); // all-business count: once per page load
    const [bills, tasks, jobs, conns, usage, mems, biz] = await Promise.all([
      get("/api/bills"), get("/api/scheduled-tasks"), get("/api/jobs"), get("/api/connections"),
      get("/api/usage?entity_id=all"), get("/api/memories?entity_id=all"), get("/api/businesses"),
    ]);
    const billList = bills.bills || [];
    DATA = {
      bills: billList,
      overdue: billList.filter((b) => b.status === "overdue"),
      dueSoon: billList.filter((b) => b.status === "due-soon"),
      tasks: tasks.tasks || [], jobs: jobs.jobs || [],
      conns: (conns.data && conns.data.connections) || [],
      usage: usage.totals || {}, docs: DATA.docs || null, mems: mems.memories || [],
      biz: biz.businesses || [],
    };
    return DATA;
  }

  /* ── Starfield ───────────────────────────────────────────────────────── */
  function stars() {
    const c = document.createElement("canvas");
    c.id = "hx-stars"; document.body.prepend(c);
    const ctx = c.getContext("2d");
    let W, H, dpr, pts = [];
    function size() {
      dpr = Math.min(2, devicePixelRatio || 1);
      W = c.width = innerWidth * dpr; H = c.height = innerHeight * dpr;
      c.style.width = innerWidth + "px"; c.style.height = innerHeight + "px";
      const n = Math.round((innerWidth * innerHeight) / 9000);
      pts = Array.from({ length: n }, () => ({
        x: Math.random() * W, y: Math.random() * H, z: Math.random() * 0.9 + 0.1,
        t: Math.random() * Math.PI * 2, s: Math.random() * 0.02 + 0.005,
      }));
    }
    size(); addEventListener("resize", size);
    let px = 0, py = 0;
    function frame() {
      if (!holo()) return requestAnimationFrame(frame);
      ctx.clearRect(0, 0, W, H);
      px += (pointer.nx * 40 - px) * 0.04; py += (pointer.ny * 40 - py) * 0.04;
      const mx = pointer.x * dpr, my = pointer.y * dpr;
      const near = [];
      for (const p of pts) {
        if (!reduce) { p.y -= p.z * 0.12 * dpr; p.t += p.s; }
        if (p.y < -10) { p.y = H + 10; p.x = Math.random() * W; }
        const x = p.x + px * p.z * dpr, y = p.y + py * p.z * dpr;
        const a = (0.25 + 0.55 * p.z) * (0.65 + 0.35 * Math.sin(p.t));
        ctx.fillStyle = `rgba(170,230,255,${a})`;
        ctx.fillRect(x, y, p.z * 1.6 * dpr, p.z * 1.6 * dpr);
        const dx = x - mx, dy = y - my;
        if (dx * dx + dy * dy < (160 * dpr) ** 2 && p.z > 0.45) near.push([x, y]);
      }
      ctx.lineWidth = dpr * 0.6;
      for (let i = 0; i < near.length; i++) for (let j = i + 1; j < near.length; j++) {
        const dx = near[i][0] - near[j][0], dy = near[i][1] - near[j][1], d = Math.sqrt(dx * dx + dy * dy);
        if (d < 90 * dpr) {
          ctx.strokeStyle = `rgba(127,220,255,${0.22 * (1 - d / (90 * dpr))})`;
          ctx.beginPath(); ctx.moveTo(near[i][0], near[i][1]); ctx.lineTo(near[j][0], near[j][1]); ctx.stroke();
        }
      }
      requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
    setTimeout(() => c.classList.add("on"), 200);
  }

  /* ── Sliding rail indicator ──────────────────────────────────────────── */
  function rail() {
    const wrap = document.querySelector(".strip-tabs");
    if (!wrap) return;
    const ind = document.createElement("i");
    ind.className = "hx-rail-ind"; wrap.appendChild(ind);
    const place = () => {
      const a = wrap.querySelector(".strip-tab.active");
      if (!a) { ind.style.opacity = 0; return; }
      ind.style.opacity = 1;
      const h = a.offsetHeight * 0.64;
      ind.style.height = h + "px";
      ind.style.transform = `translateY(${a.offsetTop + (a.offsetHeight - h) / 2}px)`;
    };
    new MutationObserver(place).observe($("app"), { attributes: true, attributeFilter: ["data-view"] });
    addEventListener("resize", place); place();
  }

  /* ── Spotlight + ripple ──────────────────────────────────────────────── */
  const SPOT = ".tk-card,.bstat,.usage-stat,.guardrail-row,.context-item,.meeting-box,.reg-proposal,.hud-mod";
  function spotlight() {
    document.addEventListener("pointermove", (e) => {
      const el = e.target.closest && e.target.closest(SPOT);
      if (!el) return;
      const r = el.getBoundingClientRect();
      el.style.setProperty("--mx", e.clientX - r.left + "px");
      el.style.setProperty("--my", e.clientY - r.top + "px");
    }, { passive: true });
    document.addEventListener("pointerout", (e) => {
      const el = e.target.closest && e.target.closest(SPOT);
      if (el && !el.contains(e.relatedTarget)) { el.style.setProperty("--mx", "-400px"); el.style.setProperty("--my", "-400px"); }
    });
    const RIP = ".tasks-new-btn,.bills-upload-btn,.context-add-btn,.meeting-primary-btn,#send-btn,.tk-save,.reg-approve,.hx-ask-go";
    document.addEventListener("pointerdown", (e) => {
      const b = e.target.closest && e.target.closest(RIP);
      if (!b || !holo()) return;
      const r = b.getBoundingClientRect(), s = document.createElement("span");
      s.className = "hx-ripple"; s.style.left = e.clientX - r.left + "px"; s.style.top = e.clientY - r.top + "px";
      b.appendChild(s); setTimeout(() => s.remove(), 750);
    });
  }

  /* ── Count-up on numeric telemetry ───────────────────────────────────── */
  const NUM = ".hud-mini-val,#hud-cost,.hud-spoke .cnt,.bstat-val,.usage-stat-value";
  function countUp() {
    const run = (el) => {
      const txt = el.textContent;
      if (el._hxLast === txt) return;
      const m = txt.match(/^([^\d-]*)(-?[\d,]*\.?\d+)(.*)$/s);
      if (!m) return;
      const to = parseFloat(m[2].replace(/,/g, "")), dec = (m[2].split(".")[1] || "").length;
      const from = el._hxVal || 0;
      el._hxVal = to;
      if (reduce || from === to) { el._hxLast = txt; return; }
      const t0 = performance.now(), D = 1300;
      cancelAnimationFrame(el._hxRaf);
      const step = (t) => {
        const k = Math.min(1, (t - t0) / D), e = 1 - Math.pow(2, -10 * k);
        const v = from + (to - from) * (k >= 1 ? 1 : e);
        const s = m[1] + v.toLocaleString(undefined, { minimumFractionDigits: dec, maximumFractionDigits: dec }) + m[3];
        el._hxLast = k >= 1 ? txt : s;
        el.textContent = k >= 1 ? txt : s;
        if (k < 1) el._hxRaf = requestAnimationFrame(step);
      };
      el._hxRaf = requestAnimationFrame(step);
    };
    new MutationObserver((muts) => {
      const seen = new Set();
      for (const mu of muts) {
        const el = mu.target.nodeType === 3 ? mu.target.parentElement : mu.target;
        if (!el) continue;
        if (el.matches && el.matches(NUM)) seen.add(el);
        if (mu.type === "childList") mu.addedNodes.forEach((n) => n.querySelectorAll && n.querySelectorAll(NUM).forEach((x) => seen.add(x)));
      }
      seen.forEach(run);
    }).observe(document.body, { subtree: true, childList: true, characterData: true });
  }

  /* ── Reactor: ticks, wave, parallax, orbit, spoke focus ──────────────── */
  function reactor() {
    const ticks = $("hx-ticks");
    if (ticks) {
      let s = "";
      for (let i = 0; i < 120; i++) {
        const a = (i / 120) * Math.PI * 2, maj = i % 10 === 0, r1 = 170, r2 = maj ? 158 : 164;
        s += `<line class="${maj ? "maj" : ""}" x1="${200 + r1 * Math.cos(a)}" y1="${200 + r1 * Math.sin(a)}" x2="${200 + r2 * Math.cos(a)}" y2="${200 + r2 * Math.sin(a)}"></line>`;
      }
      ticks.innerHTML = s;
    }
    const rings = $("hud-rings");
    if (!rings) return;
    const layers = [...rings.querySelectorAll(".pl")];
    let rx = 0, ry = 0;
    (function loop() {
      const r = rings.getBoundingClientRect();
      if (r.width) {
        const tx = Math.max(-1, Math.min(1, (pointer.x - (r.left + r.width / 2)) / (innerWidth / 2)));
        const ty = Math.max(-1, Math.min(1, (pointer.y - (r.top + r.height / 2)) / (innerHeight / 2)));
        rx += (tx - rx) * 0.06; ry += (ty - ry) * 0.06;
        if (!reduce) layers.forEach((l) => {
          const d = +l.dataset.depth || 0;
          l.style.transform = `translate3d(${rx * d}px, ${ry * d}px, 0) rotateX(${-ry * 6}deg) rotateY(${rx * 6}deg)`;
        });
      }
      requestAnimationFrame(loop);
    })();

    const cv = $("hx-wave");
    if (cv) {
      const ctx = cv.getContext("2d");
      let energy = 0.35, target = 0.35, t = 0;
      rings._energy = (v) => { target = v; };
      (function draw() {
        t += 0.016; energy += (target - energy) * 0.05;
        const W = cv.width, H = cv.height;
        ctx.clearRect(0, 0, W, H);
        for (let k = 0; k < 3; k++) {
          ctx.beginPath();
          for (let x = 0; x <= W; x += 4) {
            const n = x / W, env = Math.sin(n * Math.PI);
            const y = H * 0.73 + env * energy * 26 * Math.sin(n * (9 + k * 3) + t * (2.2 + k * 0.7) + k) * Math.sin(t * 0.7 + k);
            x ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
          }
          ctx.strokeStyle = `rgba(${k ? "127,220,255" : "201,242,255"},${0.7 - k * 0.22})`;
          ctx.lineWidth = k ? 1 : 1.6;
          ctx.stroke();
        }
        requestAnimationFrame(draw);
      })();
    }

    const label = $("hx-core-label"), sub = $("hx-core-sub"), bizEl = $("hud-core-biz");
    let saved = null;
    document.querySelectorAll(".hud-spoke").forEach((b, i) => {
      b.style.setProperty("--si", i);
      b.addEventListener("mouseenter", () => {
        saved = saved || [label.textContent, bizEl.textContent, sub.textContent];
        const cnt = b.querySelector(".cnt");
        label.textContent = "Route to";
        bizEl.textContent = b.querySelector(".lbl").textContent;
        sub.textContent = cnt && cnt.textContent.trim() && cnt.textContent !== "·" ? cnt.textContent + " records" : "";
        rings.classList.add("hx-focus"); rings._energy && rings._energy(1);
      });
      b.addEventListener("mouseleave", () => {
        if (saved) [label.textContent, bizEl.textContent, sub.textContent] = saved;
        saved = null; rings.classList.remove("hx-focus"); rings._energy && rings._energy(0.35);
      });
    });
  }

  function orbit() {
    const el = $("hx-orbit");
    if (!el) return;
    // One node per app, not per account (9 Gmail accounts are one "Gmail" node).
    // Dot: green = every account active, amber = some active or pending, grey = none.
    const groups = new Map();
    (DATA.conns || []).forEach((c) => {
      const g = groups.get(c.app) || groups.set(c.app, { app: c.app || "", total: 0, active: 0, pending: 0 }).get(c.app);
      const st = (c.status || "").toUpperCase();
      g.total++; if (st === "ACTIVE") g.active++; else if (st === "PENDING") g.pending++;
    });
    const list = [...groups.values()];
    el.innerHTML = list.map((g, i) => {
      const a = (i / Math.max(1, list.length)) * Math.PI * 2 - Math.PI / 2;
      const cls = g.active === g.total ? "" : g.active || g.pending ? "warn" : "off";
      const tip = `${g.total} account${g.total > 1 ? "s" : ""} · ${g.active} active`;
      return `<div class="hx-node ${cls}" style="left:${50 + 50 * Math.cos(a)}%;top:${50 + 50 * Math.sin(a)}%" title="${esc(tip)}"><b></b><span>${esc(g.app.replace(/_/g, " "))}</span></div>`;
    }).join("");
  }

  /* ── Greeting + briefing ─────────────────────────────────────────────── */
  function brief() {
    const h = new Date().getHours();
    if ($("hx-hello")) $("hx-hello").textContent = h < 5 ? "Working late" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
    if ($("hx-date")) $("hx-date").textContent = new Date().toLocaleDateString([], { weekday: "short", day: "numeric", month: "short", year: "numeric" });
    const d = DATA;
    const active = d.conns.filter((c) => (c.status || "").toUpperCase() === "ACTIVE").length;
    const runs = [...d.tasks, ...d.jobs].filter((r) => r.enabled && r.next_run_at && new Date(r.next_run_at) - Date.now() < 864e5).length;
    const errs = d.jobs.filter((j) => j.last_status === "error").length;
    const owed = d.overdue.reduce((s, b) => s + (+b.amount || 0), 0);
    const issues = (d.conns.length - active) + errs;
    const statusText = issues ? `${issues} item${issues > 1 ? "s" : ""} need attention` : "All systems nominal";
    if ($("hx-status")) $("hx-status").textContent = statusText;
    if ($("hx-tb-attn")) $("hx-tb-attn").title = statusText + " — open Home";
    if ($("hx-status-led")) $("hx-status-led").className = "hud-led" + (issues ? " warn" : "");
    const chip = (v, n, label, cls) => `<button type="button" class="hx-chip${cls ? " " + cls : ""}" data-goto="${v}"><b>${n}</b> ${label}</button>`;
    const parts = [];
    parts.push(d.overdue.length
      ? `${chip("bills", d.overdue.length, d.overdue.length === 1 ? "overdue bill" : "overdue bills", "warn")} totalling <b>${money(owed)}</b>`
      : "No overdue bills");
    parts.push(`${chip("tasks", runs, runs === 1 ? "run" : "runs")} scheduled in the next 24 hours`);
    if (errs) parts.push(`${chip("jobs", errs, errs === 1 ? "job failed" : "jobs failed", "warn")} on its last run`);
    const live = d.biz.filter((b) => b.connected).length;
    if ($("hx-brief")) {
      $("hx-brief").innerHTML = parts.join(", ") + `. ${chip("context", d.docs ? d.docs.length + d.mems.length : "…", "knowledge items")} indexed across <b>${live}</b> connected businesses, with <b>${active}/${d.conns.length}</b> apps online.`;
      $("hx-brief").querySelectorAll(".hx-chip").forEach((b) => b.addEventListener("click", () => go(b.dataset.goto)));
    }
  }

  /* ── Clock seconds ring ──────────────────────────────────────────────── */
  function seconds() {
    const el = $("hx-sec-arc");
    if (!el) return;
    const C = 2 * Math.PI * 46;
    el.setAttribute("stroke-dasharray", C.toFixed(1));
    (function f() {
      const n = new Date(), s = (n.getSeconds() + n.getMilliseconds() / 1000) / 60;
      el.setAttribute("stroke-dashoffset", (C * (1 - s)).toFixed(2));
      requestAnimationFrame(f);
    })();
  }

  /* ── Quick ask → Agent composer ──────────────────────────────────────── */
  function ask(text) {
    if (!text) return;
    go("agent");
    const inp = $("input"), form = $("composer");
    if (!inp || !form) return;
    setTimeout(() => {
      inp.value = text;
      inp.dispatchEvent(new Event("input", { bubbles: true }));
      form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
    }, 260);
  }
  function selectors() {
    // The top bar's Business picker — the same HermesBiz scope every tab's own
    // picker is bound to, so switching here switches the whole app.
    const bs = $("hermes-biz-select-top");
    if (bs && window.HermesBiz && HermesBiz.bind) HermesBiz.bind(bs);
    $("hx-tb-attn")?.addEventListener("click", () => go("home"));
  }
  window.HermesAsk = ask;
  function appsLabel() {
    const i = $("apps-toggle-input"), l = $("apps-toggle-label"), w = $("apps-toggle");
    if (!i || !l) return;
    const paint = () => { l.textContent = i.checked ? "On" : "Off"; if (w) w.title = l.textContent; };
    i.addEventListener("change", paint); paint();
  }
  function quickAsk() {
    const f = $("hx-ask");
    if (!f) return;
    f.addEventListener("submit", (e) => { e.preventDefault(); const i = $("hx-ask-input"); const v = i.value.trim(); i.value = ""; ask(v); });
  }

  /* ── Toasts ──────────────────────────────────────────────────────────── */
  function toast({ title, body, kind, actions = [], life = 7000 }) {
    let wrap = document.querySelector(".hx-toasts");
    if (!wrap) { wrap = document.createElement("div"); wrap.className = "hx-toasts"; document.body.appendChild(wrap); }
    const t = document.createElement("div");
    t.className = "hx-toast" + (kind ? " " + kind : "");
    t.innerHTML = `<div style="flex:1;min-width:0"><div class="t-h">${esc(title)}</div><div class="t-b">${body}</div>${actions.length ? `<div class="t-a">${actions.map((a, i) => `<button type="button" data-i="${i}">${esc(a.label)}</button>`).join("")}</div>` : ""}</div><i class="t-life" style="animation-duration:${life}ms"></i>`;
    const bye = () => { t.classList.add("bye"); setTimeout(() => t.remove(), 500); };
    t.querySelectorAll("[data-i]").forEach((b) => b.addEventListener("click", () => { actions[+b.dataset.i].run(); bye(); }));
    wrap.appendChild(t);
    setTimeout(bye, life);
  }
  window.HermesToast = toast;

  /* ── Command palette ─────────────────────────────────────────────────── */
  const VIEWS = [
    ["home", "Home", "Operating picture"], ["agent", "AI Agent", "Chat"], ["schema", "How it works", "Schematic"], ["bills", "Billing & Invoices", "Register"],
    ["meeting", "Meeting Copilot", "Briefs"], ["context", "Business Context", "Files · memories · skills"], ["tasks", "Scheduled Tasks", "Automations"], ["multiagents", "Multi-agents", "Specialist agents"],
    ["files", "File Explorer", "Drive"], ["jobs", "Jobs", "Pipelines"], ["artefacts", "Artifacts", "Live views & files"],
    ["pricing", "Usage & Cost", "Spend"], ["settings", "Settings", "Guardrails"],
  ];
  function commands() {
    const click = (id, view) => () => { if (view) go(view); setTimeout(() => $(id)?.click(), view ? 220 : 0); };
    const flip = (id, name) => () => {
      const i = $(id); if (!i) return;
      i.click();
      toast({ title: name, body: `${name} is now <b>${i.checked ? "on" : "off"}</b> for new messages.` });
    };
    const cmds = [];
    VIEWS.forEach(([v, n, m]) => cmds.push({ g: "Navigate", ic: "→", label: n, meta: m, run: () => go(v) }));
    cmds.push(
      { g: "Actions", ic: "+", label: "New conversation", meta: "Agent", run: click("reset-btn", "agent") },
      { g: "Actions", ic: "+", label: "New meeting", meta: "Meeting", run: click("meeting-new-btn", "meeting") },
      { g: "Actions", ic: "+", label: "New scheduled task", meta: "Tasks", run: click("task-new-btn", "tasks") },
      { g: "Actions", ic: "+", label: "New agent", meta: "Multi-agents", run: click("ma-new-btn", "multiagents") },
      { g: "Actions", ic: "◐", label: "Start voice call", meta: "Agent", run: click("voice-btn", "agent") },
      { g: "Actions", ic: "↻", label: "Refresh jobs", meta: "Jobs", run: click("jobs-refresh-btn", "jobs") },
      { g: "Actions", ic: "⇄", label: "Toggle business context", meta: "Agent", run: flip("context-toggle-input", "Business context") },
      { g: "Actions", ic: "⇄", label: "Toggle apps access", meta: "Agent", run: flip("apps-toggle-input", "Apps access") },
    );
    (window.HermesBiz ? HermesBiz.list : []).forEach((b) => cmds.push({
      g: "Switch business", ic: "◆", label: b.name, meta: b.slug === HermesBiz.current ? "active" : (b.connected === false ? "not connected" : ""),
      run: () => { HermesBiz.set(b.slug); toast({ title: "Business scope", body: `Hermes is now scoped to <b>${esc(b.name)}</b>.` }); },
    }));
    const ms = $("model-select");
    if (ms) [...ms.options].forEach((o) => cmds.push({
      g: "Model", ic: "◇", label: o.textContent, meta: o.selected ? "active" : "",
      run: () => { ms.value = o.value; ms.dispatchEvent(new Event("change", { bubbles: true })); toast({ title: "Model", body: `Agent model set to <b>${esc(o.textContent)}</b>.` }); },
    }));
    return cmds;
  }
  let palEl = null;
  function openPalette() {
    if (palEl) return;
    const all = commands();
    palEl = document.createElement("div");
    palEl.className = "hx-pal-ov";
    palEl.innerHTML = `<div class="hx-pal" role="dialog" aria-label="Command palette">
      <div class="hx-pal-top"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
      <input placeholder="Jump to a module, run an action, or ask Hermes…" aria-label="Command"/><kbd>ESC</kbd></div>
      <div class="hx-pal-list"></div>
      <div class="hx-pal-foot"><span>↑↓ navigate</span><span>↵ run</span><span>type a question + ↵ to ask</span></div></div>`;
    document.body.appendChild(palEl);
    const inp = palEl.querySelector("input"), list = palEl.querySelector(".hx-pal-list");
    let items = [], sel = 0;
    const render = () => {
      const q = inp.value.trim().toLowerCase();
      items = all.filter((c) => !q || (c.label + " " + c.g + " " + c.meta).toLowerCase().includes(q));
      if (q) items.push({ g: "Ask Hermes", ic: "✦", label: `“${inp.value.trim()}”`, meta: "send to agent", run: () => ask(inp.value.trim()) });
      sel = Math.min(sel, items.length - 1); if (sel < 0) sel = 0;
      let g = "", html = "";
      items.forEach((c, i) => {
        if (c.g !== g) { g = c.g; html += `<div class="hx-pal-grp">${esc(g)}</div>`; }
        html += `<button type="button" class="hx-pal-item${i === sel ? " on" : ""}" data-i="${i}"><span class="ic">${c.ic}</span><span>${esc(c.label)}</span><span class="meta">${esc(c.meta || "")}</span></button>`;
      });
      list.innerHTML = html || `<div class="hx-pal-empty">No match</div>`;
      const on = list.querySelector(".on");
      if (on) { const lt = on.offsetTop, lb = lt + on.offsetHeight; if (lt < list.scrollTop) list.scrollTop = lt - 30; else if (lb > list.scrollTop + list.clientHeight) list.scrollTop = lb - list.clientHeight + 8; }
    };
    const run = (i) => { const c = items[i]; closePalette(); c && setTimeout(c.run, 60); };
    inp.addEventListener("input", () => { sel = 0; render(); });
    inp.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") { sel = (sel + 1) % items.length; render(); e.preventDefault(); }
      else if (e.key === "ArrowUp") { sel = (sel - 1 + items.length) % items.length; render(); e.preventDefault(); }
      else if (e.key === "Enter") { run(sel); e.preventDefault(); }
    });
    list.addEventListener("click", (e) => { const b = e.target.closest(".hx-pal-item"); if (b) run(+b.dataset.i); });
    list.addEventListener("mousemove", (e) => { const b = e.target.closest(".hx-pal-item"); if (b && +b.dataset.i !== sel) { sel = +b.dataset.i; list.querySelectorAll(".hx-pal-item").forEach((x) => x.classList.toggle("on", +x.dataset.i === sel)); } });
    palEl.addEventListener("click", (e) => { if (e.target === palEl) closePalette(); });
    render(); inp.focus();
  }
  function closePalette() { if (palEl) { palEl.remove(); palEl = null; } }
  addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); palEl ? closePalette() : openPalette(); }
    else if (e.key === "Escape" && palEl) closePalette();
  });

  /* ── Boot sequence ───────────────────────────────────────────────────── */
  const home = () => $("home-view");
  function stage(on) { const h = home(); if (!h) return; h.classList.toggle("hx-stage", on); }
  function reveal() {
    const h = home(); if (!h) return;
    h.classList.remove("hx-stage", "hx-go"); void h.offsetWidth; h.classList.add("hx-go");
    setTimeout(() => h.classList.remove("hx-go"), 2400);
  }
  function boot(force) {
    if (!force && sessionStorage.getItem("hx-booted")) { reveal(); return Promise.resolve(); }
    sessionStorage.setItem("hx-booted", "1");
    if (($("app") || {}).dataset?.view !== "home") go("home");
    stage(true);
    const ov = document.createElement("div");
    ov.className = "hx-boot";
    const word = "HERMES".split("").map((c, i) => `<i style="animation-delay:${0.9 + i * 0.07}s">${c}</i>`).join("");
    ov.innerHTML = `<div class="hx-boot-inner">
      <svg class="hx-boot-mark" viewBox="0 0 150 150"><circle class="d1" cx="75" cy="75" r="70"/><circle class="d2" cx="75" cy="75" r="58"/><path class="d3" d="M55 48v54M95 48v54M55 75h40"/></svg>
      <div class="hx-boot-word">${word}</div>
      <div class="hx-boot-sub">Autonomous operations · Personal AI</div>
      <div class="hx-boot-log"></div>
      <div class="hx-boot-bar"><i></i></div></div>
      <div class="hx-boot-skip">Click or press any key to skip</div>`;
    document.body.appendChild(ov);
    const log = ov.querySelector(".hx-boot-log"), bar = ov.querySelector(".hx-boot-bar i");
    let done = false, timers = [];
    return new Promise((resolve) => {
      const finish = () => {
        if (done) return; done = true;
        timers.forEach(clearTimeout);
        ov.classList.add("out");
        stage(false); reveal();
        setTimeout(() => { ov.remove(); resolve(); }, 1000);
        removeEventListener("keydown", finish, true);
      };
      ov.addEventListener("click", finish);
      addEventListener("keydown", finish, true);
      const d = DATA, active = d.conns.filter((c) => (c.status || "").toUpperCase() === "ACTIVE").length;
      const lines = [
        ["Authenticating operator session", "OK"],
        [`Linking ${d.conns.length} apps · ${d.conns.slice(0, 3).map((c) => c.app).join(", ")}${d.conns.length > 3 ? "…" : ""}`, active === d.conns.length ? "OK" : `${active}/${d.conns.length}`],
        [`Indexing knowledge · ${d.docs ? d.docs.length : "…"} documents · ${d.mems.length} memories`, "OK"],
        [`Scoping ${d.biz.length} businesses`, "OK"],
        [`Arming scheduler · ${d.tasks.filter((t) => t.enabled).length + d.jobs.filter((j) => j.enabled).length} automations`, "ARMED"],
        [`Ledger scan · ${d.bills.length} bills`, d.overdue.length ? `${d.overdue.length} OVERDUE` : "CLEAR"],
      ];
      lines.forEach(([t, s], i) => timers.push(setTimeout(() => {
        const row = document.createElement("div");
        row.innerHTML = `<span>${esc(t)}</span><em class="${/OVERDUE|\//.test(s) ? "warn" : ""}">${esc(s)}</em>`;
        log.appendChild(row);
        bar.style.width = ((i + 1) / lines.length) * 100 + "%";
      }, 1300 + i * 260)));
      timers.push(setTimeout(finish, 1300 + lines.length * 260 + 700));
    });
  }

  function alertToast() {
    const d = DATA;
    if (d.overdue.length) {
      const owed = d.overdue.reduce((s, b) => s + (+b.amount || 0), 0);
      setTimeout(() => toast({
        title: "Attention · Billing", kind: "warn", life: 9000,
        body: `${d.overdue.length} overdue ${d.overdue.length === 1 ? "bill" : "bills"} totalling <b>${money(owed)}</b>. Top: ${esc(d.overdue[0].vendor)}.`,
        actions: [{ label: "Review", run: () => go("bills") }, { label: "Ask Hermes", run: () => ask("Summarise my overdue bills and what to pay first.") }],
      }), 1200);
    }
  }

  addEventListener("message", (e) => {
    if (!e.data) return;
    if (e.data.hermesBoot) boot(true);
    if (e.data.hermesPalette) openPalette();
  });

  async function init() {
    stage(true);
    stars(); rail(); spotlight(); countUp(); reactor(); seconds(); quickAsk(); selectors(); appsLabel();
    await loadData();
    orbit(); brief();
    await boot(false);
    alertToast();
    if (window.HermesBiz && HermesBiz.onChange) HermesBiz.onChange(() => loadData().then(() => { orbit(); brief(); }));
    document.querySelector('.strip-tab[data-view="home"]')?.addEventListener("click", () => setTimeout(reveal, 0));
    setInterval(() => loadData().then(() => { orbit(); brief(); }), 60000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
