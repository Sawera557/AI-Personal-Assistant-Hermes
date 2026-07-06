// Meeting Copilot — private, real-time guidance during a live meeting, plus
// AI-prepared briefs for upcoming meetings and AI summaries for past ones.
//
// Three moments this file covers:
//   Upcoming: calendar events -> "Prepare" -> AI brief (background, must-ask
//     questions, risks, links to relevant already-indexed files).
//   Live: mic runs continuously — a VAD pause just closes one utterance and
//     immediately starts recording the next, with zero gap. Each closed
//     utterance is queued and drained by a background worker (transcribe -->
//     append to notes --> /api/meetings/copilot, grounded in this business's
//     own indexed emails/files --> render bullet-point guidance), FIFO, so
//     nothing said during that round-trip is ever missed.
//   Ended: "End meeting" -> AI summary (decisions, action items, follow-up
//     draft) + best-effort Fathom recording link (manual fallback if not found).
//
// Never speaks, never sends anything, never joins/records the actual meeting
// platform — this only listens to what's ALREADY audible in the room (or
// what's typed) and writes private suggestions only the owner sees.
(() => {
  const listenBtn = document.getElementById("meeting-listen-btn");
  const listenBtnM = document.getElementById("meeting-listen-btn-m");
  const listenLabel = document.getElementById("meeting-listen-label");
  const statusEl = document.getElementById("meeting-status");
  const notesEl = document.getElementById("meeting-notes");
  const feedEl = document.getElementById("meeting-feed");
  const quickInput = document.getElementById("meeting-quick-input");
  const quickSend = document.getElementById("meeting-quick-send");
  const needBizEl = document.getElementById("meeting-need-biz");
  const mainEl = document.getElementById("meeting-main");
  const bizSubEl = document.getElementById("meeting-biz-sub");
  const selectEl = document.getElementById("hermes-biz-select-meeting");
  const badgeEl = document.getElementById("hermes-biz-badge-meeting");
  const newBtn = document.getElementById("meeting-new-btn");
  const sessionsEl = document.getElementById("meeting-sessions");
  const sessionsEmptyEl = document.getElementById("meeting-sessions-empty");
  const searchInput = document.getElementById("meeting-search-input");
  const subtabBtns = document.querySelectorAll(".meeting-subtab");
  const subUpcomingEl = document.getElementById("meeting-sub-upcoming");
  const subLiveEl = document.getElementById("meeting-sub-live");
  const upcomingListEl = document.getElementById("meeting-upcoming-list");
  const upcomingEmptyEl = document.getElementById("meeting-upcoming-empty");
  const briefBoxEl = document.getElementById("meeting-brief-box");
  const aiNotesBoxEl = document.getElementById("meeting-ai-notes-box");
  const summaryBoxEl = document.getElementById("meeting-summary-box");
  const endBtn = document.getElementById("meeting-end-btn");
  const notesClearBtn = document.getElementById("meeting-notes-clear-btn");
  const guidanceClearBtn = document.getElementById("meeting-guidance-clear-btn");

  if (!listenBtn || !notesEl || !feedEl) return; // page not present

  let initialized = false;
  let listening = false;
  let currentSessionId = null;
  let currentSession = null; // full session dict (brief/summary/fathom/status)
  let sessionQuery = "";

  // ---- Sub-tabs: Upcoming (calendar + briefs) / Live (one meeting) --------
  function switchSubtab(name) {
    subtabBtns.forEach((b) => b.classList.toggle("active", b.dataset.sub === name));
    if (subUpcomingEl) subUpcomingEl.hidden = name !== "upcoming";
    if (subLiveEl) subLiveEl.hidden = name !== "live";
    if (name === "upcoming") loadUpcoming();
  }
  subtabBtns.forEach((b) => b.addEventListener("click", () => switchSubtab(b.dataset.sub)));

  // ---- Business gating: this feature always needs ONE specific business ----
  // A meeting is always locked to one business (server enforces this too), so
  // switching businesses always starts a fresh meeting — same rule as chat
  // conversations being locked to whichever business their first turn used.
  let lastBiz = null;
  function refreshBizGate() {
    const biz = HermesBiz.current;
    const isSpecific = biz && biz !== "all";
    if (needBizEl) needBizEl.hidden = isSpecific;
    if (mainEl) mainEl.hidden = !isSpecific;
    if (isSpecific) {
      const info = HermesBiz.find(biz);
      const name = info ? info.name : biz;
      if (bizSubEl) bizSubEl.textContent = `Grounded in ${name}'s own indexed emails & files`;
    }
    if (!isSpecific && listening) stopListening();
    if (biz !== lastBiz) {
      lastBiz = biz;
      resetMeetingState();
      loadSessions();
      if (isSpecific) loadUpcoming();
    }
  }

  function initOnce() {
    if (initialized) return;
    initialized = true;
    HermesBiz.bind(selectEl, badgeEl);
    HermesBiz.onChange(refreshBizGate);
    refreshBizGate();
  }
  window._meetingInit = initOnce;

  // ---- New meeting / reset ----
  function resetMeetingState() {
    currentSessionId = null;
    currentSession = null;
    notesEl.value = "";
    feedEl.innerHTML = '<p class="hint meeting-feed-empty" id="meeting-feed-empty">Guidance will appear here as the meeting happens — grounded in this business\'s own emails and files, with sources cited.</p>';
    if (statusEl) statusEl.textContent = listening ? "Listening…" : "Not listening";
    if (briefBoxEl) { briefBoxEl.hidden = true; briefBoxEl.innerHTML = ""; }
    if (aiNotesBoxEl) { aiNotesBoxEl.hidden = true; aiNotesBoxEl.innerHTML = ""; }
    if (summaryBoxEl) { summaryBoxEl.hidden = true; summaryBoxEl.innerHTML = ""; }
    renderSessionsList(); // just re-highlight active (none) — list itself unchanged
  }
  newBtn?.addEventListener("click", () => {
    if (listening) stopListening();
    resetMeetingState();
    switchSubtab("live");
  });

  // ══════════════════════════════════════════════════════════════════════
  // Shared rendering: a brief's content (background / questions / risks /
  // facts / sources) — used both inline on an Upcoming card and in the Live
  // view's brief box, so the two never drift out of sync.
  // ══════════════════════════════════════════════════════════════════════
  function renderBriefContent(container, brief) {
    container.innerHTML = "";
    brief = brief || {};
    if (brief.background) {
      const p = document.createElement("p");
      p.className = "meeting-brief-bg";
      p.textContent = brief.background;
      container.appendChild(p);
    }
    [
      ["Must-ask questions", brief.must_ask_questions],
      ["Risks", brief.risks],
      ["Key facts", brief.key_facts],
    ].forEach(([label, items]) => {
      if (!items || !items.length) return;
      const lbl = document.createElement("div");
      lbl.className = "meeting-brief-label";
      lbl.textContent = label;
      container.appendChild(lbl);
      const ul = document.createElement("ul");
      ul.className = "meeting-bullets";
      items.forEach((i) => { const li = document.createElement("li"); li.textContent = i; ul.appendChild(li); });
      container.appendChild(ul);
    });
    const sources = brief.sources || [];
    if (sources.length) {
      const srcRow = document.createElement("div");
      srcRow.className = "meeting-sources";
      sources.slice(0, 6).forEach((s) => {
        const chip = document.createElement(s.download_url ? "a" : "span");
        chip.className = "meeting-source-chip";
        chip.textContent = `${s.source_system || "record"}: ${(s.title || "untitled").slice(0, 34)}`;
        if (s.download_url) { chip.href = s.download_url; chip.target = "_blank"; chip.rel = "noopener"; }
        srcRow.appendChild(chip);
      });
      container.appendChild(srcRow);
    }
    if (!brief.background && !sources.length) {
      container.innerHTML = '<p class="hint" style="margin:0">No brief yet.</p>';
    }
  }

  // ══════════════════════════════════════════════════════════════════════
  // Collapsible info box — shared shell for brief / AI notes / summary.
  // Collapsed by default so Notes & Transcript / Guidance keep the screen;
  // click the header to expand. `expanded: true` shows it open right away
  // (used right after generating something, so you see the fresh result).
  // ══════════════════════════════════════════════════════════════════════
  function buildCollapsibleBox(boxEl, title, { expanded = false } = {}) {
    boxEl.innerHTML = "";
    const head = document.createElement("div");
    head.className = "meeting-box-head";
    const titleEl = document.createElement("div");
    titleEl.className = "meeting-box-title";
    titleEl.textContent = title;
    const rightRow = document.createElement("div");
    rightRow.className = "meeting-box-head-actions";
    const chevron = document.createElement("span");
    chevron.className = "meeting-box-chevron";
    chevron.textContent = "▾";
    head.appendChild(titleEl);
    head.appendChild(rightRow);
    head.appendChild(chevron);

    const body = document.createElement("div");
    body.className = "meeting-box-body";
    body.hidden = !expanded;
    chevron.classList.toggle("is-collapsed", !expanded);

    head.addEventListener("click", (ev) => {
      if (rightRow.contains(ev.target)) return; // let PDF/build buttons handle their own clicks
      body.hidden = !body.hidden;
      chevron.classList.toggle("is-collapsed", body.hidden);
    });

    boxEl.appendChild(head);
    boxEl.appendChild(body);
    return { rightRow, body };
  }

  function createPdfButton(onClick) {
    const btn = document.createElement("button");
    btn.className = "meeting-pdf-btn";
    btn.type = "button";
    btn.title = "Download as PDF";
    btn.textContent = "⬇ PDF";
    btn.onclick = (ev) => { ev.stopPropagation(); onClick(); };
    return btn;
  }

  function pdfFilename(title, kind) {
    const safe = (title || "meeting").replace(/[^a-z0-9]+/gi, "_").replace(/^_+|_+$/g, "").slice(0, 50) || "meeting";
    return `${safe}_${kind.replace(/\s+/g, "_")}.pdf`;
  }

  // Simple, reliable text-based PDF export (no html2canvas dependency —
  // just paginated wrapped text via jsPDF's core API). "## " prefix on a
  // line renders it as a bold section heading.
  function downloadPdf(filename, title, lines) {
    if (!window.jspdf || !window.jspdf.jsPDF) { alert("PDF export isn't available right now — try reloading the page."); return; }
    const { jsPDF } = window.jspdf;
    const doc = new jsPDF({ unit: "pt", format: "a4" });
    const marginX = 44, maxWidth = 507, pageBottom = 780;
    let y = 56;
    doc.setFont("helvetica", "bold");
    doc.setFontSize(15);
    doc.splitTextToSize(title, maxWidth).forEach((l) => { doc.text(l, marginX, y); y += 19; });
    y += 10;
    lines.forEach((rawLine) => {
      const isHeading = rawLine.startsWith("## ");
      const text = isHeading ? rawLine.slice(3) : rawLine;
      doc.setFont("helvetica", isHeading ? "bold" : "normal");
      doc.setFontSize(isHeading ? 12 : 10.5);
      doc.splitTextToSize(text || " ", maxWidth).forEach((wLine) => {
        if (y > pageBottom) { doc.addPage(); y = 56; }
        doc.text(wLine, marginX, y);
        y += isHeading ? 17 : 14;
      });
      if (isHeading) y += 2;
    });
    doc.save(filename);
  }

  function briefToPdfLines(brief) {
    const lines = [];
    if (brief.background) lines.push("## Background", brief.background, "");
    [["Must-Ask Questions", brief.must_ask_questions], ["Risks", brief.risks], ["Key Facts", brief.key_facts]]
      .forEach(([label, items]) => {
        if (!items || !items.length) return;
        lines.push(`## ${label}`);
        items.forEach((i) => lines.push("• " + i));
        lines.push("");
      });
    const sources = brief.sources || [];
    if (sources.length) {
      lines.push("## Sources");
      sources.forEach((s) => lines.push(`• [${s.source_system || "record"}] ${s.title || "untitled"}`));
    }
    return lines;
  }

  function markdownToPdfLines(md) {
    return (md || "")
      .replace(/\*\*(.*?)\*\*/g, "$1")
      .replace(/\[(.*?)\]\((.*?)\)/g, "$1 ($2)")
      .split("\n")
      .map((line) => {
        const heading = line.match(/^#{1,6}\s*(.*)/);
        if (heading) return "## " + heading[1];
        return line.replace(/^[-*]\s+/, "• ");
      });
  }

  function renderBriefBox(session) {
    if (!briefBoxEl) return;
    const brief = session && session.brief;
    const hasContent = brief && (brief.background || (brief.sources || []).length);
    if (!hasContent) { briefBoxEl.hidden = true; briefBoxEl.innerHTML = ""; return; }
    briefBoxEl.hidden = false;
    const { rightRow, body } = buildCollapsibleBox(briefBoxEl, "Pre-meeting brief");
    rightRow.appendChild(createPdfButton(() => downloadPdf(
      pdfFilename(session.title, "Brief"), "Pre-Meeting Brief — " + (session.title || ""), briefToPdfLines(brief),
    )));
    renderBriefContent(body, brief);
  }

  // ══════════════════════════════════════════════════════════════════════
  // AI Meeting Notes — a deeper, on-demand pull through the full Hermes
  // agent (real Gmail/Drive search, not just what's already indexed).
  // Regenerable: the button always says "Build" the first time, "Rebuild"
  // once notes exist, and can be clicked again any time for fresh results.
  // ══════════════════════════════════════════════════════════════════════
  function createAiNotesButton(sessionId, businessSlug, meetingTitle, onResult) {
    const btn = document.createElement("button");
    btn.className = "meeting-ai-notes-btn";
    btn.type = "button";
    btn.textContent = "Build AI Meeting Notes";
    btn.onclick = async (ev) => {
      ev.stopPropagation();
      const original = btn.textContent;
      btn.disabled = true;
      btn.textContent = "Reading Gmail & Drive… (1–3 min)";
      try {
        const res = await fetch(`/api/meetings/sessions/${encodeURIComponent(sessionId)}/ai-notes`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ business: businessSlug, meeting_title: meetingTitle }),
        });
        const data = await res.json();
        if (data.error) {
          alert(data.error);
          btn.textContent = original;
          btn.disabled = false;
        } else {
          onResult(data.ai_notes || (data.notes || ""), data.ai_notes_generated_at);
        }
      } catch (err) {
        alert("Couldn't build AI meeting notes: " + (err && err.message ? err.message : err));
        btn.textContent = original;
        btn.disabled = false;
      }
    };
    return btn;
  }

  function renderAiNotesContent(container, notes, generatedAt) {
    container.innerHTML = "";
    if (!notes) {
      container.innerHTML = '<p class="hint" style="margin:0">No AI meeting notes yet — click Build to pull relevant info from Gmail &amp; Drive.</p>';
      return;
    }
    if (generatedAt) {
      const stamp = document.createElement("div");
      stamp.className = "meeting-ai-notes-stamp";
      stamp.textContent = "Built " + new Date(generatedAt * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
      container.appendChild(stamp);
    }
    const body = document.createElement("div");
    body.className = "meeting-ai-notes-body";
    body.innerHTML = renderMarkdown(notes);
    container.appendChild(body);
  }

  // Persistent box in the Live view — always shows the build/rebuild button,
  // whether or not notes exist yet (unlike brief/summary boxes, which hide
  // entirely when empty). `opts.expanded` opens it right after a fresh build
  // so the result is immediately visible instead of collapsed.
  function renderAiNotesBox(session, opts = {}) {
    if (!aiNotesBoxEl || !session || !session.session_id) { if (aiNotesBoxEl) aiNotesBoxEl.hidden = true; return; }
    const biz = session.business || HermesBiz.current;
    aiNotesBoxEl.hidden = false;
    const { rightRow, body } = buildCollapsibleBox(aiNotesBoxEl, "AI Meeting Notes", { expanded: !!opts.expanded });

    if (session.ai_notes) {
      rightRow.appendChild(createPdfButton(() => downloadPdf(
        pdfFilename(session.title, "AI Notes"), "AI Meeting Notes — " + (session.title || ""), markdownToPdfLines(session.ai_notes),
      )));
    }
    const buildBtn = createAiNotesButton(session.session_id, biz, session.title, (notes, generatedAt) => {
      session.ai_notes = notes;
      session.ai_notes_generated_at = generatedAt;
      renderAiNotesBox(session, { expanded: true }); // re-render open so the fresh result is visible
    });
    if (session.ai_notes) buildBtn.textContent = "Rebuild AI Meeting Notes";
    rightRow.appendChild(buildBtn);

    renderAiNotesContent(body, session.ai_notes, session.ai_notes_generated_at);
  }

  function renderSummaryBox(session) {
    if (!summaryBoxEl) return;
    const summary = session.agenda_summary || {};
    const hasSummary = summary.summary || (summary.decisions || []).length || (summary.action_items || []).length;
    if (session.status !== "ended" && !hasSummary && !session.fathom_url) {
      summaryBoxEl.hidden = true;
      summaryBoxEl.innerHTML = "";
      return;
    }
    summaryBoxEl.hidden = false;
    const { rightRow, body } = buildCollapsibleBox(summaryBoxEl, "Meeting summary");
    if (hasSummary) {
      rightRow.appendChild(createPdfButton(() => {
        const lines = [];
        if (summary.summary) lines.push("## Summary", summary.summary, "");
        [["Decisions", summary.decisions], ["Action Items", summary.action_items], ["Open Questions", summary.open_questions]]
          .forEach(([label, items]) => {
            if (!items || !items.length) return;
            lines.push(`## ${label}`);
            items.forEach((i) => lines.push("• " + i));
            lines.push("");
          });
        if (summary.follow_up_email_draft) lines.push("## Follow-Up Email Draft (not sent)", summary.follow_up_email_draft);
        downloadPdf(pdfFilename(session.title, "Summary"), "Meeting Summary — " + (session.title || ""), lines);
      }));
    }

    if (summary.summary) {
      const p = document.createElement("p");
      p.className = "meeting-brief-bg";
      p.textContent = summary.summary;
      body.appendChild(p);
    }
    [
      ["Decisions", summary.decisions],
      ["Action items", summary.action_items],
      ["Open questions", summary.open_questions],
    ].forEach(([label, items]) => {
      if (!items || !items.length) return;
      const lbl = document.createElement("div");
      lbl.className = "meeting-brief-label";
      lbl.textContent = label;
      body.appendChild(lbl);
      const ul = document.createElement("ul");
      ul.className = "meeting-bullets";
      items.forEach((i) => { const li = document.createElement("li"); li.textContent = i; ul.appendChild(li); });
      body.appendChild(ul);
    });
    if (summary.follow_up_email_draft) {
      const lbl = document.createElement("div");
      lbl.className = "meeting-brief-label";
      lbl.textContent = "Follow-up email draft (not sent — draft only)";
      body.appendChild(lbl);
      const draft = document.createElement("div");
      draft.className = "meeting-email-draft";
      draft.textContent = summary.follow_up_email_draft;
      body.appendChild(draft);
    }

    // Fathom link: show it if present, else a manual-paste fallback — the
    // auto-fetch attempted server-side in /end may not have found one.
    const fathomRow = document.createElement("div");
    fathomRow.className = "meeting-fathom-row";
    if (session.fathom_url) {
      const link = document.createElement("a");
      link.href = session.fathom_url;
      link.target = "_blank";
      link.rel = "noopener";
      link.className = "meeting-fathom-link";
      link.textContent = "▶ Fathom recording";
      fathomRow.appendChild(link);
    } else {
      const input = document.createElement("input");
      input.className = "meeting-quick-input";
      input.placeholder = "No Fathom link found — paste it here…";
      const btn = document.createElement("button");
      btn.className = "meeting-quick-send";
      btn.textContent = "Save";
      btn.onclick = async () => {
        const url = (input.value || "").trim();
        if (!url) return;
        try {
          const res = await fetch(`/api/meetings/sessions/${encodeURIComponent(session.session_id)}/fathom`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url }),
          });
          const data = await res.json();
          if (!data.error) { session.fathom_url = data.fathom_url; renderSummaryBox(session); }
        } catch (_) { /* ignore */ }
      };
      fathomRow.appendChild(input);
      fathomRow.appendChild(btn);
    }
    body.appendChild(fathomRow);
  }

  // ══════════════════════════════════════════════════════════════════════
  // Upcoming: calendar events + "Prepare" -> AI brief
  // ══════════════════════════════════════════════════════════════════════
  function fmtEventTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  // Upcoming cards are deliberately minimal — no inline brief/notes preview.
  // "Open in Live" is the only way to view them, so this list stays scannable
  // even once you've prepared a dozen meetings.
  function renderUpcomingCard(e) {
    const card = document.createElement("div");
    card.className = "meeting-upcoming-card";

    const head = document.createElement("div");
    head.className = "meeting-upcoming-head";
    const info = document.createElement("div");
    const title = document.createElement("div");
    title.className = "meeting-upcoming-title";
    title.textContent = e.title;
    const meta = document.createElement("div");
    meta.className = "meeting-upcoming-time";
    const attendeeCount = (e.attendees || []).length;
    meta.textContent = fmtEventTime(e.start) + (attendeeCount ? ` · ${attendeeCount} attendee${attendeeCount === 1 ? "" : "s"}` : "");
    info.appendChild(title);
    info.appendChild(meta);
    head.appendChild(info);

    const actions = document.createElement("div");
    actions.className = "meeting-upcoming-actions";

    function setActions(status, sessionId) {
      actions.innerHTML = "";
      if (status === "prepared" || status === "active" || status === "ended") {
        const label = document.createElement("span");
        label.className = "meeting-upcoming-status";
        label.textContent = status === "ended" ? "Ended" : status === "active" ? "Live" : "Prepared";
        actions.appendChild(label);

        const openBtn = document.createElement("button");
        openBtn.className = "meeting-quick-send";
        openBtn.type = "button";
        openBtn.textContent = "Open in Live";
        openBtn.onclick = () => { switchSubtab("live"); openSession(sessionId); };
        actions.appendChild(openBtn);
      } else {
        const prepBtn = document.createElement("button");
        prepBtn.className = "meeting-quick-send";
        prepBtn.type = "button";
        prepBtn.textContent = "Prepare";
        prepBtn.onclick = async () => {
          prepBtn.disabled = true;
          prepBtn.textContent = "Preparing…";
          try {
            const res = await fetch("/api/meetings/prepare", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                business: HermesBiz.current, event_id: e.event_id, title: e.title,
                start: e.start, description: e.description, attendees: e.attendees || [],
              }),
            });
            const data = await res.json();
            if (data.error) { alert(data.error); prepBtn.disabled = false; prepBtn.textContent = "Prepare"; return; }
            setActions("prepared", data.session_id);
          } catch (err) {
            alert("Couldn't prepare this meeting.");
            prepBtn.disabled = false;
            prepBtn.textContent = "Prepare";
          }
        };
        actions.appendChild(prepBtn);
      }
    }

    setActions(e.session_status, e.session_id);
    head.appendChild(actions);
    card.appendChild(head);
    return card;
  }

  async function loadUpcoming() {
    if (!upcomingListEl) return;
    const biz = HermesBiz.current;
    upcomingListEl.innerHTML = "";
    if (!biz || biz === "all") {
      if (upcomingEmptyEl) { upcomingEmptyEl.style.display = "block"; upcomingEmptyEl.textContent = "Select a business to see upcoming meetings."; }
      return;
    }
    if (upcomingEmptyEl) { upcomingEmptyEl.style.display = "block"; upcomingEmptyEl.textContent = "Loading upcoming meetings…"; }
    try {
      const res = await fetch(`/api/meetings/upcoming?business=${encodeURIComponent(biz)}`);
      const data = await res.json();
      if (data.error) {
        if (upcomingEmptyEl) { upcomingEmptyEl.style.display = "block"; upcomingEmptyEl.textContent = data.error; }
        return;
      }
      const events = data.events || [];
      if (upcomingEmptyEl) {
        upcomingEmptyEl.style.display = events.length ? "none" : "block";
        upcomingEmptyEl.textContent = "No upcoming meetings in the next 14 days.";
      }
      events.forEach((e) => upcomingListEl.appendChild(renderUpcomingCard(e)));
    } catch (err) {
      if (upcomingEmptyEl) { upcomingEmptyEl.style.display = "block"; upcomingEmptyEl.textContent = "Couldn't load upcoming meetings."; }
    }
  }

  // ---- History: list, reopen, delete past meetings (mirrors chat history) ----
  let lastLoadedSessions = [];

  function renderSessionsList() {
    if (!sessionsEl) return;
    sessionsEl.innerHTML = "";
    if (sessionsEmptyEl) {
      sessionsEmptyEl.style.display = lastLoadedSessions.length ? "none" : "block";
      sessionsEmptyEl.textContent = sessionQuery
        ? "No meetings match your search."
        : "No meetings yet for this business.";
    }
    lastLoadedSessions.forEach((s) => {
      const li = document.createElement("li");
      li.className = "convo" + (s.session_id === currentSessionId ? " active" : "");

      const openBtn = document.createElement("button");
      openBtn.className = "convo-open";
      openBtn.title = s.title;
      const snippetHtml = s.turn_count
        ? `<span class="convo-snippet">${s.turn_count} turn${s.turn_count === 1 ? "" : "s"}</span>`
        : "";
      openBtn.innerHTML = `<span class="convo-title">${escapeHtml(s.title)}</span>${snippetHtml}`;
      openBtn.onclick = () => { switchSubtab("live"); openSession(s.session_id); };

      const delBtn = document.createElement("button");
      delBtn.className = "convo-del";
      delBtn.title = "Delete meeting";
      delBtn.innerHTML =
        '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>';
      delBtn.onclick = (e) => { e.stopPropagation(); deleteSession(s.session_id); };

      li.appendChild(openBtn);
      li.appendChild(delBtn);
      sessionsEl.appendChild(li);
    });
  }

  async function loadSessions() {
    const biz = HermesBiz.current;
    if (!biz || biz === "all") { lastLoadedSessions = []; renderSessionsList(); return; }
    try {
      const res = await fetch(`/api/meetings/sessions?business=${encodeURIComponent(biz)}`);
      const data = await res.json();
      let list = data.sessions || [];
      if (sessionQuery) {
        const q = sessionQuery.toLowerCase();
        list = list.filter((s) => (s.title || "").toLowerCase().includes(q));
      }
      lastLoadedSessions = list;
    } catch (_) {
      lastLoadedSessions = [];
    }
    renderSessionsList();
  }

  async function openSession(id) {
    if (!id || id === currentSessionId) return;
    try {
      const res = await fetch(`/api/meetings/sessions/${encodeURIComponent(id)}`);
      if (!res.ok) return;
      const data = await res.json();
      currentSessionId = id;
      currentSession = data;
      // Reopening a past meeting reseeds the business lock, same as chat history.
      if (data.business && data.business !== HermesBiz.current) {
        lastBiz = data.business; // prevent refreshBizGate's change-detector from resetting us
        HermesBiz.set(data.business);
      }
      notesEl.value = (data.turns || [])
        .filter((t) => t.heard_text)
        .map((t) => t.heard_text)
        .join("\n");
      feedEl.innerHTML = "";
      (data.turns || []).forEach((t) => {
        if (t.bullets && t.bullets.length) {
          addFeedCard({ bullets: t.bullets, sources: t.sources, sourceText: t.heard_text });
        }
      });
      if (!feedEl.children.length) {
        feedEl.innerHTML = '<p class="hint meeting-feed-empty" id="meeting-feed-empty">No guidance recorded in this meeting.</p>';
      }
      renderBriefBox(data);
      renderAiNotesBox(data);
      renderSummaryBox(data);
      renderSessionsList();
      closeMobileNav();
    } catch (_) { /* ignore */ }
  }

  async function deleteSession(id) {
    try {
      await fetch(`/api/meetings/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
    } catch (_) { /* ignore */ }
    if (id === currentSessionId) resetMeetingState();
    await loadSessions();
  }

  let searchTimer = null;
  searchInput?.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      sessionQuery = searchInput.value.trim();
      loadSessions();
    }, 220);
  });
  searchInput?.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { searchInput.value = ""; sessionQuery = ""; loadSessions(); }
  });

  // ---- End meeting: AI summary + best-effort Fathom link ----
  endBtn?.addEventListener("click", async () => {
    if (!currentSessionId) { alert("Start or open a meeting first."); return; }
    if (listening) stopListening();
    endBtn.disabled = true;
    endBtn.textContent = "Ending…";
    try {
      const res = await fetch(`/api/meetings/sessions/${encodeURIComponent(currentSessionId)}/end`, { method: "POST" });
      const data = await res.json();
      if (!data.error) {
        currentSession = data;
        renderBriefBox(data);
        renderAiNotesBox(data);
        renderSummaryBox(data);
        loadSessions();
      }
    } catch (_) { /* ignore */ }
    endBtn.disabled = false;
    endBtn.textContent = "End meeting";
  });

  // ---- Clear notes/transcript + guidance (e.g. wiping test content while
  // iterating on a live session) — both buttons hit the same turns, since
  // notes and guidance are just two views of the same saved turn rows. ----
  async function clearTurns() {
    if (currentSessionId) {
      if (!confirm("Clear all notes, transcript & guidance for this meeting? This can't be undone.")) return;
      try {
        await fetch(`/api/meetings/sessions/${encodeURIComponent(currentSessionId)}/turns`, { method: "DELETE" });
      } catch (_) { /* ignore */ }
    }
    notesEl.value = "";
    feedEl.innerHTML = '<p class="hint meeting-feed-empty" id="meeting-feed-empty">Guidance will appear here as the meeting happens — grounded in this business\'s own emails and files, with sources cited.</p>';
    if (currentSessionId) loadSessions();
  }
  notesClearBtn?.addEventListener("click", clearTurns);
  guidanceClearBtn?.addEventListener("click", clearTurns);

  // ---- Guidance feed rendering ----
  // Every card's full text always stays visible — nothing is ever hidden
  // behind a click. The newest card just gets a highlight so it stands out;
  // decluttering comes from the prompt giving fewer/shorter/non-repeating
  // bullets and skipping empty turns, not from hiding real answers.
  function addFeedCard({ bullets, sources, error, sourceText }) {
    // Query fresh each time (not a cached top-level const) — resetMeetingState()
    // replaces the feed's innerHTML, which creates a NEW placeholder node.
    document.getElementById("meeting-feed-empty")?.remove();

    // Nothing new to say and nothing went wrong — the copilot is allowed to
    // say nothing; skip the card entirely instead of cluttering the feed with
    // an empty one every time nothing changed.
    if (!error && (!bullets || !bullets.length)) return;

    feedEl.querySelectorAll(".meeting-card-latest").forEach((c) => c.classList.remove("meeting-card-latest"));

    const card = document.createElement("div");
    card.className = "meeting-card meeting-card-latest" + (error ? " meeting-card-error" : "");

    const time = document.createElement("div");
    time.className = "meeting-card-time";
    time.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    card.appendChild(time);

    if (sourceText) {
      const heard = document.createElement("div");
      heard.className = "meeting-card-heard";
      heard.textContent = `“${sourceText}”`;
      card.appendChild(heard);
    }

    if (error) {
      const err = document.createElement("div");
      err.className = "meeting-card-errtext";
      err.textContent = error;
      card.appendChild(err);
    } else {
      const ul = document.createElement("ul");
      ul.className = "meeting-bullets";
      bullets.forEach((b) => {
        const li = document.createElement("li");
        li.textContent = b;
        ul.appendChild(li);
      });
      card.appendChild(ul);

      if (sources && sources.length) {
        const srcRow = document.createElement("div");
        srcRow.className = "meeting-sources";
        sources.slice(0, 4).forEach((s) => {
          const chip = document.createElement(s.download_url ? "a" : "span");
          chip.className = "meeting-source-chip";
          chip.textContent = `${s.source_system || "record"}: ${(s.title || "untitled").slice(0, 34)}`;
          if (s.download_url) {
            chip.href = s.download_url;
            chip.target = "_blank";
            chip.rel = "noopener";
          }
          srcRow.appendChild(chip);
        });
        card.appendChild(srcRow);
      }
    }

    feedEl.appendChild(card);
    feedEl.scrollTop = feedEl.scrollHeight;
  }

  function appendNote(text) {
    if (!text) return;
    const stamp = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const line = `[${stamp}] ${text}`;
    notesEl.value = notesEl.value ? `${notesEl.value}\n${line}` : line;
    notesEl.scrollTop = notesEl.scrollHeight;
  }

  // ---- Ask the copilot for guidance on one utterance/note ----
  async function askCopilot(latestText) {
    const biz = HermesBiz.current;
    if (!biz || biz === "all") return;
    try {
      const res = await fetch("/api/meetings/copilot", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          business: biz,
          latest_text: latestText,
          notes_so_far: notesEl.value,
          session_id: currentSessionId,
        }),
      });
      const data = await res.json();
      if (data.error) {
        addFeedCard({ error: data.error, sourceText: latestText });
        return;
      }
      // Server is authoritative — creates the session on the first turn.
      const isNewSession = data.session_id && data.session_id !== currentSessionId;
      currentSessionId = data.session_id || currentSessionId;
      addFeedCard({ bullets: data.bullets, sources: data.sources, sourceText: latestText });
      if (isNewSession) loadSessions(); // first turn of a meeting — show it in history now
    } catch (err) {
      addFeedCard({ error: "Couldn't reach the copilot: " + (err && err.message ? err.message : err), sourceText: latestText });
    }
  }

  // ---- Quick typed-note flow (no voice needed) ----
  async function sendQuickNote() {
    const text = (quickInput.value || "").trim();
    if (!text) return;
    quickInput.value = "";
    appendNote(text);
    await askCopilot(text);
  }
  quickSend?.addEventListener("click", sendQuickNote);
  quickInput?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendQuickNote();
    }
  });

  // ══════════════════════════════════════════════════════════════════════
  // Continuous listening (adapted from voice.js's VAD loop — no playback,
  // no barge-in needed since nothing is spoken back).
  // ══════════════════════════════════════════════════════════════════════
  const SILENCE_MS = 1400;
  const MIN_SPEECH_MS = 300;
  const MAX_UTTER_MS = 30000;
  const TICK_MS = 16;

  let stream = null, audioCtx = null, analyser = null, recorder = null, chunks = [];
  let rafTimer = null;
  let noiseFloor = 0.01, smoothRms = 0, speechStartedAt = 0, lastVoiceAt = 0, recStartedAt = 0;
  let micPhase = "Not listening";

  function setListenUI(isListening) {
    listening = isListening;
    [listenBtn, listenBtnM].forEach((b) => b && b.classList.toggle("is-listening", isListening));
    if (listenLabel) listenLabel.textContent = isListening ? "Stop Listening" : "Start Listening";
  }

  // Status text combines two independent things: the mic's own phase
  // (Listening/Hearing/Not listening) and how many closed utterances are
  // still waiting on transcription+guidance — mic capture never waits on
  // this queue, so both can be true at once ("Listening… · Processing 2").
  function renderStatus() {
    if (!statusEl) return;
    statusEl.textContent = queueDepth > 0 ? `${micPhase} · Processing ${queueDepth}` : micPhase;
  }

  function pickMime() {
    const types = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];
    for (const t of types) {
      if (window.MediaRecorder && MediaRecorder.isTypeSupported(t)) return t;
    }
    return "";
  }

  async function startListening() {
    if (listening) return;
    const biz = HermesBiz.current;
    if (!biz || biz === "all") { alert("Select a specific business first."); return; }
    switchSubtab("live"); // starting to listen means you're in a live meeting now
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (err) {
      alert("Microphone access is required to listen.\n\n" + err);
      return;
    }
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") await audioCtx.resume();
    const source = audioCtx.createMediaStreamSource(stream);
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 1024;
    source.connect(analyser);

    setListenUI(true);
    beginUtterance();
  }

  function stopListening() {
    if (rafTimer) { clearTimeout(rafTimer); rafTimer = null; }
    if (recorder && recorder.state !== "inactive") {
      try { recorder.onstop = null; recorder.stop(); } catch (_) {}
    }
    recorder = null;
    if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
    if (audioCtx) { try { audioCtx.close(); } catch (_) {} audioCtx = null; }
    analyser = null;
    setListenUI(false);
    micPhase = "Not listening";
    renderStatus(); // note: any already-queued utterances keep draining in the background
  }

  function toggleListening() {
    if (listening) stopListening(); else startListening();
  }
  listenBtn?.addEventListener("click", toggleListening);
  listenBtnM?.addEventListener("click", toggleListening);

  function beginUtterance() {
    if (!listening && !stream) return;
    chunks = [];
    const mime = pickMime();
    try {
      recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
    } catch (err) {
      setListenUI(false);
      micPhase = "Recorder error";
      renderStatus();
      return;
    }
    recorder.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
    recorder.onstop = onUtteranceComplete;
    recorder.start(100);

    speechStartedAt = 0; lastVoiceAt = 0; recStartedAt = Date.now();
    micPhase = "Listening…";
    renderStatus();
    monitor();
  }

  function monitor() {
    if (!stream || !analyser || recorder?.state === "inactive") return;
    const buf = new Uint8Array(analyser.fftSize);
    analyser.getByteTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) { const v = (buf[i] - 128) / 128; sum += v * v; }
    const rms = Math.sqrt(sum / buf.length);
    smoothRms = smoothRms * 0.85 + rms * 0.15;

    const startThresh = Math.max(0.024, noiseFloor * 2.0 + 0.015);
    const keepThresh = Math.max(0.012, noiseFloor * 1.25 + 0.006);
    const now = Date.now();
    const voiced = speechStartedAt ? smoothRms > keepThresh : smoothRms > startThresh;

    if (voiced) {
      if (!speechStartedAt) { speechStartedAt = now; micPhase = "Hearing…"; renderStatus(); }
      lastVoiceAt = now;
    } else if (!speechStartedAt) {
      noiseFloor = noiseFloor * 0.95 + smoothRms * 0.05;
    }

    const spokeLongEnough = speechStartedAt && now - speechStartedAt > MIN_SPEECH_MS;
    const silentNow = speechStartedAt && now - lastVoiceAt > SILENCE_MS;
    const tooLong = now - recStartedAt > MAX_UTTER_MS;

    if ((spokeLongEnough && silentNow) || (speechStartedAt && tooLong)) {
      if (recorder && recorder.state !== "inactive") recorder.stop();
      return;
    }
    if (!speechStartedAt && now - recStartedAt > MAX_UTTER_MS) {
      if (recorder && recorder.state !== "inactive") {
        recorder.onstop = () => { if (stream) beginUtterance(); };
        recorder.stop();
      }
      return;
    }
    rafTimer = setTimeout(monitor, TICK_MS);
  }

  function onUtteranceComplete() {
    if (!stream) return; // stopListening() already tore everything down
    const blob = new Blob(chunks, { type: recorder ? recorder.mimeType : "audio/webm" });
    const hadSpeech = !!(blob.size && speechStartedAt);
    chunks = [];
    // Resume capturing the NEXT segment immediately, before this one is even
    // transcribed — the mic must never sit idle while the copilot round-trip
    // (STT + Pinecone + Claude) for the previous segment is in flight, or
    // whatever's said during that window is lost for good.
    beginUtterance();
    if (hadSpeech) enqueueUtterance(blob);
  }

  // ---- Utterance processing queue -----------------------------------------
  // Decoupled from mic capture on purpose: beginUtterance() above has already
  // restarted recording by the time anything here runs. Drained FIFO (not in
  // parallel) so notes/guidance stay in the order they were actually spoken —
  // each guidance call needs notes_so_far to reflect everything said before it.
  const utteranceQueue = [];
  let processingQueue = false;
  let queueDepth = 0;

  function enqueueUtterance(blob) {
    utteranceQueue.push(blob);
    processQueue();
  }

  async function processQueue() {
    if (processingQueue) return;
    processingQueue = true;
    while (utteranceQueue.length) {
      queueDepth = utteranceQueue.length;
      renderStatus();
      const blob = utteranceQueue.shift();
      try {
        const text = await transcribeBlob(blob);
        if (text) {
          appendNote(text);
          await askCopilot(text);
        }
      } catch (err) {
        addFeedCard({ error: "Transcription error: " + (err && err.message ? err.message : err) });
      }
    }
    queueDepth = 0;
    processingQueue = false;
    renderStatus();
  }

  async function transcribeBlob(blob) {
    const body = new FormData();
    const ext = (blob.type.split(";")[0].split("/")[1] || "webm");
    body.append("audio", blob, "speech." + ext);
    body.append("context", "meeting"); // ambient room audio — different STT prompt than one-off voice commands
    const res = await fetch("/api/voice/transcribe", { method: "POST", body });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    return (data.text || "").trim();
  }
})();
