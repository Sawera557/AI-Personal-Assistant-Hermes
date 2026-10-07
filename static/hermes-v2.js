/* Hermes v2 — UX review fixes (behaviour). Runs after all view scripts. */
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const isMac = /Mac|iPhone|iPad/i.test(navigator.platform || navigator.userAgent);

  const APP_NAMES = {
    "google-mail": "Gmail", "google-drive": "Google Drive", "google-sheets": "Google Sheets", "google-docs": "Google Docs",
    "google-calendar": "Google Calendar", "google-forms": "Google Forms", "google-meet": "Google Meet", "google-contacts": "Google Contacts",
    quickbooks: "QuickBooks", "pdf-co": "PDF.co", fathom: "Fathom", wati: "WhatsApp", notion: "Notion", zoom: "Zoom", "zoom-admin": "Zoom Admin", jira: "Jira",
    github: "GitHub", twilio: "Twilio", firecrawl: "Firecrawl", fireflies: "Fireflies",
  };

  function shortcutLabels() {
    if (isMac) return;
    $$(".hx-kbd").forEach((b) => { if (b.innerHTML.includes("⌘K")) b.innerHTML = b.innerHTML.replace("⌘K", "Ctrl&nbsp;K"); });
  }

  function appNames() {
    $$(".hud-conn span, .apps li, .hx-node span").forEach((el) => {
      const t = el.textContent.trim();
      if (APP_NAMES[t]) { el.textContent = APP_NAMES[t]; el.title = t; }
    });
  }

  function syncErrors() {
    $$(".af-err-chip:not([data-v2])").forEach((el) => {
      el.dataset.v2 = "1";
      const raw = (el.getAttribute("title") || el.textContent).replace(/^⚠\s*/, "");
      let msg = "The last sync didn't finish. Try Sync again.";
      if (/google sheets/i.test(raw) && /range/i.test(raw)) msg = "Hermes couldn't read the Google Sheet. The sheet or range may have been renamed or moved.";
      else if (/401|403|auth|token/i.test(raw)) msg = "The connected app needs to be re-authorised.";
      el.innerHTML = `<b>Sync failed.</b>${esc(msg)}<span class="v2-err-raw">${esc(raw)}</span>`;
    });
  }

  function starters() {
    const box = $("#v2-starters");
    if (!box) return;
    if (!box.dataset.v2) {
      box.dataset.v2 = "1";
      box.addEventListener("click", (e) => {
        const b = e.target.closest(".v2-starter");
        const input = $("#input"), form = $("#composer");
        if (!b || !input || !form) return;
        input.value = b.textContent.trim();
        input.dispatchEvent(new Event("input", { bubbles: true }));
        form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
      });
    }
    const hide = $$("#messages .msg.user").length > 0;
    if (box.hidden !== hide) box.hidden = hide;
  }

  function agentBar() {
    const ab = $("#agent-bar");
    if (!ab || ab.dataset.v2) return;
    ab.dataset.v2 = "1";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "v2-ab-more";
    btn.setAttribute("aria-expanded", "false");
    ab.insertBefore(btn, ab.querySelector(".ab-status"));
    const update = () => {
      const c = $("#context-toggle-input")?.checked, a = $("#apps-toggle-input")?.checked;
      const open = ab.classList.contains("v2-open");
      btn.innerHTML = `<span>${open ? "Fewer options" : "More options"}</span>` +
        (open ? "" : `<span class="v2-ab-sum">Context ${c ? "on" : "off"} · Apps ${a ? "on" : "off"}</span>`);
      btn.classList.toggle("warn", !c || !a);
    };
    btn.addEventListener("click", () => { ab.classList.toggle("v2-open"); btn.setAttribute("aria-expanded", ab.classList.contains("v2-open")); update(); });
    ab.addEventListener("change", update);
    update();
  }

  function rail() {
    const st = $('.strip-tab[data-view="settings"]'), tg = $("#strip-toggle");
    if (!st || !tg || st.dataset.v2) return;
    st.dataset.v2 = "1";
    const foot = document.createElement("div");
    foot.className = "v2-strip-foot";
    tg.before(foot);
    foot.append(st);
  }

  function tzNote() {
    const el = $("#v2-tz");
    if (!el) return;
    let tz = "";
    try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone.replace(/_/g, " "); } catch (e) {}
    el.textContent = `“Next” times are shown in your local time${tz ? ` (${tz})` : ""}. Each schedule runs in its own time zone.`;
  }

  function meetingPick() {
    const b = $("#v2-meeting-pick");
    if (!b) return;
    b.addEventListener("click", () => {
      const s = $("#hermes-biz-select-meeting");
      if (!s) return;
      s.focus();
      try { s.showPicker && s.showPicker(); } catch (e) {}
    });
  }

  function crumbs() {
    $$("#files-breadcrumb *").forEach((el) => {
      if (!el.children.length && el.textContent.trim() === "Home") el.textContent = "All files";
    });
  }

  const EMOJI = /^(?:\p{Extended_Pictographic}|\p{Emoji_Presentation})(?:\uFE0F|\u200D(?:\p{Extended_Pictographic}))*\s*/u;
  function stripEmoji() {
    $$(".tk-chip, .tk-group-head, #context-upload-btn, #context-list li *, #skill-list li *, .lib-subtab, .tk-run-trig").forEach((el) => {
      el.childNodes.forEach((n) => {
        if (n.nodeType === 3 && EMOJI.test(n.nodeValue.trimStart())) n.nodeValue = n.nodeValue.trimStart().replace(EMOJI, "");
      });
    });
  }

  function billing() {
    const ov = $("#bill-overview");
    if (ov) {
      const n = (id) => parseInt(($(id) || {}).textContent || "0", 10) || 0;
      const empty = n("#reg-count-lines") === 0 && n("#reg-count-docs") === 0;
      let card = $("#v2-bill-empty");
      if (empty && !card && !ov.hidden && ov.children.length) {
        card = document.createElement("div");
        card.id = "v2-bill-empty";
        card.className = "v2-bill-empty";
        card.innerHTML = `<div><b>No bills yet</b><p>Upload a bill or connect your Google Sheet register. Hermes reads the amount, due date and vendor, and fills in this overview.</p></div><label class="v2-btn" for="bill-file-input">Upload bills</label>`;
        ov.prepend(card);
      } else if (!empty && card) card.remove();
    }
    $$("#reg-list *").forEach((el) => {
      if (!el.children.length && el.textContent.trim() === "No lines match." && !$("#reg-toolbar input")?.value) el.textContent = "No bills in the register yet.";
    });
  }

  function money() {
    $$("#pricing-totals *").forEach((el) => {
      if (!el.children.length && /^\$0\.0+$/.test(el.textContent.trim())) el.textContent = "$0.00";
    });
  }

  function orbLink() {
    const face = $(".hud-core-face"), rings = $("#hud-rings");
    if (!face || !rings || rings.dataset.v2) return;
    rings.dataset.v2 = "1";
    const go = () => {
      const t = $('.strip-tab[data-view="agent"]');
      if (t) t.click();
      setTimeout(() => { const i = $("#input"); if (i) i.focus(); }, 250);
    };
    [face, rings].forEach((el) => { el.style.cursor = "pointer"; });
    face.setAttribute("role", "button");
    face.setAttribute("tabindex", "0");
    face.setAttribute("aria-label", "Open Agent chat");
    face.title = "Open Agent chat";
    rings.addEventListener("click", (e) => {
      const r = rings.getBoundingClientRect();
      const dx = e.clientX - (r.left + r.width / 2), dy = e.clientY - (r.top + r.height / 2);
      if (Math.hypot(dx, dy) <= r.width * 0.5) go();
    });
    face.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
  }

  shortcutLabels(); rail(); agentBar(); tzNote(); meetingPick(); orbLink();
  let queued = 0;
  const run = () => { queued = 0; appNames(); syncErrors(); starters(); crumbs(); shortcutLabels(); stripEmoji(); billing(); money(); };
  run();
  new MutationObserver(() => { if (!queued) queued = requestAnimationFrame(run); })
    .observe(document.body, { childList: true, subtree: true, characterData: true });
})();

/* Top bar — skin switch (remembered), live API status, palette / replay / present.
   Palette and replay go through the message hooks hermes-fx.js listens for. */
(() => {
  const bar = document.getElementById("hx-topbar");
  if (!bar) return;
  const root = document.documentElement;
  const $ = (id) => document.getElementById(id);

  const skins = bar.querySelectorAll("[data-skin]");
  const paint = () => skins.forEach((b) => b.setAttribute("aria-pressed", String((b.dataset.skin === "holo") === root.classList.contains("holo"))));
  skins.forEach((b) => b.addEventListener("click", () => {
    root.classList.toggle("holo", b.dataset.skin === "holo");
    localStorage.setItem("hermes.skin", b.dataset.skin);
    paint();
  }));
  paint();

  if (/Mac|iPhone|iPad/i.test(navigator.platform || navigator.userAgent)) bar.querySelector(".hx-tb-kbd").textContent = "⌘K";
  const send = (msg) => window.postMessage(msg, location.origin);
  $("hx-tb-palette").addEventListener("click", () => send({ hermesPalette: true }));
  $("hx-tb-replay").addEventListener("click", () => send({ hermesBoot: true }));
  $("hx-tb-present").addEventListener("click", () => {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else if (root.requestFullscreen) root.requestFullscreen().catch(() => {});
  });

  const data = $("hx-tb-data"), status = $("hx-tb-status");
  const STATES = {
    live: ["Connected", "Live · Hermes API is healthy"],
    degraded: ["Degraded", "Live · Hermes API is up, but some checks are failing — see Settings"],
    down: ["Unreachable", "Can't reach the Hermes API"],
  };
  const show = (s) => { data.dataset.state = s; status.textContent = STATES[s][0]; data.title = STATES[s][1]; };
  const check = () => fetch("/api/health")
    .then((r) => (r.ok ? r.json() : Promise.reject()))
    .then((h) => show(h.status === "ok" ? "live" : "degraded"))
    .catch(() => show("down"));
  check();
  setInterval(check, 60000);
})();
