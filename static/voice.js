// Hermes voice client — talks ONLY to our own Pipecat service (never to Groq).
// Wire format: raw little-endian PCM16 mono. Mic -> 16 kHz out; bot audio in at
// 24 kHz. A small JSON text frame {"type":"interrupt"} clears playback (barge-in).
(function () {
  const IN_RATE = 16000;   // must match voice_server IN_SAMPLE_RATE
  const OUT_RATE = 24000;  // must match voice_server OUT_SAMPLE_RATE
  const WS_URL = window.HERMES_VOICE_WS;

  const btn = document.getElementById("voice-btn");
  const statusEl = document.getElementById("voice-status");
  if (!btn) return;

  let ws = null;
  let micStream = null;
  let inCtx = null;       // capture AudioContext
  let outCtx = null;      // playback AudioContext
  let processor = null;
  let sources = [];       // scheduled playback nodes (for barge-in stop)
  let playHead = 0;
  let active = false;

  function setStatus(state, text) {
    if (statusEl) {
      statusEl.dataset.state = state;
      statusEl.textContent = text;
      statusEl.style.display = "flex";
    }
  }
  function hideStatus() {
    if (statusEl) statusEl.style.display = "none";
  }

  // ---- mic capture -> PCM16 @ IN_RATE ----
  function downsample(input, inRate, outRate) {
    if (outRate === inRate) return input;
    const ratio = inRate / outRate;
    const outLen = Math.floor(input.length / ratio);
    const out = new Float32Array(outLen);
    for (let i = 0; i < outLen; i++) out[i] = input[Math.floor(i * ratio)];
    return out;
  }
  function floatToPCM16(f32) {
    const out = new Int16Array(f32.length);
    for (let i = 0; i < f32.length; i++) {
      const s = Math.max(-1, Math.min(1, f32[i]));
      out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return out;
  }

  // ---- playback of incoming PCM16 @ OUT_RATE ----
  function playPCM16(arrayBuf) {
    const i16 = new Int16Array(arrayBuf);
    if (!i16.length) return;
    const f32 = new Float32Array(i16.length);
    for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 0x8000;

    const buf = outCtx.createBuffer(1, f32.length, OUT_RATE);
    buf.copyToChannel(f32, 0);
    const src = outCtx.createBufferSource();
    src.buffer = buf;
    src.connect(outCtx.destination);

    const now = outCtx.currentTime;
    if (playHead < now) playHead = now;
    src.start(playHead);
    playHead += buf.duration;
    sources.push(src);
    src.onended = () => { sources = sources.filter((s) => s !== src); };

    setStatus("speaking", "Hermes is speaking…");
  }

  function stopPlayback() {
    sources.forEach((s) => { try { s.stop(); } catch (_) {} });
    sources = [];
    playHead = 0;
    if (active) setStatus("listening", "Listening…");
  }

  // periodic state: revert to "listening" once the bot finishes speaking
  setInterval(() => {
    if (active && outCtx && sources.length === 0 &&
        statusEl && statusEl.dataset.state === "speaking") {
      setStatus("listening", "Listening…");
    }
  }, 300);

  async function start() {
    if (!WS_URL) { setStatus("error", "Voice service not configured."); return; }
    setStatus("connecting", "Connecting…");
    try {
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
    } catch (err) {
      if (err && err.name === "NotAllowedError") setStatus("error", "Microphone permission denied.");
      else if (err && err.name === "NotFoundError") setStatus("error", "No microphone found.");
      else setStatus("error", "Could not access microphone.");
      return;
    }

    inCtx = new (window.AudioContext || window.webkitAudioContext)();
    outCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (outCtx.state === "suspended") await outCtx.resume();

    ws = new WebSocket(WS_URL);
    ws.binaryType = "arraybuffer";

    ws.onopen = () => {
      active = true;
      btn.classList.add("on");
      btn.textContent = "■ End voice";
      setStatus("listening", "Listening…");

      const source = inCtx.createMediaStreamSource(micStream);
      processor = inCtx.createScriptProcessor(4096, 1, 1);
      source.connect(processor);
      processor.connect(inCtx.destination);
      processor.onaudioprocess = (e) => {
        if (!ws || ws.readyState !== WebSocket.OPEN) return;
        const f32 = e.inputBuffer.getChannelData(0);
        const ds = downsample(f32, inCtx.sampleRate, IN_RATE);
        ws.send(floatToPCM16(ds).buffer);
      };
    };

    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === "interrupt") stopPlayback(); // barge-in
        } catch (_) {}
        return;
      }
      playPCM16(ev.data); // binary = audio
    };

    ws.onerror = () => setStatus("error", "Connection error.");
    ws.onclose = () => { if (active) stop(); };
  }

  function stop() {
    active = false;
    btn.classList.remove("on");
    btn.textContent = "🎙 Start voice";
    hideStatus();
    stopPlayback();
    try { processor && processor.disconnect(); } catch (_) {}
    try { micStream && micStream.getTracks().forEach((t) => t.stop()); } catch (_) {}
    try { inCtx && inCtx.close(); } catch (_) {}
    try { outCtx && outCtx.close(); } catch (_) {}
    try { ws && ws.close(); } catch (_) {}
    ws = micStream = inCtx = outCtx = processor = null;
  }

  btn.addEventListener("click", () => (active ? stop() : start()));
})();
