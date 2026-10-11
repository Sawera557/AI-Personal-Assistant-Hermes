// Jobs — code-driven recurring background jobs (scrapers & pipelines).
//
// Lists jobs with a live status badge (running / finished / failed), Start &
// Stop controls and an enable/pause toggle, plus a detail modal with run history
// (counts, searches used, the S3 CSV link). Talks to /api/jobs/*. Reuses the
// Scheduled Tasks (tk-*) card/run styles. Mirrors tasks.js's lazy-init pattern.
(function () {
  "use strict";

  let inited = false;
  let detailJob = null;            // job open in the detail modal
  let pollTimer = null;            // live-status interval while a job is running

  const $ = (id) => document.getElementById(id);
  const esc = (s) =>
    (typeof escapeHtml === "function"
      ? escapeHtml(String(s == null ? "" : s))
      : String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
          ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])));

  function fmtWhen(iso) {
    if (!iso) return "—";
    try {
      return new Date(iso).toLocaleString([], {
        month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
      });
    } catch (_) { return iso; }
  }

  function statusDot(status) {
    const map = { success: "ok", error: "err", running: "run", cancelled: "idle" };
    return `<span class="tk-status-dot ${map[status] || "idle"}"></span>`;
  }

  const isRunning = (j) => j && j.last_status === "running";
  const spinner = (text) => `<div class="spin-box"><span class="spin" role="status" aria-label="Loading"></span>${text ? esc(text) : ""}</div>`;

  // ── In-app dialog (replaces the browser's confirm()/alert()) ─────────────
  let _dlg = null;
  function ensureDialog() {
    if (_dlg) return _dlg;
    const o = document.createElement("div");
    o.className = "appdlg-overlay";
    o.hidden = true;
    o.innerHTML =
      `<div class="appdlg" role="dialog" aria-modal="true" aria-labelledby="appdlg-title">
         <div class="appdlg-title" id="appdlg-title"></div>
         <div class="appdlg-msg" id="appdlg-msg"></div>
         <div class="appdlg-actions">
           <button class="tk-btn tk-cancel" id="appdlg-cancel" type="button">Cancel</button>
           <button class="tk-btn tk-save" id="appdlg-ok" type="button">OK</button>
         </div>
       </div>`;
    document.body.appendChild(o);
    _dlg = {
      o, box: o.querySelector(".appdlg"),
      title: o.querySelector("#appdlg-title"), msg: o.querySelector("#appdlg-msg"),
      cancel: o.querySelector("#appdlg-cancel"), ok: o.querySelector("#appdlg-ok"),
    };
    return _dlg;
  }
  function openDialog({ title, message, okText = "OK", danger = false, showCancel = true }) {
    const d = ensureDialog();
    d.title.textContent = title || "";
    d.msg.textContent = message || "";
    d.ok.textContent = okText;
    d.cancel.style.display = showCancel ? "" : "none";
    d.ok.classList.toggle("tk-danger", !!danger);
    d.o.hidden = false;
    return new Promise((resolve) => {
      const done = (v) => { cleanup(); d.o.hidden = true; resolve(v); };
      const onOk = () => done(true), onCancel = () => done(false);
      const onKey = (e) => { if (e.key === "Escape") done(false); else if (e.key === "Enter") done(true); };
      const onBack = (e) => { if (e.target === d.o) done(false); };
      function cleanup() {
        d.ok.removeEventListener("click", onOk); d.cancel.removeEventListener("click", onCancel);
        document.removeEventListener("keydown", onKey, true); d.o.removeEventListener("click", onBack);
      }
      d.ok.addEventListener("click", onOk); d.cancel.addEventListener("click", onCancel);
      document.addEventListener("keydown", onKey, true); d.o.addEventListener("click", onBack);
      d.ok.focus();
    });
  }
  const appConfirm = (message, opts = {}) =>
    openDialog({ title: opts.title || "Please confirm", message, okText: opts.okText || "Confirm", danger: opts.danger, showCancel: true });
  const appNotice = (message, opts = {}) =>
    openDialog({ title: opts.title || "Heads up", message, okText: "OK", showCancel: false });

  // ── Job list ─────────────────────────────────────────────────────────────
  async function loadJobs() {
    const listEl = $("jobs-list");
    const emptyEl = $("jobs-empty");
    if (!listEl) return;
    if (!listEl.dataset.loaded) listEl.innerHTML = spinner("Loading jobs…");
    let jobs = [];
    try {
      const r = await fetch("/api/jobs");
      const j = await r.json();
      jobs = j.jobs || [];
    } catch (e) {
      listEl.innerHTML = `<p class="hint" style="padding:8px">Couldn't load jobs: ${esc(e)}</p>`;
      return;
    }
    listEl.dataset.loaded = "1";
    if (!jobs.length) {
      listEl.innerHTML = "";
      if (emptyEl) emptyEl.hidden = false;
      schedulePolling(jobs);
      return;
    }
    if (emptyEl) emptyEl.hidden = true;
    listEl.innerHTML = jobs.map(jobCard).join("");
    schedulePolling(jobs);
    // If the detail modal is open, keep it fresh too.
    if (detailJob && !$("jb-detail-overlay").hidden) {
      const fresh = jobs.find((j) => j.job_id === detailJob.job_id);
      if (fresh) refreshDetailRuns(fresh);
    }
  }

  function jobCard(j) {
    const disabled = !j.enabled ? " tk-card-off" : "";
    const next = j.enabled
      ? `<span class="tk-next">Next: ${esc(fmtWhen(j.next_run_at))}</span>`
      : `<span class="tk-next tk-paused">Paused</span>`;
    const last = j.last_run_at
      ? `<span class="tk-last">${statusDot(j.last_status)}Last run ${esc(fmtWhen(j.last_run_at))}</span>`
      : `<span class="tk-last">${statusDot()}Never run</span>`;
    const running = isRunning(j);
    const action = running
      ? `<button class="tk-btn tk-danger" data-stop="${esc(j.job_id)}" type="button">■ Stop</button>`
      : `<button class="tk-btn tk-save" data-start="${esc(j.job_id)}" type="button">▶ Start</button>`;
    return `
      <div class="tk-card${disabled}" data-id="${esc(j.job_id)}">
        <div class="tk-card-main" data-open="${esc(j.job_id)}">
          <div class="tk-card-top">
            <div class="tk-card-name">${esc(j.name)}</div>
            <label class="tk-toggle" title="${j.enabled ? "Enabled" : "Paused"}">
              <input type="checkbox" ${j.enabled ? "checked" : ""} data-toggle="${esc(j.job_id)}">
              <span class="tk-toggle-track"></span>
            </label>
          </div>
          <div class="tk-card-sched">⏱ ${esc(j.schedule_summary || "")}</div>
          <div class="tk-card-chips">
            <span class="tk-chip">⚙️ ${esc(j.job_type)}</span>
            ${running ? `<span class="tk-run-badge running">running</span>` : ""}
          </div>
          <div class="tk-card-foot">${next}${last}</div>
        </div>
        <div class="tk-footer-actions" style="padding:0 16px 14px">${action}</div>
      </div>`;
  }

  // ── Detail modal ─────────────────────────────────────────────────────────
  async function openDetail(jobId) {
    $("jb-detail-overlay").hidden = false;
    $("jb-detail-name").textContent = "Loading…";
    $("jb-detail-sched").textContent = "";
    $("jb-detail-meta").innerHTML = "";
    $("jb-runs").innerHTML = spinner("Loading run history…");
    $("jb-runs-empty").hidden = true;
    $("jb-detail-msg").textContent = "";
    try {
      const r = await fetch(`/api/jobs/${jobId}`);
      const j = await r.json();
      if (!r.ok) { $("jb-detail-name").textContent = j.error || "Not found"; return; }
      detailJob = j.job;
      renderDetail(j.job, j.runs || []);
    } catch (e) {
      $("jb-detail-name").textContent = "Error: " + e;
    }
  }

  function closeDetail() { $("jb-detail-overlay").hidden = true; detailJob = null; }

  // Feedback that works whether or not the detail modal is open.
  function feedback(text) {
    const overlay = $("jb-detail-overlay");
    if (overlay && !overlay.hidden) { $("jb-detail-msg").textContent = text; }
    else if (text) { appNotice(text); }
  }

  function renderDetail(j, runs) {
    detailJob = j;
    $("jb-detail-name").textContent = j.name;
    $("jb-detail-sched").textContent = `⏱ ${j.schedule_summary || ""}`;
    const am = $("jb-allow-multi");
    if (am) am.checked = !!j.allow_multiple_runs;
    $("jb-detail-meta").innerHTML =
      `<div class="tk-detail-row"><span class="tk-chip">⚙️ ${esc(j.job_type)}</span>` +
      `<span class="tk-chip ${j.enabled ? "tk-chip-on" : "tk-chip-off"}">${j.enabled ? "● Enabled" : "❚❚ Paused"}</span>` +
      `${isRunning(j) ? `<span class="tk-run-badge running">running</span>` : ""}</div>` +
      `<div class="tk-detail-line"><b>Next run:</b> ${j.enabled ? esc(fmtWhen(j.next_run_at)) : "—"}` +
      `  ·  <b>Timezone:</b> ${esc(j.timezone || "")}</div>`;
    // Toggle Start/Stop in the footer to match running state.
    $("jb-detail-run").hidden = isRunning(j);
    $("jb-detail-stop").hidden = !isRunning(j);
    renderRuns(runs);
  }

  async function refreshDetailRuns(job) {
    detailJob = job;
    $("jb-detail-run").hidden = isRunning(job);
    $("jb-detail-stop").hidden = !isRunning(job);
    try {
      const r = await fetch(`/api/jobs/${job.job_id}/runs`);
      const j = await r.json();
      renderRuns(j.runs || []);
    } catch (_) { /* best-effort */ }
  }

  function countsChips(counts) {
    const keys = Object.keys(counts || {});
    if (!keys.length) return "";
    return `<div class="jb-counts">` +
      keys.sort().map((k) => `<span class="jb-count-chip">${esc(k)}: ${esc(counts[k])}</span>`).join("") +
      `</div>`;
  }

  function driveSummary(res) {
    const t = res.totals || {};
    const added = res.added_total != null
      ? res.added_total
      : (t.synced || 0) + (t.updated || 0) + (t.restored || 0);
    const bits = [`<b>${esc(added)} file${added === 1 ? "" : "s"} added</b>`];
    if (t.scanned != null) bits.push(`scanned ${esc(t.scanned)}`);
    if (t.unchanged) bits.push(`unchanged ${esc(t.unchanged)}`);
    if (t.restored) bits.push(`restored ${esc(t.restored)}`);
    if (t.skipped) bits.push(`skipped ${esc(t.skipped)}`);
    if (t.errors) bits.push(`errors ${esc(t.errors)}`);
    let html = `<div>${bits.join(" · ")}</div>`;

    // Per-business "files added" chips (+ a ⚠️ for any business that errored).
    const abb = res.added_by_business || {};
    const chips = Object.keys(abb).sort().map(
      (s) => `<span class="jb-count-chip">${esc(s)}: ${esc(abb[s])}</span>`);
    const bb = res.by_business || {};
    Object.keys(bb).forEach((s) => {
      if (bb[s] && bb[s].error) chips.push(`<span class="jb-count-chip">${esc(s)}: ⚠️</span>`);
    });
    if (chips.length) html += `<div class="jb-counts">${chips.join("")}</div>`;

    if (res.categories && res.categories.length)
      html += `<div>Categories: ${esc(res.categories.join(", "))}</div>`;

    const files = res.added_files || [];
    if (files.length) {
      const items = files.map((f) => {
        const extra = f.outcome && f.outcome !== "synced" ? ` · ${esc(f.outcome)}` : "";
        return `<li>${esc(f.name || "(unnamed)")}` +
          `<span class="jb-file-meta"> — ${esc(f.business || "")}/${esc(f.category || "")}${extra}</span></li>`;
      }).join("");
      html += `<ul class="jb-file-list">${items}</ul>`;
      if (res.added_files_truncated) html += `<div>…and more (list capped at 300)</div>`;
    } else if (added === 0) {
      html += `<div>Nothing new to sync — everything is already in S3.</div>`;
    }
    return `<div class="jb-run-summary">${html}</div>`;
  }

  function runSummary(res) {
    if (!res || typeof res !== "object") return "";
    if (res.added_by_business || res.by_business) return driveSummary(res);
    const bits = [];
    if (res.date) bits.push(`<b>${esc(res.date)}</b>`);
    if (res.rows_written != null) bits.push(`${esc(res.rows_written)} rows`);
    if (res.searches_used != null) bits.push(`${esc(res.searches_used)} searches`);
    if (res.buckets && res.buckets.length) bits.push(`buckets: ${esc(res.buckets.join(", "))}`);
    let html = bits.length ? `<div>${bits.join(" · ")}</div>` : "";
    html += countsChips(res.counts);
    if (res.report_url) html += `<div><a href="${esc(res.report_url)}" target="_blank" rel="noopener" class="jb-report-link">📊 Open full report</a></div>`;
    if (res.s3_uri) html += `<div>Saved to <code>${esc(res.s3_uri)}</code></div>`;
    else if (res.s3_enabled === false && res.rows_written) html += `<div>⚠️ S3 disabled — rows were not persisted (set S3_BUCKET).</div>`;
    return html ? `<div class="jb-run-summary">${html}</div>` : "";
  }

  function renderRuns(runs) {
    const wrap = $("jb-runs");
    $("jb-runs-count").textContent = runs.length ? `${runs.length} run${runs.length > 1 ? "s" : ""}` : "";
    if (!runs.length) { wrap.innerHTML = ""; $("jb-runs-empty").hidden = false; return; }
    $("jb-runs-empty").hidden = true;
    wrap.innerHTML = runs.map((run, i) => {
      const badge = `<span class="tk-run-badge ${esc(run.status)}">${esc(run.status)}</span>`;
      const trig = run.trigger === "manual" ? "▶ Manual" : "⏱ Scheduled";
      const body = run.status === "error"
        ? `<div class="tk-run-err">${esc(run.error || "Unknown error")}</div>`
        : runSummary(run.result);
      return `
        <div class="tk-run">
          <div class="tk-run-head" data-run="${i}">
            ${badge}<span class="tk-run-trig">${trig}</span>
            <span class="tk-run-time">${esc(fmtWhen(run.started_at))}</span>
            <span class="tk-run-chev">▾</span>
          </div>
          <div class="tk-run-body" hidden>${body || `<div class="jb-run-summary">(no details)</div>`}</div>
        </div>`;
    }).join("");
    wrap.querySelectorAll(".tk-run-head").forEach((h) =>
      h.addEventListener("click", () => {
        const body = h.nextElementSibling;
        body.hidden = !body.hidden;
        h.querySelector(".tk-run-chev").textContent = body.hidden ? "▾" : "▴";
      }));
  }

  // ── Actions ──────────────────────────────────────────────────────────────
  async function startJob(jobId, btn) {
    // Confirm so a job is never started by an accidental click.
    const ok = await appConfirm(
      "This starts a live scrape (uses API credits) and writes the results to storage.",
      { title: "Run this job now?", okText: "Run now" });
    if (!ok) return;
    if (btn) { btn.disabled = true; btn.textContent = "Starting…"; }
    if (detailJob && detailJob.job_id === jobId) feedback("Running the job — this can take a moment.");
    // Optimistically start polling so the cron/other-tab case updates.
    startPolling();
    try {
      const r = await fetch(`/api/jobs/${jobId}/run`, { method: "POST" });
      const j = await r.json();
      if (!r.ok) {
        feedback(j.error || "Run failed.");
      } else if (j.status === "skipped") {
        feedback(j.reason === "already ran today"
          ? "This job already ran today. Turn on “Allow multiple runs per day” to run it again."
          : ("Skipped: " + (j.reason || "not run")));
      } else {
        feedback(j.status === "error" ? "Run finished with an error."
          : j.status === "cancelled" ? "Run stopped." : "Run complete.");
      }
    } catch (e) {
      feedback("Error: " + e);
    } finally {
      await loadJobs();
      if (detailJob && detailJob.job_id === jobId) openDetail(jobId);
    }
  }

  async function setAllowMultiple(jobId, allow) {
    try {
      await fetch(`/api/jobs/${jobId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ allow_multiple_runs: allow }),
      });
    } catch (_) { /* best-effort */ }
    loadJobs();
  }

  async function stopJob(jobId, btn) {
    if (btn) { btn.disabled = true; btn.textContent = "Stopping…"; }
    const msg = $("jb-detail-msg");
    try {
      const r = await fetch(`/api/jobs/${jobId}/stop`, { method: "POST" });
      const j = await r.json();
      if (msg) msg.textContent = r.ok ? "Stop requested — the job will halt shortly." : (j.error || "Could not stop.");
    } catch (e) {
      if (msg) msg.textContent = "Error: " + e;
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "■ Stop"; }
      loadJobs();
    }
  }

  async function toggleEnabled(jobId, enabled) {
    try {
      await fetch(`/api/jobs/${jobId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
    } catch (_) { /* best-effort; a reload would resync */ }
    loadJobs();
  }

  // ── Live polling while any job is running ────────────────────────────────
  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(loadJobs, 5000);
  }
  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }
  function schedulePolling(jobs) {
    if ((jobs || []).some(isRunning)) startPolling();
    else stopPolling();
  }

  // ── Wiring ───────────────────────────────────────────────────────────────
  function bindOnce() {
    $("jobs-refresh-btn")?.addEventListener("click", loadJobs);
    $("jobs-refresh-main")?.addEventListener("click", loadJobs);

    // Delegated: card click → detail; start/stop buttons; enable toggle.
    $("jobs-list")?.addEventListener("click", (e) => {
      const start = e.target.closest("[data-start]");
      if (start) { e.stopPropagation(); startJob(start.dataset.start, start); return; }
      const stop = e.target.closest("[data-stop]");
      if (stop) { e.stopPropagation(); stopJob(stop.dataset.stop, stop); return; }
      const toggle = e.target.closest("[data-toggle]");
      if (toggle) { e.stopPropagation(); toggleEnabled(toggle.dataset.toggle, toggle.checked); return; }
      const open = e.target.closest("[data-open]");
      if (open) openDetail(open.dataset.open);
    });

    // Detail modal
    $("jb-detail-close")?.addEventListener("click", closeDetail);
    $("jb-detail-overlay")?.addEventListener("click", (e) => {
      if (e.target === $("jb-detail-overlay")) closeDetail();
    });
    $("jb-detail-run")?.addEventListener("click", (e) => {
      if (detailJob) startJob(detailJob.job_id, e.currentTarget);
    });
    $("jb-detail-stop")?.addEventListener("click", (e) => {
      if (detailJob) stopJob(detailJob.job_id, e.currentTarget);
    });
    $("jb-allow-multi")?.addEventListener("change", (e) => {
      if (detailJob) setAllowMultiple(detailJob.job_id, e.target.checked);
    });

    // Esc closes the detail modal.
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !$("jb-detail-overlay").hidden) closeDetail();
    });
  }

  window._jobsInit = function () {
    if (!inited) { bindOnce(); inited = true; }
    loadJobs();
  };
})();
