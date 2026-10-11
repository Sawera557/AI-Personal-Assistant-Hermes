/* Hermes v2 · App-wide motion theme — backdrops, title reveals, card stagger, tilt, ripple. */
(function () {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const RM = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const VIEWS = {
    home: ["#home-view"], agent: ["main.chat"], bills: ["#bills-view", "Billing · register & invoices"], meeting: ["#meeting-view", "Meetings · briefs & notes"],
    context: ["#context-view", "Knowledge · files, memories, skills"], tasks: ["#tasks-view", "Automations · on a schedule"], jobs: ["#jobs-view", "Pipelines · background work"],
    artefacts: ["#artefacts-view", "Library · live views & files"], pricing: ["#pricing-view", "Spend · tokens & cost"], settings: ["#settings-view", "Safety · what Hermes may do"],
  };
  const CARDS = ".tk-card,.bstat,.usage-stat,.guardrail-row,.context-item,.meeting-box,.reg-proposal,.reg-row,.meeting-upcoming-row,.lib-card,.art-card,.job-card";
  const TILT = ".tk-card,.bstat,.usage-stat,.context-item,.meeting-box,.reg-proposal,.hud-mod,.lib-card,.art-card,.job-card";
  const TITLES = ".tasks-pg-title,.bills-pg-title,.context-pg-header .bills-panel-title";

  function split(t) {
    if (t.dataset.tf || t.children.length) return;
    t.dataset.tf = "1";
    t.innerHTML = t.textContent.trim().split(/\s+/).map((w, i) => '<span class="tf-w" style="animation-delay:' + (80 + i * 90) + 'ms">' + w.replace(/&/g, "&amp;").replace(/</g, "&lt;") + "</span>").join(" ");
  }

  function prep(main, eyebrow) {
    if (!main.dataset.tf) {
      main.dataset.tf = "1";
      const bg = document.createElement("div");
      bg.className = "tf-bg";
      bg.innerHTML = '<i class="g1"></i><i class="g2"></i><i class="grid"></i>';
      main.prepend(bg);
    }
    main.querySelectorAll(TITLES).forEach((t) => {
      split(t);
      const box = t.parentElement;
      if (eyebrow && box && !box.dataset.tfEyebrow) box.dataset.tfEyebrow = eyebrow;
    });
  }

  function stagger(main) {
    let i = 0;
    main.querySelectorAll(CARDS).forEach((c) => { if (c.offsetParent) c.style.setProperty("--tf-d", Math.min(i++, 16) * 40 + "ms"); });
  }

  function enter(view) {
    const cfg = VIEWS[view];
    if (!cfg) return;
    const main = $(cfg[0]);
    if (!main) return;
    prep(main, cfg[1]);
    if (view === "home") return;
    stagger(main);
    main.classList.remove("tf-enter"); void main.offsetWidth; main.classList.add("tf-enter");
    if (!main.dataset.tfObs) {
      main.dataset.tfObs = "1";
      let q = 0;
      new MutationObserver(() => { if (!q) q = requestAnimationFrame(() => { q = 0; stagger(main); main.querySelectorAll(TITLES).forEach(split); }); }).observe(main, { childList: true, subtree: true });
    }
  }

  document.addEventListener("pointermove", (e) => {
    const el = e.target.closest && e.target.closest(TILT);
    if (!el || RM || el.closest(".schema-view, .files-view")) return;
    const r = el.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
    el.style.setProperty("--mx", x + "px"); el.style.setProperty("--my", y + "px");
    const k = r.width > 520 ? 0.35 : 1;
    el.style.transform = "perspective(900px) rotateX(" + ((y / r.height - 0.5) * -5 * k).toFixed(2) + "deg) rotateY(" + ((x / r.width - 0.5) * 6 * k).toFixed(2) + "deg) translateY(-2px)";
  }, { passive: true });
  document.addEventListener("pointerout", (e) => {
    const el = e.target.closest && e.target.closest(TILT);
    if (el && !el.contains(e.relatedTarget)) el.style.transform = "";
  });
  document.addEventListener("pointerdown", (e) => {
    const el = e.target.closest && e.target.closest(CARDS + ",.hud-mod");
    if (!el || RM || el.closest(".schema-view, .files-view")) return;
    const r = el.getBoundingClientRect(), d = document.createElement("span");
    d.className = "tf-ripple";
    d.style.left = e.clientX - r.left + "px"; d.style.top = e.clientY - r.top + "px";
    let w = el.querySelector(":scope > .tf-rip-wrap");
    if (!w) { w = document.createElement("span"); w.className = "tf-rip-wrap"; el.appendChild(w); }
    w.appendChild(d);
    setTimeout(() => d.remove(), 650);
    el.classList.remove("tf-ping"); void el.offsetWidth; el.classList.add("tf-ping");
    setTimeout(() => el.classList.remove("tf-ping"), 450);
  }, true);

  function boot() {
    const app = $("#app");
    if (!app) return setTimeout(boot, 300);
    Object.keys(VIEWS).forEach((v) => { const m = $(VIEWS[v][0]); m && prep(m, VIEWS[v][1]); });
    let last = "";
    const sync = () => { const v = app.dataset.view; if (v !== last) { last = v; requestAnimationFrame(() => enter(v)); } };
    new MutationObserver(sync).observe(app, { attributes: true, attributeFilter: ["data-view"] });
    sync();
  }
  boot();
})();
