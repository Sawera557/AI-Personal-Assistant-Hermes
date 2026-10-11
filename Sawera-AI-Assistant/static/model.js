// Hermes agent model picker — Claude/OpenAI, cheap/expensive tier.
//
// This is a PLATFORM-WIDE setting, not per-browser: switching it here changes
// which model every conversation (any user, any device) uses on its next
// turn, via POST /api/settings/model. The <select> is pre-rendered by the
// server with the currently active model already selected.
(() => {
  const select = document.getElementById("model-select");
  const status = document.getElementById("model-select-status");
  if (!select) return;

  const showStatus = (text, isError) => {
    if (!status) return;
    status.textContent = text;
    status.hidden = false;
    status.style.color = isError ? "var(--danger, #d33)" : "";
    if (!isError) setTimeout(() => { status.hidden = true; }, 2500);
  };

  select.addEventListener("change", async () => {
    const model = select.value;
    const previous = select.dataset.current || model;
    select.disabled = true;
    try {
      const res = await fetch("/api/settings/model", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || "Could not switch model.");
      select.dataset.current = data.active || model;
      showStatus("Model switched for the whole platform.", false);
    } catch (err) {
      select.value = previous;
      showStatus(err.message || "Could not switch model.", true);
    } finally {
      select.disabled = false;
    }
  });

  select.dataset.current = select.value;
})();
