// Hermes hands-free voice call.
//
// Flow per turn:  mic --(VAD detects you stopped)--> /api/voice/transcribe
//   --> /api/chat (the SAME Claude + Maton agent as the typed chat)
//   --> /api/voice/speak (TTS) --> play aloud --> listen again.
//
// Reuses addMessage / renderMarkdown / renderThinking / renderToolTrace /
// highlightCode / sessionId from app.js so the spoken conversation also shows
// up in the normal chat thread and in the conversation-history sidebar.
(() => {
  const startBtn = document.getElementById("voice-btn");
  const callEl = document.getElementById("call");
  if (!startBtn || !callEl) return;

  const orb = document.getElementById("call-orb");
  const statusEl = document.getElementById("call-status");
  const captionEl = document.getElementById("call-caption");
  const muteBtn = document.getElementById("call-mute");
  const endBtn = document.getElementById("call-end");
  const voiceSelect = document.getElementById("voice-select");

  // ---- Voice selection (persisted) ----
  const savedVoice = localStorage.getItem("hermes_voice");
  if (voiceSelect) {
    if (savedVoice) voiceSelect.value = savedVoice;
    voiceSelect.addEventListener("change", () => {
      localStorage.setItem("hermes_voice", voiceSelect.value);
    });
  }
  const currentVoice = () =>
    (voiceSelect && voiceSelect.value) || savedVoice || undefined;

  // ---- Tunables (VAD) ----
  // Endpointing waits for a real pause and uses two thresholds (hysteresis):
  // a higher one to START a turn, a lower one to KEEP it open — so trailing-off
  // words and brief mid-sentence breaths don't get cut off as "you stopped".
  const SILENCE_MS = 1600;    // continuous quiet after speech => end of your turn
  const MIN_SPEECH_MS = 250;  // ignore tiny blips / clicks
  const MAX_UTTER_MS = 45000; // hard cap on one utterance (~45s)
  const TICK_MS = 16;         // ~60fps analyser loop

  // ---- Barge-in (interrupt the assistant while it is speaking) ----
  // High threshold + required duration so the TTS bleeding into the mic
  // (despite echo cancellation) doesn't falsely trigger an interrupt.
  const BARGE_RMS = 0.14;     // you must be clearly louder than the playback
  const BARGE_MS = 320;       // sustained this long => interrupt
  const BARGE_GRACE_MS = 350; // ignore the first moments of playback

  // ---- Call state ----
  let active = false;
  let muted = false;
  let phase = "idle"; // idle | listening | thinking | speaking
  let stream = null;
  let audioCtx = null;
  let analyser = null;
  let recorder = null;
  let chunks = [];
  let rafTimer = null;
  let player = null;
  let playerUrl = null;
  let bargeTimer = null;
  let playStartedAt = 0;
  let bargeStartedAt = 0;

  // VAD bookkeeping
  let noiseFloor = 0.01;
  let smoothRms = 0;
  let speechStartedAt = 0;
  let lastVoiceAt = 0;
  let recStartedAt = 0;

  const setPhase = (p) => {
    phase = p;
    orb.dataset.state = muted ? "muted" : p;
  };

  const pickMime = () => {
    const types = [
      "audio/webm;codecs=opus",
      "audio/webm",
      "audio/ogg;codecs=opus",
      "audio/mp4",
    ];
    for (const t of types) {
      if (window.MediaRecorder && MediaRecorder.isTypeSupported(t)) return t;
    }
    return "";
  };

  async function startCall() {
    if (active) return;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (err) {
      alert("Microphone access is required for voice mode.\n\n" + err);
      return;
    }

    active = true;
    muted = false;
    muteBtn.classList.remove("is-muted");
    muteBtn.textContent = "Mute";
    callEl.classList.add("open");
    callEl.setAttribute("aria-hidden", "false");

    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") await audioCtx.resume();
    const source = audioCtx.createMediaStreamSource(stream);
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 1024;
    source.connect(analyser);

    setPhase("listening");
    statusEl.textContent = "Listening…";
    captionEl.textContent = "Go ahead — I'm listening.";
    beginListening();
  }

  function endCall() {
    active = false;
    stopRaf();
    stopPlayback();
    if (recorder && recorder.state !== "inactive") {
      try { recorder.onstop = null; recorder.stop(); } catch (_) {}
    }
    recorder = null;
    if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
    if (audioCtx) { try { audioCtx.close(); } catch (_) {} audioCtx = null; }
    analyser = null;
    callEl.classList.remove("open");
    callEl.setAttribute("aria-hidden", "true");
    setPhase("idle");
  }

  function stopRaf() {
    if (rafTimer) { clearTimeout(rafTimer); rafTimer = null; }
  }

  function stopBarge() {
    if (bargeTimer) { clearTimeout(bargeTimer); bargeTimer = null; }
  }

  function stopPlayback() {
    stopBarge();
    if (player) {
      try { player.onended = null; player.onerror = null; player.pause(); } catch (_) {}
    }
    if (playerUrl) { try { URL.revokeObjectURL(playerUrl); } catch (_) {} playerUrl = null; }
    player = null;
  }

  // ---- Listening with voice-activity detection ----
  function beginListening() {
    if (!active || muted) return;
    setPhase("listening");
    statusEl.textContent = "Listening…";

    chunks = [];
    const mime = pickMime();
    try {
      recorder = mime ? new MediaRecorder(stream, { mimeType: mime })
                      : new MediaRecorder(stream);
    } catch (err) {
      captionEl.textContent = "Recorder error: " + err;
      return;
    }
    recorder.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
    recorder.onstop = onUtteranceComplete;
    recorder.start(100); // gather audio in 100ms slices

    speechStartedAt = 0;
    lastVoiceAt = 0;
    recStartedAt = Date.now();
    monitor();
  }

  function monitor() {
    if (!active || phase !== "listening" || !analyser) return;
    const buf = new Uint8Array(analyser.fftSize);
    analyser.getByteTimeDomainData(buf);

    // RMS of the centered waveform (0..~1).
    let sum = 0;
    for (let i = 0; i < buf.length; i++) {
      const v = (buf[i] - 128) / 128;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / buf.length);
    smoothRms = smoothRms * 0.85 + rms * 0.15;

    // Two thresholds: high to begin a turn, low to keep it alive (hysteresis).
    const startThresh = Math.max(0.024, noiseFloor * 2.0 + 0.015);
    const keepThresh = Math.max(0.012, noiseFloor * 1.25 + 0.006);
    const now = Date.now();
    // Before you've started: only loud sound counts. After: any voiced sound
    // above the lower threshold keeps the turn open.
    const voiced = speechStartedAt ? smoothRms > keepThresh : smoothRms > startThresh;

    if (voiced) {
      if (!speechStartedAt) speechStartedAt = now;
      lastVoiceAt = now;
      orb.style.setProperty("--level", Math.min(1, smoothRms * 6).toFixed(2));
    } else {
      // Adapt the noise floor only while waiting for you to start talking,
      // never during a pause mid-utterance (that would cut you off early).
      if (!speechStartedAt) noiseFloor = noiseFloor * 0.95 + smoothRms * 0.05;
      orb.style.setProperty("--level", "0");
    }

    const spokeLongEnough = speechStartedAt && now - speechStartedAt > MIN_SPEECH_MS;
    const silentNow = speechStartedAt && now - lastVoiceAt > SILENCE_MS;
    const tooLong = now - recStartedAt > MAX_UTTER_MS;

    if ((spokeLongEnough && silentNow) || (speechStartedAt && tooLong)) {
      stopRaf();
      if (recorder && recorder.state !== "inactive") recorder.stop();
      return;
    }

    // No speech for a long while: recycle the recorder so the blob stays small.
    if (!speechStartedAt && now - recStartedAt > MAX_UTTER_MS) {
      stopRaf();
      if (recorder && recorder.state !== "inactive") {
        recorder.onstop = () => { if (active && !muted) beginListening(); };
        recorder.stop();
      }
      return;
    }

    rafTimer = setTimeout(monitor, TICK_MS);
  }

  async function onUtteranceComplete() {
    if (!active) return;
    const blob = new Blob(chunks, { type: recorder ? recorder.mimeType : "audio/webm" });
    chunks = [];
    if (!blob.size || !speechStartedAt) { beginListening(); return; }

    setPhase("thinking");
    statusEl.textContent = "Thinking…";
    captionEl.textContent = "…";

    try {
      // 1) Speech -> text
      const transcript = await transcribe(blob);
      if (!transcript) { resumeAfterTurn(); return; }
      captionEl.textContent = `“${transcript}”`;
      addMessage("user", renderUserText(transcript));

      // 2) Run the SAME agent the typed chat uses
      const reply = await runAgent(transcript);

      // 3) Text -> speech
      await speak(reply);
    } catch (err) {
      captionEl.textContent = "Error: " + (err && err.message ? err.message : err);
      resumeAfterTurn();
    }
  }

  async function transcribe(blob) {
    const body = new FormData();
    const ext = (blob.type.split(";")[0].split("/")[1] || "webm");
    body.append("audio", blob, "speech." + ext);
    const res = await fetch("/api/voice/transcribe", { method: "POST", body });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    return (data.text || "").trim();
  }

  async function runAgent(message) {
    const body = new FormData();
    body.append("session_id", sessionId);
    body.append("entity_id", entityIdForSend());
    body.append("message", message);
    body.append("voice", "1"); // ask the agent for a short, spoken-style reply
    const res = await fetch("/api/chat", { method: "POST", body });
    const data = await res.json();
    if (data.error) throw new Error(data.error);

    const wrap = addMessage("assistant", "");
    const bubble = wrap.querySelector(".bubble");
    bubble.innerHTML =
      renderMarkdown(data.reply) + renderThinking(data.thinking) + renderToolTrace(data.tool_calls);
    highlightCode(bubble);
    // Lock (or reaffirm) this session's business — same rule as the typed chat.
    HermesBiz.setLock(sessionId, data.entity_id || "all");
    if (window.updateLockNote) window.updateLockNote();
    // The spoken turn was recorded server-side — refresh the history sidebar.
    if (window.hermesReloadConversations) window.hermesReloadConversations();
    return data.reply || "";
  }

  async function speak(replyText) {
    const clean = (replyText || "").trim();
    if (!clean) { resumeAfterTurn(); return; }

    let res;
    try {
      res = await fetch("/api/voice/speak", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: clean, voice: currentVoice() }),
      });
    } catch (err) {
      captionEl.textContent = "Couldn't reach the voice service.";
      resumeAfterTurn();
      return;
    }

    if (!res.ok) {
      let msg = "Voice playback unavailable (the reply is shown in the chat).";
      try { const j = await res.json(); if (j.error) msg = j.error; } catch (_) {}
      captionEl.textContent = msg;
      resumeAfterTurn(1600);
      return;
    }

    const audioBlob = await res.blob();
    playerUrl = URL.createObjectURL(audioBlob);
    player = new Audio(playerUrl);

    setPhase("speaking");
    statusEl.textContent = "Speaking…";
    captionEl.textContent = shorten(clean) + "   (just talk to interrupt)";

    const done = () => { stopPlayback(); resumeAfterTurn(); };
    player.onended = done;
    player.onerror = done;
    try {
      await player.play();
      playStartedAt = Date.now();
      bargeStartedAt = 0;
      monitorBargeIn();
    } catch (_) { done(); }
  }

  // While the assistant is speaking, listen for the user cutting in.
  function monitorBargeIn() {
    if (!active || muted || phase !== "speaking" || !analyser || !player) return;

    const buf = new Uint8Array(analyser.fftSize);
    analyser.getByteTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) {
      const v = (buf[i] - 128) / 128;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / buf.length);
    const now = Date.now();

    if (now - playStartedAt > BARGE_GRACE_MS && rms > BARGE_RMS) {
      if (!bargeStartedAt) bargeStartedAt = now;
      if (now - bargeStartedAt > BARGE_MS) {
        // User cut in — stop talking and start listening to them immediately.
        stopPlayback();
        if (active && !muted) beginListening();
        return;
      }
    } else {
      bargeStartedAt = 0;
    }
    bargeTimer = setTimeout(monitorBargeIn, TICK_MS);
  }

  function resumeAfterTurn(delay = 250) {
    if (!active) return;
    if (muted) { setPhase("listening"); statusEl.textContent = "Muted"; return; }
    setTimeout(() => { if (active && !muted) beginListening(); }, delay);
  }

  function shorten(s) {
    s = s.replace(/\s+/g, " ").trim();
    return s.length > 240 ? s.slice(0, 240).trim() + "…" : s;
  }

  // ---- Controls ----
  function toggleMute() {
    muted = !muted;
    muteBtn.classList.toggle("is-muted", muted);
    muteBtn.textContent = muted ? "Unmute" : "Mute";
    if (muted) {
      stopRaf();
      stopBarge();
      if (recorder && recorder.state !== "inactive") {
        try { recorder.onstop = null; recorder.stop(); } catch (_) {}
      }
      setPhase("listening");
      orb.dataset.state = "muted";
      statusEl.textContent = "Muted";
      captionEl.textContent = "Mic off. Tap Unmute to keep talking.";
    } else if (phase !== "speaking" && phase !== "thinking") {
      beginListening();
    }
  }

  startBtn.addEventListener("click", startCall);
  endBtn.addEventListener("click", endCall);
  muteBtn.addEventListener("click", toggleMute);
  // Esc ends the call.
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && active) endCall();
  });
})();
