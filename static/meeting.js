// Meeting Copilot — private, real-time guidance during a live meeting.
//
// Flow while "listening":
//   mic --(VAD detects a pause)--> /api/voice/transcribe --> append to notes
//     --> /api/meetings/copilot (grounded in this business's own indexed
//         emails/files) --> render bullet-point guidance --> keep listening.
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

  if (!listenBtn || !notesEl || !feedEl) return; // page not present

  let initialized = false;
  let listening = false;
  let currentSessionId = null;
  let sessionQuery = "";

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
    notesEl.value = "";
    feedEl.innerHTML = '<p class="hint meeting-feed-empty" id="meeting-feed-empty">Guidance will appear here as the meeting happens — grounded in this business\'s own emails and files, with sources cited.</p>';
    if (statusEl) statusEl.textContent = listening ? "Listening…" : "Not listening";
    renderSessionsList(); // just re-highlight active (none) — list itself unchanged
  }
  newBtn?.addEventListener("click", () => {
    if (listening) stopListening();
    resetMeetingState();
  });

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
      openBtn.onclick = () => openSession(s.session_id);

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

  // ---- Guidance feed rendering ----
  function addFeedCard({ bullets, sources, error, sourceText }) {
    // Query fresh each time (not the cached top-level const) — resetMeetingState()
    // replaces the feed's innerHTML, which creates a NEW placeholder node.
    document.getElementById("meeting-feed-empty")?.remove();
    const card = document.createElement("div");
    card.className = "meeting-card" + (error ? " meeting-card-error" : "");

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
    } else if (bullets && bullets.length) {
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

  function setListenUI(isListening, label) {
    listening = isListening;
    [listenBtn, listenBtnM].forEach((b) => b && b.classList.toggle("is-listening", isListening));
    if (listenLabel) listenLabel.textContent = isListening ? "Stop Listening" : "Start Listening";
    if (statusEl) statusEl.textContent = label || (isListening ? "Listening…" : "Not listening");
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

    setListenUI(true, "Listening…");
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
    setListenUI(false, "Not listening");
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
      setListenUI(false, "Recorder error");
      return;
    }
    recorder.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
    recorder.onstop = onUtteranceComplete;
    recorder.start(100);

    speechStartedAt = 0; lastVoiceAt = 0; recStartedAt = Date.now();
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
      if (!speechStartedAt) { speechStartedAt = now; setListenUI(true, "Hearing…"); }
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

  async function onUtteranceComplete() {
    if (!stream) return; // stopListening() already tore everything down
    const blob = new Blob(chunks, { type: recorder ? recorder.mimeType : "audio/webm" });
    chunks = [];
    if (!blob.size || !speechStartedAt) { beginUtterance(); return; }

    setListenUI(true, "Transcribing…");
    try {
      const text = await transcribeBlob(blob);
      if (text) {
        appendNote(text);
        setListenUI(true, "Thinking…");
        await askCopilot(text);
      }
    } catch (err) {
      addFeedCard({ error: "Transcription error: " + (err && err.message ? err.message : err) });
    }
    if (stream) { setListenUI(true, "Listening…"); beginUtterance(); }
  }

  async function transcribeBlob(blob) {
    const body = new FormData();
    const ext = (blob.type.split(";")[0].split("/")[1] || "webm");
    body.append("audio", blob, "speech." + ext);
    const res = await fetch("/api/voice/transcribe", { method: "POST", body });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    return (data.text || "").trim();
  }
})();
