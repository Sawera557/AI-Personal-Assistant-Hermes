// Scheduled Tasks — Claude-style recurring, unattended agent runs.
//
// Lists tasks, a create/edit modal with a schedule builder + app picker +
// autonomy control, and a detail modal with run history and "Run now".
// Talks to /api/scheduled-tasks/*. Business scope mirrors the rest of the app
// via HermesBiz, but each task also carries its OWN business (set in the editor)
// so the sidebar scope only filters the list — it doesn't retarget a task.
(function () {
  "use strict";

  // Apps a task can be restricted to (Maton slugs + the "web" pseudo-app).
  const APPS = [
    { slug: "google-mail", label: "Gmail", emoji: "📧" },
    { slug: "google-calendar", label: "Calendar", emoji: "📅" },
    { slug: "google-drive", label: "Drive", emoji: "📁" },
    { slug: "google-sheets", label: "Sheets", emoji: "📊" },
    { slug: "google-docs", label: "Docs", emoji: "📄" },
    { slug: "quickbooks", label: "QuickBooks", emoji: "💰" },
    { slug: "wati", label: "WhatsApp", emoji: "💬" },
    { slug: "notion", label: "Notion", emoji: "📝" },
    { slug: "jira", label: "Jira", emoji: "🧩" },
    { slug: "zoom", label: "Zoom", emoji: "🎥" },
    { slug: "web", label: "Web search", emoji: "🌐" },
  ];

  // Quick-start templates. {biz} is swapped for the selected business name.
  const TEMPLATES = [
    {
      icon: "🌅", title: "Daily briefing",
      name: "Daily morning briefing",
      prompt:
        "Scan this business's Gmail and Calendar for today and write me a short briefing: what's urgent, what's coming up, and any bills or deadlines due. Lead with what needs my attention first.",
      apps: ["google-mail", "google-calendar"], autonomy: "readonly",
      schedule: { frequency: "daily", times: ["07:00"], days_of_week: [] },
    },
    {
      icon: "📥", title: "Inbox triage",
      name: "Weekday inbox triage",
      prompt:
        "Review new emails from the last day, group them by urgency, and prepare draft replies to anything that clearly needs one. Summarize what you drafted and what still needs my decision.",
      apps: ["google-mail"], autonomy: "draft",
      schedule: { frequency: "weekdays", times: ["08:00"], days_of_week: [] },
    },
    {
      icon: "💰", title: "Arrears sweep",
      name: "Weekly rent arrears sweep",
      prompt:
        "Pull the AR aging from QuickBooks and list who is overdue, how much, and how many days late. Cross-check recent Gmail for any payment promises, and flag who to follow up with first.",
      apps: ["quickbooks", "google-mail"], autonomy: "readonly",
      schedule: { frequency: "weekly", times: ["09:00"], days_of_week: [0] },
    },
    {
      icon: "📊", title: "Weekly review",
      name: "Friday weekly review",
      prompt:
        "Give me a Friday wrap-up for this business: cash position and arrears from QuickBooks, this week's meetings and next week's calendar, and any open items from email that need attention. End with the top 3 things to handle next week.",
      apps: ["google-mail", "google-calendar", "quickbooks"], autonomy: "readonly",
      schedule: { frequency: "weekly", times: ["16:00"], days_of_week: [4] },
    },
  ];

  const DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  let inited = false;
  let editingId = null;              // task_id when editing, null when creating
  let detailTask = null;             // task currently open in the detail modal
  // Schedule builder working state.
  let sb = { frequency: "daily", times: ["07:00"], days: [] };

  const $ = (id) => document.getElementById(id);
  const esc = (s) =>
    (typeof escapeHtml === "function"
      ? escapeHtml(String(s == null ? "" : s))
      : String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
          ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])));

  function md(text) {
    if (typeof renderMarkdown === "function") return renderMarkdown(text);
    return esc(text).replace(/\n/g, "<br>");
  }

  function fmtWhen(iso) {
    if (!iso) return "—";
    try {
      return new Date(iso).toLocaleString([], {
        month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
      });
    } catch (_) { return iso; }
  }

  function fmtTime12(hhmm) {
    const [h, m] = String(hhmm).split(":").map(Number);
    const suf = h < 12 ? "AM" : "PM";
    return `${(h % 12) || 12}:${String(m).padStart(2, "0")} ${suf}`;
  }

  function fmtBytes(n) {
    if (!n && n !== 0) return "";
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  }

  // Client-side mirror of scheduler.describe_schedule, for the live preview.
  function describeSchedule(s) {
    if (s.frequency === "hourly") {
      const m = (s.times[0] || "00:00").split(":")[1] || "00";
      return `Every hour (at :${m})`;
    }
    const times = (s.times.length ? s.times : ["09:00"]).slice().sort();
    const timeStr = times.map(fmtTime12).join(", ");
    if (s.frequency === "weekdays") return `Weekdays at ${timeStr}`;
    if (s.frequency === "weekly") {
      const days = s.days.slice().sort((a, b) => a - b);
      const when = days.length ? days.map((d) => DAY_LABELS[d]).join(", ") : "Weekly";
      return `${when} at ${timeStr}`;
    }
    if (times.length > 1) return `${times.length} times a day · ${timeStr}`;
    return `Every day at ${timeStr}`;
  }

  // ── Task list ────────────────────────────────────────────────────────────
  async function loadTasks() {
    const listEl = $("tasks-list");
    const emptyEl = $("tasks-empty");
    if (!listEl) return;
    listEl.innerHTML = `<p class="hint" style="padding:8px">Loading tasks…</p>`;
    let tasks = [];
    try {
      const r = await fetch(`/api/scheduled-tasks?entity_id=${encodeURIComponent(HermesBiz.current)}`);
      const j = await r.json();
      tasks = j.tasks || [];
    } catch (e) {
      listEl.innerHTML = `<p class="hint" style="padding:8px">Couldn't load tasks: ${esc(e)}</p>`;
      return;
    }
    if (!tasks.length) {
      listEl.innerHTML = "";
      if (emptyEl) emptyEl.hidden = false;
      return;
    }
    if (emptyEl) emptyEl.hidden = true;
    // In the "All Businesses" view, separate tasks under a heading per business
    // (with an "All businesses" section for cross-business, unscoped tasks). When
    // a single business is selected the list is already scoped, so render flat.
    listEl.innerHTML = HermesBiz.current === "all"
      ? groupedTasksHtml(tasks)
      : tasks.map(taskCard).join("");
  }

  // Group tasks by business into labelled sections. Unscoped tasks (business
  // === null, i.e. "run across all businesses") get their own "All businesses"
  // section. Sections follow the business-selector order; the count is shown.
  function groupedTasksHtml(tasks) {
    const byBiz = new Map();
    for (const t of tasks) {
      const key = t.business || "all";
      if (!byBiz.has(key)) byBiz.set(key, []);
      byBiz.get(key).push(t);
    }
    const order = HermesBiz.list.map((b) => b.slug);
    const rank = (k) => { const i = order.indexOf(k); return i < 0 ? 999 : i; };
    return [...byBiz.keys()]
      .sort((a, b) => rank(a) - rank(b))
      .map((key) => {
        const b = key === "all" ? null : HermesBiz.find(key);
        const label = key === "all"
          ? "🏢 All businesses"
          : `${(b && b.emoji) || "🏢"} ${esc((b && b.name) || key)}`;
        const group = byBiz.get(key);
        return `<div class="tk-group-head">${label}` +
               `<span class="tk-group-count">${group.length}</span></div>` +
               group.map(taskCard).join("");
      })
      .join("");
  }

  function bizChip(slug) {
    if (!slug) return `<span class="tk-chip tk-chip-all">🏢 All businesses</span>`;
    const b = HermesBiz.find(slug);
    const name = b ? b.name : slug;
    const emoji = b ? (b.emoji || "🏢") : "🏢";
    return `<span class="tk-chip">${emoji} ${esc(name)}</span>`;
  }

  const AUTONOMY_CHIP = {
    readonly: `<span class="tk-chip tk-chip-ro">👁 Read-only</span>`,
    draft: `<span class="tk-chip tk-chip-draft">✎ Draft</span>`,
    full: `<span class="tk-chip tk-chip-full">⚡ Full</span>`,
  };

  function statusDot(status) {
    const map = { success: "ok", error: "err", running: "run" };
    return `<span class="tk-status-dot ${map[status] || "idle"}"></span>`;
  }

  function taskCard(t) {
    const disabled = !t.enabled ? " tk-card-off" : "";
    const next = t.enabled
      ? `<span class="tk-next">Next: ${esc(fmtWhen(t.next_run_at))}</span>`
      : `<span class="tk-next tk-paused">Paused</span>`;
    const last = t.last_run_at
      ? `<span class="tk-last">${statusDot(t.last_status)}Last run ${esc(fmtWhen(t.last_run_at))}</span>`
      : `<span class="tk-last">${statusDot()}Never run</span>`;
    return `
      <div class="tk-card${disabled}" data-id="${esc(t.task_id)}">
        <div class="tk-card-main" data-open="${esc(t.task_id)}">
          <div class="tk-card-top">
            <div class="tk-card-name">${esc(t.name)}</div>
            <label class="tk-toggle" title="${t.enabled ? "Enabled" : "Paused"}">
              <input type="checkbox" ${t.enabled ? "checked" : ""} data-toggle="${esc(t.task_id)}">
              <span class="tk-toggle-track"></span>
            </label>
          </div>
          <div class="tk-card-sched">⏱ ${esc(t.schedule_summary || "")}</div>
          ${t.window_summary ? `<div class="tk-card-window">📆 ${esc(t.window_summary)}</div>` : ""}
          <div class="tk-card-chips">${bizChip(t.business)}${AUTONOMY_CHIP[t.autonomy] || ""}</div>
          <div class="tk-card-foot">${next}${last}</div>
        </div>
      </div>`;
  }

  // ── Editor modal ─────────────────────────────────────────────────────────
  function renderTemplates() {
    const wrap = $("tk-templates");
    if (!wrap) return;
    wrap.innerHTML =
      `<div class="tk-templates-label">Start from a template</div>` +
      `<div class="tk-templates-grid">` +
      TEMPLATES.map((t, i) =>
        `<button type="button" class="tk-template" data-tpl="${i}">
           <span class="tk-template-icon">${t.icon}</span>
           <span class="tk-template-title">${esc(t.title)}</span>
         </button>`).join("") +
      `</div>`;
    wrap.querySelectorAll(".tk-template").forEach((btn) =>
      btn.addEventListener("click", () => applyTemplate(TEMPLATES[+btn.dataset.tpl])));
  }

  function applyTemplate(tpl) {
    $("tk-name").value = tpl.name;
    $("tk-prompt").value = tpl.prompt;
    sb = {
      frequency: tpl.schedule.frequency,
      times: tpl.schedule.times.slice(),
      days: (tpl.schedule.days_of_week || []).slice(),
    };
    setSelectedApps(tpl.apps || []);
    const radio = document.querySelector(`input[name="tk-autonomy"][value="${tpl.autonomy}"]`);
    if (radio) radio.checked = true;
    syncScheduleUI();
  }

  function renderApps() {
    const wrap = $("tk-apps");
    if (!wrap) return;
    wrap.innerHTML = APPS.map((a) =>
      `<label class="tk-app" data-app="${a.slug}">
         <input type="checkbox" value="${a.slug}">
         <span>${a.emoji} ${esc(a.label)}</span>
       </label>`).join("");
    wrap.querySelectorAll(".tk-app input").forEach((cb) =>
      cb.addEventListener("change", () => cb.closest(".tk-app").classList.toggle("on", cb.checked)));
  }

  function setSelectedApps(slugs) {
    const set = new Set(slugs || []);
    document.querySelectorAll("#tk-apps .tk-app").forEach((el) => {
      const cb = el.querySelector("input");
      cb.checked = set.has(cb.value);
      el.classList.toggle("on", cb.checked);
    });
  }

  function getSelectedApps() {
    return Array.from(document.querySelectorAll("#tk-apps .tk-app input:checked")).map((c) => c.value);
  }

  function populateBusinessSelect() {
    const sel = $("tk-business");
    if (!sel) return;
    sel.innerHTML = HermesBiz.list
      .map((b) => `<option value="${b.slug}">${b.emoji || "🏢"} ${esc(b.name)}</option>`)
      .join("");
  }

  // Schedule builder UI sync.
  function syncScheduleUI() {
    document.querySelectorAll("#tk-freq-tabs .tk-freq").forEach((b) =>
      b.classList.toggle("active", b.dataset.freq === sb.frequency));
    $("tk-days").hidden = sb.frequency !== "weekly";
    document.querySelectorAll("#tk-days .tk-day").forEach((b) =>
      b.classList.toggle("on", sb.days.includes(+b.dataset.day)));
    // Hourly uses only the minute of a single time; show a simpler label.
    const timesWrap = $("tk-times-wrap");
    $("tk-times-label").textContent = sb.frequency === "hourly" ? "At this minute past each hour" : "At these times";
    renderTimes();
    $("tk-sched-preview").textContent = describeSchedule(sb);
    timesWrap.hidden = false;
  }

  function renderTimes() {
    const wrap = $("tk-times");
    if (!wrap) return;
    const times = sb.frequency === "hourly" ? sb.times.slice(0, 1) : sb.times;
    if (!times.length) { wrap.innerHTML = `<span class="hint">No times yet — add one.</span>`; return; }
    wrap.innerHTML = times
      .slice().sort()
      .map((t) => `<span class="tk-time-chip">${fmtTime12(t)}<button type="button" data-rmtime="${t}">✕</button></span>`)
      .join("");
    wrap.querySelectorAll("[data-rmtime]").forEach((btn) =>
      btn.addEventListener("click", () => {
        sb.times = sb.times.filter((x) => x !== btn.dataset.rmtime);
        syncScheduleUI();
      }));
  }

  // ── Backfill / date-window UI ──────────────────────────────────────────────
  function windowPace() {
    return (document.querySelector('input[name="tk-window-pace"]:checked') || {}).value || "catchup";
  }

  function describeWindowClient() {
    const start = $("tk-window-start").value;
    const end = $("tk-window-end").value;
    const step = Math.max(1, parseInt($("tk-window-step").value, 10) || 1);
    if (!start) return "Pick a start date.";
    const per = step === 1 ? "1 day/run" : `${step} days/run`;
    let days = "";
    if (end) {
      const n = Math.floor((new Date(end) - new Date(start)) / 86400000);
      if (n < 0) return "End date is before the start date.";
      days = ` · ${Math.floor(n / step) + 1} run(s)`;
    } else {
      days = " · open-ended";
    }
    const label = windowPace() === "catchup" ? "Backfill" : "Windowed";
    return `${label} ${start}${end ? " → " + end : ""} · ${per}${days}`;
  }

  function syncWindowUI() {
    const on = $("tk-window-on").checked;
    $("tk-window").hidden = !on;
    // A catchup backfill needs an end date; nudge the field.
    $("tk-window-end").required = on && windowPace() === "catchup";
    $("tk-window-preview").textContent = on ? describeWindowClient() : "";
  }

  function openEditor(task) {
    editingId = task ? task.task_id : null;
    $("tk-editor-title").textContent = task ? "Edit scheduled task" : "New scheduled task";
    $("tk-editor-msg").textContent = "";
    populateBusinessSelect();
    if (task) {
      $("tk-name").value = task.name || "";
      $("tk-prompt").value = task.prompt || "";
      $("tk-business").value = task.business || "all";
      $("tk-timezone").value = task.timezone || "America/Chicago";
      $("tk-enabled").checked = !!task.enabled;
      const s = task.schedule || {};
      sb = {
        frequency: s.frequency || "daily",
        times: (s.times && s.times.length ? s.times : ["07:00"]).slice(),
        days: (s.days_of_week || []).slice(),
      };
      setSelectedApps(task.allowed_apps || []);
      const radio = document.querySelector(`input[name="tk-autonomy"][value="${task.autonomy || "readonly"}"]`);
      if (radio) radio.checked = true;
      // Backfill window
      const hasWin = !!task.window_pace;
      $("tk-window-on").checked = hasWin;
      $("tk-window-start").value = task.window_start || "";
      $("tk-window-end").value = task.window_end || "";
      $("tk-window-step").value = task.window_step || 1;
      const pr = document.querySelector(`input[name="tk-window-pace"][value="${task.window_pace || "catchup"}"]`);
      if (pr) pr.checked = true;
    } else {
      $("tk-name").value = "";
      $("tk-prompt").value = "";
      $("tk-business").value = HermesBiz.current || "all";
      $("tk-timezone").value = "America/Chicago";
      $("tk-enabled").checked = true;
      sb = { frequency: "daily", times: ["07:00"], days: [] };
      setSelectedApps([]);
      const radio = document.querySelector('input[name="tk-autonomy"][value="readonly"]');
      if (radio) radio.checked = true;
      $("tk-window-on").checked = false;
      $("tk-window-start").value = "";
      $("tk-window-end").value = "";
      $("tk-window-step").value = 1;
      const pr = document.querySelector('input[name="tk-window-pace"][value="catchup"]');
      if (pr) pr.checked = true;
    }
    syncScheduleUI();
    syncWindowUI();
    applyEditorFull();
    $("tk-editor-overlay").hidden = false;
  }

  function closeEditor() { $("tk-editor-overlay").hidden = true; }

  // Expand the editor to full-page. The preference persists across opens.
  let editorFull = false;
  function applyEditorFull() {
    $("tk-editor-overlay").classList.toggle("tk-full", editorFull);
    const btn = $("tk-editor-expand");
    if (btn) {
      btn.textContent = editorFull ? "⤡" : "⤢";
      btn.title = editorFull ? "Shrink to window" : "Expand to full screen";
      btn.setAttribute("aria-label", btn.title);
    }
  }
  function toggleEditorFull() { editorFull = !editorFull; applyEditorFull(); }

  function collectForm() {
    const name = $("tk-name").value.trim();
    const prompt = $("tk-prompt").value.trim();
    if (!name) return { error: "Give the task a name." };
    if (!prompt) return { error: "Describe what the task should do." };
    if (sb.frequency !== "hourly" && !sb.times.length)
      return { error: "Add at least one time for the schedule." };
    if (sb.frequency === "weekly" && !sb.days.length)
      return { error: "Pick at least one day for a weekly schedule." };

    // Backfill window (optional). Always send the `window` key — an object when
    // enabled, null when off — so an edit can also CLEAR a previously-set window.
    let window = null;
    if ($("tk-window-on").checked) {
      const start = $("tk-window-start").value;
      const end = $("tk-window-end").value || null;
      const step = Math.max(1, parseInt($("tk-window-step").value, 10) || 1);
      const pace = windowPace();
      if (!start) return { error: "Pick a start date for the backfill window." };
      if (end && new Date(end) < new Date(start))
        return { error: "The window's end date must be on or after the start." };
      if (pace === "catchup" && !end)
        return { error: "A catch-up backfill needs an end date." };
      window = { pace, start, end, step_days: step };
    }

    const business = $("tk-business").value;
    return {
      body: {
        name,
        prompt,
        business,
        allowed_apps: getSelectedApps(),
        autonomy: (document.querySelector('input[name="tk-autonomy"]:checked') || {}).value || "readonly",
        timezone: $("tk-timezone").value,
        enabled: $("tk-enabled").checked,
        schedule: {
          frequency: sb.frequency,
          times: sb.frequency === "hourly" ? sb.times.slice(0, 1) : sb.times,
          days_of_week: sb.frequency === "weekly" ? sb.days : [],
        },
        window,
      },
    };
  }

  async function saveTask() {
    const { body, error } = collectForm();
    if (error) { $("tk-editor-msg").textContent = error; return; }
    const btn = $("tk-editor-save");
    btn.disabled = true; btn.textContent = "Saving…";
    try {
      const url = editingId ? `/api/scheduled-tasks/${editingId}` : "/api/scheduled-tasks";
      const method = editingId ? "PATCH" : "POST";
      const r = await fetch(url, {
        method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      const j = await r.json();
      if (!r.ok) { $("tk-editor-msg").textContent = j.error || "Could not save."; return; }
      closeEditor();
      await loadTasks();
    } catch (e) {
      $("tk-editor-msg").textContent = "Error: " + e;
    } finally {
      btn.disabled = false; btn.textContent = "Save task";
    }
  }

  // ── Detail modal ─────────────────────────────────────────────────────────
  async function openDetail(taskId) {
    $("tk-detail-overlay").hidden = false;
    $("tk-detail-name").textContent = "Loading…";
    $("tk-detail-sched").textContent = "";
    $("tk-detail-meta").innerHTML = "";
    $("tk-detail-prompt").textContent = "";
    $("tk-runs").innerHTML = "";
    $("tk-runs-empty").hidden = true;
    $("tk-detail-msg").textContent = "";
    try {
      const r = await fetch(`/api/scheduled-tasks/${taskId}`);
      const j = await r.json();
      if (!r.ok) { $("tk-detail-name").textContent = j.error || "Not found"; return; }
      detailTask = j.task;
      renderDetail(j.task, j.runs || []);
    } catch (e) {
      $("tk-detail-name").textContent = "Error: " + e;
    }
  }

  function closeDetail() { $("tk-detail-overlay").hidden = true; detailTask = null; }

  function renderDetail(t, runs) {
    $("tk-detail-name").textContent = t.name;
    $("tk-detail-sched").textContent = `⏱ ${t.schedule_summary || ""}`;
    const appsStr = (t.allowed_apps && t.allowed_apps.length)
      ? t.allowed_apps.map((s) => {
          const a = APPS.find((x) => x.slug === s);
          return a ? `${a.emoji} ${a.label}` : s;
        }).join(" · ")
      : "All connected apps";
    $("tk-detail-meta").innerHTML =
      `<div class="tk-detail-row">${bizChip(t.business)}${AUTONOMY_CHIP[t.autonomy] || ""}` +
      `<span class="tk-chip ${t.enabled ? "tk-chip-on" : "tk-chip-off"}">${t.enabled ? "● Enabled" : "❚❚ Paused"}</span></div>` +
      `<div class="tk-detail-line"><b>Apps:</b> ${esc(appsStr)}</div>` +
      `<div class="tk-detail-line"><b>Next run:</b> ${t.enabled ? esc(fmtWhen(t.next_run_at)) : "—"}` +
      `  ·  <b>Timezone:</b> ${esc(t.timezone || "")}</div>`;
    $("tk-detail-prompt").innerHTML = `<b>Prompt</b><div class="tk-detail-prompt-body">${esc(t.prompt)}</div>`;
    renderRuns(runs);
  }

  function renderRuns(runs) {
    const wrap = $("tk-runs");
    $("tk-runs-count").textContent = runs.length ? `${runs.length} run${runs.length > 1 ? "s" : ""}` : "";
    if (!runs.length) { wrap.innerHTML = ""; $("tk-runs-empty").hidden = false; return; }
    $("tk-runs-empty").hidden = true;
    wrap.innerHTML = runs.map((run, i) => {
      const badge = `<span class="tk-run-badge ${esc(run.status)}">${esc(run.status)}</span>`;
      const trig = run.trigger === "manual" ? "▶ Manual" : "⏱ Scheduled";
      const body = run.status === "error"
        ? `<div class="tk-run-err">${esc(run.error || "Unknown error")}</div>`
        : `<div class="tk-run-reply">${md(run.reply || "(no output)")}</div>`;
      const tools = (run.tool_calls && run.tool_calls.length)
        ? `<div class="tk-run-tools">${run.tool_calls.map((c) => `<span class="tk-run-tool">${esc(c.label || c.tool || "")}</span>`).join("")}</div>`
        : "";
      const files = (run.files && run.files.length)
        ? `<div class="tk-run-files"><div class="tk-run-files-label">📎 Files created</div>` +
          run.files.map((f) => {
            const size = f.size_bytes ? ` · ${fmtBytes(f.size_bytes)}` : "";
            const label = `📄 ${esc(f.filename || "file")}${size}`;
            return f.download_url
              ? `<a class="tk-run-file" href="${esc(f.download_url)}" target="_blank" rel="noopener" download>${label}</a>`
              : `<span class="tk-run-file tk-run-file-dead" title="Link unavailable">${label}</span>`;
          }).join("") + `</div>`
        : "";
      const fileTag = (run.files && run.files.length)
        ? `<span class="tk-run-filecount" title="${run.files.length} file(s) created">📎 ${run.files.length}</span>`
        : "";
      return `
        <div class="tk-run">
          <div class="tk-run-head" data-run="${i}">
            ${badge}<span class="tk-run-trig">${trig}</span>
            <span class="tk-run-time">${esc(fmtWhen(run.started_at))}</span>
            ${fileTag}
            <span class="tk-run-chev">▾</span>
          </div>
          <div class="tk-run-body" hidden>${tools}${files}${body}</div>
        </div>`;
    }).join("");
    wrap.querySelectorAll(".tk-run-head").forEach((h) =>
      h.addEventListener("click", () => {
        const body = h.nextElementSibling;
        body.hidden = !body.hidden;
        h.querySelector(".tk-run-chev").textContent = body.hidden ? "▾" : "▴";
      }));
  }

  async function runNow() {
    if (!detailTask) return;
    const btn = $("tk-detail-run");
    btn.disabled = true; btn.textContent = "Running…";
    $("tk-detail-msg").textContent = "Running the task — this can take a moment.";
    try {
      const r = await fetch(`/api/scheduled-tasks/${detailTask.task_id}/run`, { method: "POST" });
      const j = await r.json();
      if (!r.ok) { $("tk-detail-msg").textContent = j.error || "Run failed."; return; }
      $("tk-detail-msg").textContent = j.status === "error" ? "Run finished with an error." : "Run complete.";
      // Refresh the run list (and the card's last-run stamp behind the modal).
      const rr = await fetch(`/api/scheduled-tasks/${detailTask.task_id}/runs`);
      const jj = await rr.json();
      renderRuns(jj.runs || []);
      loadTasks();
    } catch (e) {
      $("tk-detail-msg").textContent = "Error: " + e;
    } finally {
      btn.disabled = false; btn.textContent = "▶ Run now";
    }
  }

  async function deleteTask() {
    if (!detailTask) return;
    if (!confirm(`Delete “${detailTask.name}”? This removes the task and its run history.`)) return;
    try {
      const r = await fetch(`/api/scheduled-tasks/${detailTask.task_id}`, { method: "DELETE" });
      if (!r.ok) { const j = await r.json(); $("tk-detail-msg").textContent = j.error || "Delete failed."; return; }
      closeDetail();
      loadTasks();
    } catch (e) {
      $("tk-detail-msg").textContent = "Error: " + e;
    }
  }

  async function toggleEnabled(taskId, enabled) {
    try {
      await fetch(`/api/scheduled-tasks/${taskId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
    } catch (_) { /* best-effort; a reload would resync */ }
    loadTasks();
  }

  // ── Wiring ───────────────────────────────────────────────────────────────
  function bindOnce() {
    HermesBiz.bind($("hermes-biz-select-tasks"), $("hermes-biz-badge-tasks"));
    HermesBiz.onChange(() => { if (inited) loadTasks(); });

    ["task-new-btn", "task-new-btn-m", "task-new-btn-main", "task-new-btn-empty"].forEach((id) =>
      $(id)?.addEventListener("click", () => openEditor(null)));

    // Editor modal
    $("tk-editor-close")?.addEventListener("click", closeEditor);
    $("tk-editor-cancel")?.addEventListener("click", closeEditor);
    $("tk-editor-expand")?.addEventListener("click", toggleEditorFull);
    $("tk-editor-save")?.addEventListener("click", saveTask);
    $("tk-editor-overlay")?.addEventListener("click", (e) => {
      if (e.target === $("tk-editor-overlay")) closeEditor();
    });

    // Frequency tabs
    document.querySelectorAll("#tk-freq-tabs .tk-freq").forEach((b) =>
      b.addEventListener("click", () => { sb.frequency = b.dataset.freq; syncScheduleUI(); }));
    // Weekly day chips
    document.querySelectorAll("#tk-days .tk-day").forEach((b) =>
      b.addEventListener("click", () => {
        const d = +b.dataset.day;
        sb.days = sb.days.includes(d) ? sb.days.filter((x) => x !== d) : [...sb.days, d];
        syncScheduleUI();
      }));
    // Add time
    $("tk-add-time-btn")?.addEventListener("click", () => {
      const v = $("tk-time-input").value;
      if (!v) return;
      if (sb.frequency === "hourly") sb.times = [v];
      else if (!sb.times.includes(v)) sb.times.push(v);
      syncScheduleUI();
    });

    // Backfill / date window
    $("tk-window-on")?.addEventListener("change", syncWindowUI);
    ["tk-window-start", "tk-window-end", "tk-window-step"].forEach((id) =>
      $(id)?.addEventListener("input", syncWindowUI));
    document.querySelectorAll('input[name="tk-window-pace"]').forEach((r) =>
      r.addEventListener("change", syncWindowUI));

    renderTemplates();
    renderApps();

    // Detail modal
    $("tk-detail-close")?.addEventListener("click", closeDetail);
    $("tk-detail-overlay")?.addEventListener("click", (e) => {
      if (e.target === $("tk-detail-overlay")) closeDetail();
    });
    $("tk-detail-run")?.addEventListener("click", runNow);
    $("tk-detail-delete")?.addEventListener("click", deleteTask);
    $("tk-detail-edit")?.addEventListener("click", () => {
      const t = detailTask;
      closeDetail();
      if (t) openEditor(t);
    });

    // Delegated: open detail on card click, toggle enable on the switch.
    $("tasks-list")?.addEventListener("click", (e) => {
      const toggle = e.target.closest("[data-toggle]");
      if (toggle) {
        e.stopPropagation();
        toggleEnabled(toggle.dataset.toggle, toggle.checked);
        return;
      }
      const open = e.target.closest("[data-open]");
      if (open) openDetail(open.dataset.open);
    });

    // Esc closes whichever modal is open.
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      if (!$("tk-editor-overlay").hidden) closeEditor();
      else if (!$("tk-detail-overlay").hidden) closeDetail();
    });
  }

  window._tasksInit = function () {
    if (!inited) { bindOnce(); inited = true; }
    loadTasks();
  };
})();
