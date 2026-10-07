/* Hermes v2 · System tab motion layer — comets, request trace, spotlight tilt. */
(function () {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const NS = "http://www.w3.org/2000/svg";
  const RM = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const onSchema = () => ($("#app") || {}).dataset?.view === "schema";
  const FX = { tracing: false };

  const STEPS = [
    ["web", "api", 0, "You send a message from the web app"],
    ["api", "core", 0, "FastAPI hands it to the Hermes agent"],
    ["core", "ctx", 0, "The context builder gathers what is relevant"],
    ["ctx", "pine", 0, "It searches this business's Pinecone namespace"],
    ["ctx", "pg", 0, "Memories and documents come from Postgres"],
    ["core", "llm", 0, "The model plans a reply and picks tools"],
    ["core", "guard", 0, "Guardrails check every action first"],
    ["guard", "maton", 0, "Approved actions go out to your apps through Maton"],
    ["core", "s3", 0, "Anything generated is saved to S3"],
    ["api", "core", 1, "The answer returns to the API"],
    ["web", "api", 1, "The reply streams back to you"],
  ];

  const wire = (a, b) => $('#sx-wires path[data-e="' + a + "-" + b + '"]');
  const nodeEl = (id) => $('#sx-board [data-n="' + id + '"]');

  function ping(id) {
    const n = nodeEl(id);
    if (!n) return;
    n.classList.remove("fx-ping");
    void n.offsetWidth;
    n.classList.add("fx-ping");
    setTimeout(() => n.classList.remove("fx-ping"), 900);
  }

  function comet(a, b, opt = {}) {
    return new Promise((done) => {
      const p = wire(a, b), svg = $("#sx-wires");
      if (!p || !svg || p.classList.contains("off")) return done(false);
      const g = document.createElementNS(NS, "g");
      g.setAttribute("class", "fx-comet" + (p.classList.contains("gate") ? " amber" : "") + (opt.trace ? " trace" : ""));
      const TAIL = 7, dots = [];
      for (let i = 0; i < TAIL; i++) {
        const c = document.createElementNS(NS, "circle");
        c.setAttribute("r", (opt.trace ? 3.4 : 2.6) * (1 - i / (TAIL + 2)));
        c.style.opacity = String(1 - i / TAIL);
        if (i === 0) c.setAttribute("class", "head");
        g.appendChild(c); dots.push(c);
      }
      svg.appendChild(g);
      const len = p.getTotalLength(), D = opt.dur || 1400, t0 = performance.now(), lag = 0.035;
      const ease = (k) => (k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2);
      (function f(t) {
        const raw = (t - t0) / D;
        dots.forEach((c, i) => {
          const e = ease(Math.min(1, Math.max(0, raw - i * lag)));
          const pt = p.getPointAtLength(len * (opt.rev ? 1 - e : e));
          c.setAttribute("cx", pt.x); c.setAttribute("cy", pt.y);
        });
        if (!g.isConnected) return done(false);
        if (raw < 1 + TAIL * lag) requestAnimationFrame(f);
        else { g.remove(); ping(opt.rev ? a : b); done(true); }
      })(t0);
    });
  }

  if (!RM) setInterval(() => {
    if (!onSchema() || document.hidden || FX.tracing) return;
    const ps = [...document.querySelectorAll("#sx-wires path.sx-wire:not(.off)")];
    if (!ps.length) return;
    const [a, b] = ps[Math.floor(Math.random() * ps.length)].dataset.e.split("-");
    comet(a, b, { rev: Math.random() < 0.3, dur: 1200 + Math.random() * 900 });
  }, 520);

  async function trace() {
    if (FX.tracing) return;
    FX.tracing = true;
    const token = (FX.token = (FX.token || 0) + 1);
    const board = $("#sx-board"), cap = $("#fx-cap"), btn = $("#fx-trace");
    board && board.classList.add("fx-tracing");
    btn && (btn.classList.add("run"), (btn.querySelector("b").textContent = "Tracing…"));
    const live = STEPS.filter(([a, b]) => { const w = wire(a, b); return w && !w.classList.contains("off"); });
    ping("web");
    for (let i = 0; i < live.length; i++) {
      const [a, b, rev, txt] = live[i];
      if (!onSchema() || FX.token !== token) return;
      if (cap) {
        cap.classList.add("on");
        cap.innerHTML = '<span class="n">' + String(i + 1).padStart(2, "0") + " / " + String(live.length).padStart(2, "0") + '</span><span class="t">' + txt + '</span><span class="bar"><i style="width:' + ((i + 1) / live.length) * 100 + '%"></i></span>';
      }
      [a, b].forEach((id) => { const n = nodeEl(id); n && n.classList.add("fx-lit"); });
      const w = wire(a, b);
      w && w.classList.add("fx-trace");
      await comet(a, b, { rev: !!rev, dur: RM ? 300 : 950, trace: true });
      await new Promise((r) => setTimeout(r, RM ? 150 : 260));
    }
    if (cap) cap.innerHTML = '<span class="n">Done</span><span class="t">' + live.length + " hops, from your message to the streamed reply</span>";
    setTimeout(() => {
      if (FX.token !== token) return;
      document.querySelectorAll("#sx-board .fx-lit, #sx-wires .fx-trace").forEach((e) => e.classList.remove("fx-lit", "fx-trace"));
      board && board.classList.remove("fx-tracing");
      cap && cap.classList.remove("on");
      btn && (btn.classList.remove("run"), (btn.querySelector("b").textContent = "Trace a request"));
      FX.tracing = false;
    }, 2600);
  }

  function enhance() {
    const view = $("#schema-view"), board = $("#sx-board");
    if (!view || !board || board.dataset.fx) return;
    board.dataset.fx = "1";
    FX.tracing = false;
    FX.token = (FX.token || 0) + 1;

    const h1 = $(".vx-h1", view);
    if (h1 && !h1.dataset.fx) {
      h1.dataset.fx = "1";
      const words = h1.textContent.trim().split(" ");
      h1.innerHTML = words.map((w, i) => '<span class="fx-w" style="animation-delay:' + (80 + i * 90) + 'ms">' + w + "</span>").join(" ");
    }
    const ctrl = $(".sx-ctrl", view);
    if (ctrl && !$("#fx-trace", view)) {
      const b = document.createElement("button");
      b.type = "button"; b.id = "fx-trace"; b.className = "fx-trace-btn";
      b.innerHTML = '<svg viewBox="0 0 24 24" width="12" height="12"><path d="M7 4l13 8-13 8z" fill="currentColor"></path></svg><b>Trace a request</b>';
      b.addEventListener("click", trace);
      ctrl.appendChild(b);
    }
    const head = $(".ax-head", view);
    if (head && !$("#fx-cap", view)) {
      const c = document.createElement("div");
      c.id = "fx-cap"; c.className = "fx-cap"; c.setAttribute("aria-live", "polite");
      head.after(c);
    }
    const bg = document.createElement("div");
    bg.className = "fx-bg";
    bg.innerHTML = '<i class="g1"></i><i class="g2"></i><i class="grid"></i>';
    board.prepend(bg);

    const core = $('[data-n="core"]', board);
    if (core && !$(".fx-ring", core)) {
      const r = document.createElement("span");
      r.className = "fx-ring";
      core.prepend(r);
    }
    board.querySelectorAll(".ax-node").forEach((n, i) => {
      n.style.animationDelay = 120 + i * 45 + "ms";
      n.addEventListener("pointermove", (e) => {
        const r = n.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
        n.style.setProperty("--mx", x + "px");
        n.style.setProperty("--my", y + "px");
        if (!RM) n.style.transform = "perspective(800px) rotateX(" + ((y / r.height - 0.5) * -5).toFixed(2) + "deg) rotateY(" + ((x / r.width - 0.5) * 6).toFixed(2) + "deg) translateY(-2px)";
      });
      n.addEventListener("pointerleave", () => { n.style.transform = ""; n.style.setProperty("--mx", "-400px"); });
    });
    board.querySelectorAll(".ax-lbl").forEach((l, i) => (l.style.animationDelay = 60 + i * 70 + "ms"));
  }

  function boot() {
    const view = $("#schema-view");
    if (!view) return setTimeout(boot, 300);
    new MutationObserver(enhance).observe(view, { childList: true });
    enhance();
  }
  boot();
})();
