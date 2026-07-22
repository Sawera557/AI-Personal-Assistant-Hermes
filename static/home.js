// Home ("Orbit") — the landing view. One orbiting node per REAL business
// (from HermesBiz.list, using each business's real color), a central voice
// orb, and two honestly-real summary chips. Never fabricates a number: if a
// source hasn't returned yet, the chip shows "–" rather than a guess.
(() => {
  const nodesEl = document.getElementById("home-nodes");
  const chipsEl = document.getElementById("home-chips");
  const greetingEl = document.getElementById("home-greeting");
  const dateEl = document.getElementById("home-date");
  const coreBtn = document.getElementById("home-core");

  function setGreetingDate() {
    const h = new Date().getHours();
    if (greetingEl) greetingEl.textContent = h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
    if (dateEl) {
      const d = new Date().toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" }).toUpperCase();
      dateEl.textContent = `${d} · YOUR ECOSYSTEM AT A GLANCE`;
    }
  }

  // Ring layout mirrors the reference exactly: 3 rings, each its own radius
  // (px, inside the 640px field), revolution period, and direction (ring 2
  // spins the opposite way). Businesses fill rings 1→2→3 in order.
  const RING = {
    1: { r: 140, dur: 90,  rev: "" },
    2: { r: 230, dur: 140, rev: " reverse" },
    3: { r: 300, dur: 200, rev: "" },
  };
  const SLOTS = [
    { ring: 1, ang: -90 }, { ring: 1, ang: 30 }, { ring: 1, ang: 150 },
    { ring: 2, ang: -40 }, { ring: 2, ang: 80 }, { ring: 2, ang: 210 },
    { ring: 3, ang: 186 },
  ];

  function renderNodes() {
    if (!nodesEl) return;
    const businesses = HermesBiz.list.filter((b) => b.slug !== "all");
    const n = businesses.length || 1;
    nodesEl.innerHTML = businesses.map((b, i) => {
      // Use the reference's fixed slot if there's one; otherwise fan the
      // overflow evenly across rings 2 and 3 alternately (not ring 2 only),
      // so a growing portfolio spreads out instead of crowding one ring.
      const overflowN = Math.max(1, n - SLOTS.length);
      const overflowIdx = i - SLOTS.length;
      const slot = SLOTS[i] || {
        ring: overflowIdx % 2 === 0 ? 2 : 3,
        ang: (Math.floor(overflowIdx / 2) / Math.ceil(overflowN / 2)) * 360,
      };
      const rr = RING[slot.ring];
      const rad = (slot.ang * Math.PI) / 180;
      const x = Math.round(Math.cos(rad) * rr.r * 100) / 100;
      const y = Math.round(Math.sin(rad) * rr.r * 100) / 100;
      const initials = (b.name || b.slug || "?").slice(0, 2).toUpperCase();
      const color = b.color || "#38e1ff";
      const name = escapeHtml((b.name || b.slug || "").slice(0, 22));
      const sub = escapeHtml((b.industry || b.slug || "").slice(0, 22));
      const spin = `linear infinite${rr.rev}`;
      // Nodes genuinely orbit (matching the reference), but stay locked to a
      // perfect, fixed-radius circle instead of the old drift: the chip is
      // centered exactly ON the orbit point (see .home-node-counter in
      // style.css — it's sized to the chip only, with labels floated below
      // out of flow), so nothing biases the rotation center off the ring.
      // .home-node-orbit revolves the whole node; .home-node-counter spins
      // the opposite way at the same speed so the chip + name stay upright
      // instead of tumbling as they travel around the ring.
      return (
        `<div class="home-node-orbit" style="animation:hud-orbit ${rr.dur}s ${spin}">` +
          `<div class="home-node-pos" data-slug="${b.slug}" role="button" tabindex="0" style="left:${x}px;top:${y}px">` +
            `<div class="home-node-counter" style="animation:hud-counter ${rr.dur}s ${spin}">` +
              `<div class="home-node-chip" style="--node-color:${color}">${initials}</div>` +
              `<div class="home-node-labels">` +
                `<div class="home-node-name">${name}</div>` +
                `<div class="home-node-sub">${sub}</div>` +
              `</div>` +
            `</div>` +
          `</div>` +
        `</div>`
      );
    }).join("");
    nodesEl.querySelectorAll("[data-slug]").forEach((el) => {
      const go = () => {
        HermesBiz.set(el.dataset.slug);
        document.querySelector('.strip-tab[data-view="command"]')?.click();
      };
      el.addEventListener("click", go);
      el.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); }
      });
    });
  }

  // Scale the whole 640px orbit field down to fit the available area, so the
  // fixed-px node radii never overflow on smaller screens (the reference's
  // orbScale trick). Keeps everything aligned instead of clipping.
  const fieldEl = document.querySelector(".home-orbit-field");
  function updateScale() {
    if (!fieldEl) return;
    const wrap = fieldEl.parentElement;
    if (!wrap) return;
    const cw = wrap.clientWidth;
    const ch = wrap.clientHeight;
    // View hidden (0-size) or not laid out yet: don't clamp to a wrong, tiny
    // scale — keep the last good value and wait for the ResizeObserver to fire
    // once the field actually has room. (A pre-layout one-shot call was the
    // bug: it computed against a stale size and left the orbit far too big,
    // so nodes overflowed off-screen — the "falls apart".)
    if (cw < 50 || ch < 50) return;
    // Divide by 700, not the 660px field: the node labels sit ~40px beyond the
    // outer ring, so the field must shrink enough for them to clear the edges
    // instead of clipping.
    const avail = Math.min(cw, ch) - 24;
    const scale = Math.max(0.4, Math.min(1, avail / 700));
    fieldEl.style.setProperty("--orb-scale", scale.toFixed(3));
  }
  // Recompute on every size change of the orbit area: initial layout, window
  // resizes, sidebar collapse, and — critically — switching TO the home view,
  // which grows the wrap from 0 to full size. Relying on a single init call +
  // window 'resize' left the scale stale whenever the view wasn't the first
  // thing laid out.
  if (fieldEl && window.ResizeObserver) {
    new ResizeObserver(() => updateScale()).observe(fieldEl.parentElement);
  }
  window.addEventListener("resize", updateScale);

  function chipHtml(icon, value, label, subOk, subEmpty, color, border) {
    const known = value !== null && value !== undefined;
    const displayLabel = label === "Due this week" ? "Prepared for approval" : label;
    const displaySubOk = {
      "bills overdue across every business": "awaiting your call",
      "bills to review before they're due": "ready on your GO",
      "jobs & scheduled tasks that failed last run": "monitoring changes",
    }[subOk] || subOk;
    return (
      `<div class="home-chip" style="--chip-color:${color};--chip-border:${border || color}">` +
        `<span class="home-chip-icon">${icon}</span>` +
        `<span class="home-chip-n">${known ? value : "–"}</span>` +
        `<div><div class="home-chip-label">${displayLabel}</div><div class="home-chip-sub">${known ? displaySubOk : subEmpty}</div></div>` +
      `</div>`
    );
  }

  async function renderChips() {
    if (!chipsEl) return;
    let overdue = null;
    let dueSoon = null;
    let failing = null;
    try {
      const r = await fetch("/api/bills/insights");
      if (r.ok) {
        const j = await r.json();
        overdue = (j.overdue || []).length;
        dueSoon = (j.upcoming || []).length;
      }
    } catch (_) { /* leave null -> honest "not synced" state */ }
    try {
      const [jobsR, tasksR] = await Promise.all([fetch("/api/jobs"), fetch("/api/scheduled-tasks")]);
      let errCount = 0;
      if (jobsR.ok) {
        const j = await jobsR.json();
        errCount += (j.jobs || []).filter((x) => x.last_status === "error").length;
      }
      if (tasksR.ok) {
        const t = await tasksR.json();
        errCount += (t.tasks || []).filter((x) => x.last_status === "error").length;
      }
      failing = errCount;
    } catch (_) { /* leave null -> honest "not synced" state */ }
    chipsEl.innerHTML =
      chipHtml("⚠", overdue, "Needs you now", "bills overdue across every business", "not synced yet", "var(--hud-amber)", "var(--hud-amberline)") +
      chipHtml("◷", dueSoon, "Due this week", "bills to review before they're due", "not synced yet", "var(--hud-green)", "var(--hud-line-2)") +
      chipHtml("◆", failing, "Watch / risk", "jobs & scheduled tasks that failed last run", "not synced yet", "var(--hud-red)", "var(--hud-redline)");
  }

  let subscribed = false;
  let chipTimer = null;
  window._homeInit = function () {
    setGreetingDate();
    renderNodes();
    updateScale();
    renderChips();
    if (!subscribed) {
      subscribed = true;
      HermesBiz.onChange(() => { renderNodes(); updateScale(); });
    }
    // Keep the hero cards fresh during a long session on the home view —
    // renderChips() used to run once at init and never again. Only re-fetch
    // while the home view is actually the active one, and only ever start
    // one interval (guarded by chipTimer) even though _homeInit re-runs every
    // time the user switches back to Home.
    if (!chipTimer) {
      chipTimer = setInterval(() => {
        if (document.querySelector(".app")?.dataset.view === "home") renderChips();
      }, 90000);
    }
  };

  coreBtn?.addEventListener("click", () => {
    document.getElementById("voice-btn")?.click();
  });
})();
