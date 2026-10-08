/* Hermes v2 · URL routing — each tab gets its own path (/agent, /files, /billing …). */
(function () {
  "use strict";
  const ROUTES = {
    home: "", agent: "agent", schema: "system", bills: "billing", meeting: "meetings", context: "context",
    tasks: "tasks", multiagents: "multi-agents", files: "files", jobs: "jobs", artefacts: "artifacts", pricing: "usage", settings: "settings",
  };
  const BY_SLUG = Object.fromEntries(Object.entries(ROUTES).map(([v, s]) => [s, v]));
  ["artefacts", "pricing", "bills", "schema", "meeting"].forEach((v) => (BY_SLUG[v] = v));

  // Served as a static .html file (preview) → "#/agent". Served by the app at "/" → real paths "/agent".
  const FILE_MODE = /\.html?$/i.test(location.pathname);
  const BASE = FILE_MODE ? "" : (document.querySelector("base")?.getAttribute("href") || "/").replace(/\/?$/, "/");

  const current = () => {
    const raw = FILE_MODE ? location.hash.replace(/^#\/?/, "") : location.pathname.slice(BASE.length);
    return BY_SLUG[raw.split(/[/?#]/)[0].toLowerCase()] || (raw ? null : "home");
  };
  const urlFor = (v) => {
    const s = ROUTES[v] ?? v;
    return FILE_MODE ? location.pathname + location.search + (s ? "#/" + s : "#/") : BASE + s + location.search;
  };
  const go = (v) => {
    if (typeof window.setView === "function") window.setView(v);
    else document.querySelector('.strip-tab[data-view="' + v + '"]')?.click();
  };

  let applying = false;
  const title = (v) => { document.title = "Hermes · " + (document.querySelector('.strip-tab[data-view="' + v + '"] span')?.textContent.trim() || v); };
  function boot() {
    const app = document.getElementById("app");
    if (!app) return setTimeout(boot, 200);
    const start = () => {
      const want = current();
      if (want && want !== app.dataset.view) { applying = true; go(want); applying = false; }
      history.replaceState({ view: app.dataset.view }, "", urlFor(app.dataset.view));
      new MutationObserver(() => {
        const v = app.dataset.view;
        if (!v || applying || current() === v) return;
        history.pushState({ view: v }, "", urlFor(v));
        title(v);
      }).observe(app, { attributes: true, attributeFilter: ["data-view"] });
    };
    // wait for the Home boot sequence to finish so it doesn't override the deep link
    const waitBoot = () => (document.querySelector(".hx-boot") ? setTimeout(waitBoot, 150) : setTimeout(start, 50));
    waitBoot();
  }
  addEventListener("popstate", () => {
    const v = current();
    const app = document.getElementById("app");
    if (v && app && v !== app.dataset.view) { applying = true; go(v); applying = false; }
    if (v) title(v);
  });
  if (FILE_MODE) addEventListener("hashchange", () => { const v = current(); if (v) go(v); });
  boot();
})();
