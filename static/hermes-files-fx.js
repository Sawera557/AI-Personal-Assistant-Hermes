/* Hermes v2 · File Explorer motion layer — header, stats, spotlight tilt, typed glow, transitions. */
(function () {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const RM = matchMedia("(prefers-reduced-motion: reduce)").matches;

  function header(page) {
    if ($(".ff-head", page)) return;
    const bg = document.createElement("div");
    bg.className = "ff-bg";
    bg.innerHTML = '<i class="g1"></i><i class="g2"></i><i class="grid"></i>';
    page.prepend(bg);
    const h = document.createElement("header");
    h.className = "ff-head";
    h.innerHTML = '<div><div class="ff-eyebrow">Drive · stored on S3</div><h1 class="ff-h1"><span style="animation-delay:80ms">File</span> <span style="animation-delay:170ms">Explorer</span></h1></div>' +
      '<div class="ff-stats" aria-live="polite"><div>Folders<b id="ff-n-folders">–</b></div><div>Files<b id="ff-n-files">–</b></div><div>In view<b id="ff-n-size">–</b></div></div>';
    bg.after(h);
  }

  const UNITS = { B: 1, KB: 1024, MB: 1024 ** 2, GB: 1024 ** 3 };
  function sizeOf(meta) {
    const m = /([\d.]+)\s*(B|KB|MB|GB)\b/i.exec(meta || "");
    return m ? parseFloat(m[1]) * (UNITS[m[2].toUpperCase()] || 1) : 0;
  }
  const human = (b) => { if (!b) return "0 B"; const u = ["B", "KB", "MB", "GB"]; let i = 0; while (b >= 1024 && i < 3) { b /= 1024; i++; } return (b >= 10 || i === 0 ? Math.round(b) : b.toFixed(1)) + " " + u[i]; };

  function count(el, to) {
    if (!el) return;
    const from = parseInt(el.textContent, 10) || 0;
    if (RM || from === to) { el.textContent = to; return; }
    const t0 = performance.now(), D = 600;
    (function f(t) { const k = Math.min(1, (t - t0) / D); el.textContent = Math.round(from + (to - from) * (1 - Math.pow(1 - k, 3))); if (k < 1) requestAnimationFrame(f); })(t0);
  }

  function decorate(content) {
    const items = [...content.querySelectorAll(".file-item")];
    const folders = items.filter((i) => i.classList.contains("folder")).length;
    count($("#ff-n-folders"), folders);
    count($("#ff-n-files"), items.length - folders);
    const sz = $("#ff-n-size");
    if (sz) sz.textContent = human(items.reduce((s, i) => s + sizeOf((i.querySelector(".file-item-meta") || {}).textContent), 0));
    items.forEach((it, i) => {
      if (it.dataset.ff) return;
      it.dataset.ff = "1";
      it.style.setProperty("--d", Math.min(i, 18) * 35 + "ms");
      const svg = it.querySelector(".file-item-icon svg, .file-icon");
      if (svg) it.style.setProperty("--tc", getComputedStyle(svg).color);
    });
  }

  function bind(content) {
    content.addEventListener("pointermove", (e) => {
      const it = e.target.closest(".file-item");
      if (!it) return;
      const r = it.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
      it.style.setProperty("--mx", x + "px");
      it.style.setProperty("--my", y + "px");
      if (!RM && content.classList.contains("tiles"))
        it.style.transform = "rotateX(" + ((y / r.height - 0.5) * -8).toFixed(2) + "deg) rotateY(" + ((x / r.width - 0.5) * 9).toFixed(2) + "deg) translateY(-3px)";
    });
    content.addEventListener("pointerout", (e) => {
      const it = e.target.closest(".file-item");
      if (it && !it.contains(e.relatedTarget)) { it.style.transform = ""; it.style.setProperty("--mx", "-400px"); }
    });
    content.addEventListener("pointerdown", (e) => {
      const it = e.target.closest(".file-item");
      if (!it || RM) return;
      const r = it.getBoundingClientRect(), d = document.createElement("span");
      d.className = "ff-ripple";
      d.style.left = e.clientX - r.left + "px"; d.style.top = e.clientY - r.top + "px";
      it.appendChild(d);
      setTimeout(() => d.remove(), 650);
      it.classList.remove("ff-open"); void it.offsetWidth; it.classList.add("ff-open");
    }, true);
  }

  const TOGGLES = ["#files-view-toggle", "#files-group-toggle"];
  function pill() {
    TOGGLES.forEach((sel) => {
      const tg = $(sel);
      if (!tg) return;
      let p = $(".ff-pill", tg);
      if (!p) { p = document.createElement("span"); p.className = "ff-pill"; tg.prepend(p); }
      const a = $("button.active", tg);
      if (!a) { p.style.width = "0"; return; }
      p.style.width = a.offsetWidth - 4 + "px";
      p.style.transform = "translateX(" + (a.offsetLeft + 2) + "px)";
    });
  }

  function boot() {
    const page = $("#files-view .files-page"), content = $("#files-content");
    if (!page || !content) return setTimeout(boot, 300);
    header(page);
    bind(content);
    let last = "";
    new MutationObserver(() => {
      const sig = content.children.length + ":" + (content.firstElementChild || {}).outerHTML?.slice(0, 80);
      if (sig !== last && content.querySelector(".file-item")) {
        last = sig;
        content.classList.remove("ff-swap"); void content.offsetWidth; content.classList.add("ff-swap");
      }
      decorate(content);
    }).observe(content, { childList: true });
    TOGGLES.forEach((sel) => {
      const tg = $(sel);
      if (tg) new MutationObserver(pill).observe(tg, { attributes: true, subtree: true, attributeFilter: ["class"] });
    });
    new MutationObserver(() => requestAnimationFrame(pill)).observe($("#app") || document.body, { attributes: true, attributeFilter: ["data-view"] });
    addEventListener("resize", pill);
    decorate(content);
    setTimeout(pill, 200);
  }
  boot();
})();
