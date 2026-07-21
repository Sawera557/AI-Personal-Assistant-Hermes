// Boot sequence — plays once per browser tab session, always skippable.
// Purely decorative: gated by sessionStorage so reloads/nav within the same
// tab don't replay it, but a fresh tab always sees it once.
(() => {
  const overlay = document.getElementById("boot-overlay");
  if (!overlay) return;

  const SESSION_KEY = "hermesBooted";
  if (sessionStorage.getItem(SESSION_KEY)) return; // already booted this session

  const LINES = [
    "INITIALIZING HERMES INTELLIGENCE CORE …",
    "LOADING CLAUDE REASONING ENGINE … OK",
    "MOUNTING MATON CONNECTOR GATEWAY … OK",
    "MEMORY LINK · POSTGRES … OK",
    "VECTOR INDEX · PINECONE … STANDBY",
    "VOICE SUBSYSTEM · STT / TTS … STANDBY",
    "SCANNING DATA SOURCES … AWAITING UPLINK",
    "HERMES ONLINE. GOOD TO SEE YOU.",
  ];

  const linesEl = document.getElementById("boot-lines");
  const barEl = document.getElementById("boot-bar-fill");
  const skipBtn = document.getElementById("boot-skip");

  overlay.hidden = false;

  let step = 0;
  let timer = null;

  function render() {
    if (linesEl) {
      linesEl.innerHTML = LINES.slice(0, step)
        .map((l) => `<div class="boot-line">${l}</div>`)
        .join("");
    }
    if (barEl) barEl.style.width = Math.min(100, Math.round((step / LINES.length) * 100)) + "%";
  }

  function finish() {
    clearInterval(timer);
    sessionStorage.setItem(SESSION_KEY, "1");
    overlay.classList.add("boot-done");
    setTimeout(() => { overlay.hidden = true; }, 350);
  }

  timer = setInterval(() => {
    step += 1;
    render();
    if (step >= LINES.length) {
      clearInterval(timer);
      setTimeout(finish, 550);
    }
  }, 420);
  render();

  skipBtn?.addEventListener("click", finish);
})();
