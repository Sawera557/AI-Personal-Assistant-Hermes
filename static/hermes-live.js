/* Hermes Live — real-time voice on the Home orb (Gemini Live).

   Click the orb: the server mints a single-use token (/api/voice/live/session),
   the browser opens a WebSocket straight to Gemini Live, streams 16 kHz mic PCM
   up and plays the 24 kHz native-audio reply as it arrives. Talking over Hermes
   interrupts it (Gemini's own voice-activity detection does the barge-in).

   Gemini is only the voice. Its `ask_hermes` tool is answered here by calling
   /api/chat — the same Claude agent, RAG and app connections as typed chat —
   so every fact it speaks comes from Hermes, never from Gemini's imagination. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const IN_RATE = 16000, OUT_RATE = 24000;

  // Mic → 16 kHz Int16 chunks of 100 ms, plus an RMS level for the orb. The
  // box-filter downsample doubles as a cheap low-pass against aliasing.
  const WORKLET = `
    class HermesMic extends AudioWorkletProcessor {
      constructor() {
        super();
        this.ratio = sampleRate / ${IN_RATE};
        this.out = new Int16Array(1600); this.n = 0;
        this.acc = 0; this.w = 0; this.sq = 0;
      }
      process(inputs) {
        const ch = inputs[0] && inputs[0][0];
        if (!ch) return true;
        for (let i = 0; i < ch.length; i++) {
          this.acc += ch[i]; this.w++;
          if (this.w >= this.ratio) {
            const s = Math.max(-1, Math.min(1, this.acc / this.w));
            this.out[this.n++] = s < 0 ? s * 0x8000 : s * 0x7fff;
            this.sq += s * s;
            this.acc = 0; this.w -= this.ratio;
            if (this.n === this.out.length) {
              this.port.postMessage({ pcm: this.out.buffer, level: Math.sqrt(this.sq / this.n) }, [this.out.buffer]);
              this.out = new Int16Array(1600); this.n = 0; this.sq = 0;
            }
          }
        }
        return true;
      }
    }
    registerProcessor("hermes-mic", HermesMic);`;

  function b64FromBuffer(buf) {
    const bytes = new Uint8Array(buf);
    let s = "";
    for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(s);
  }
  function int16FromB64(b64) {
    const bin = atob(b64), bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new Int16Array(bytes.buffer);
  }

  let S = null; // the live session; null when idle

  /* ── UI ──────────────────────────────────────────────────────────────── */
  const STATE_TEXT = {
    connecting: "Connecting", listening: "Listening", speaking: "Speaking",
    thinking: "Checking Hermes", error: "Voice error",
  };

  function ui() {
    let el = $("hx-live");
    if (el) return el;
    el = document.createElement("div");
    el.id = "hx-live";
    el.className = "hx-live";
    el.setAttribute("role", "status");
    el.setAttribute("aria-live", "polite");
    el.innerHTML =
      '<div class="hx-live-head"><i class="hx-live-dot"></i><span class="hx-live-state"></span>' +
      '<span class="hx-live-hint">talk naturally · interrupt any time</span>' +
      '<button type="button" class="hx-live-stop" aria-label="End voice session">End</button></div>' +
      '<div class="hx-live-log"></div>';
    el.querySelector(".hx-live-stop").addEventListener("click", stop);
    document.body.appendChild(el);
    return el;
  }

  function setState(st, detail) {
    if (S) S.state = st;
    const el = ui();
    el.dataset.state = st;
    el.querySelector(".hx-live-state").textContent = detail || STATE_TEXT[st] || st;
    const rings = $("hud-rings");
    if (rings) {
      rings.classList.toggle("hx-live-on", st !== "idle");
      rings.dataset.live = st;
    }
    const lbl = $("hx-live-core");
    if (lbl) lbl.textContent = STATE_TEXT[st] || "";
    el.classList.toggle("open", st !== "idle");
  }

  // One line per speaker turn; transcription arrives in fragments.
  function say(who, text) {
    if (!S || !text) return;
    const log = ui().querySelector(".hx-live-log");
    if (!S.line || S.line.who !== who) {
      const row = document.createElement("div");
      row.className = "hx-live-line " + who;
      row.innerHTML = `<b>${who === "you" ? "You" : "Hermes"}</b><span></span>`;
      log.appendChild(row);
      while (log.children.length > 6) log.firstChild.remove();
      S.line = { who, span: row.querySelector("span") };
    }
    S.line.span.textContent += text;
    log.scrollTop = log.scrollHeight;
  }

  /* ── Audio out ───────────────────────────────────────────────────────── */
  function play(int16) {
    const ctx = S.out;
    const f = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) f[i] = int16[i] / 0x8000;
    const buf = ctx.createBuffer(1, f.length, OUT_RATE);
    buf.copyToChannel(f, 0);
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(S.analyser);
    const at = Math.max(S.playAt, ctx.currentTime + 0.04);
    src.start(at);
    S.playAt = at + buf.duration;
    S.sources.add(src);
    src.onended = () => {
      S && S.sources.delete(src);
      if (S && !S.sources.size && S.state === "speaking") setState("listening");
    };
  }
  function flush() { // barge-in: drop everything still queued
    S.sources.forEach((s) => { try { s.stop(); } catch (_) {} });
    S.sources.clear();
    S.playAt = 0;
  }

  /* ── Orb energy follows whoever is talking ───────────────────────────── */
  function meter() {
    if (!S) return;
    const rings = $("hud-rings");
    let lvl = S.micLevel;
    if (S.sources.size) {
      const a = S.analyser, d = S.meterBuf;
      a.getFloatTimeDomainData(d);
      let sq = 0; for (let i = 0; i < d.length; i++) sq += d[i] * d[i];
      lvl = Math.sqrt(sq / d.length);
    }
    if (rings) {
      rings._energy && rings._energy(Math.min(1.8, 0.35 + lvl * 6));
      rings.style.setProperty("--live-lvl", Math.min(1, lvl * 5).toFixed(3));
    }
    S.raf = requestAnimationFrame(meter);
  }

  /* ── ask_hermes → the real agent ─────────────────────────────────────── */
  async function answerTool(calls) {
    setState("thinking");
    const responses = await Promise.all(calls.map(async (fc) => {
      if (fc.name !== "ask_hermes") return { id: fc.id, name: fc.name, response: { error: "Unknown tool." } };
      const request = (fc.args && fc.args.request) || "";
      try {
        // Out-of-credit models are handled inside the agent turn itself (it falls
        // through to the next funded model), so one request is enough.
        const r = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: request, entity_id: S.entity, session_id: S.chatId, voice: true }),
        });
        const data = await r.json().catch(() => ({}));
        if (data.session_id && S) S.chatId = data.session_id;
        if (!r.ok || data.error) return { id: fc.id, name: fc.name, response: { error: data.error || `Hermes returned ${r.status}.` } };
        return { id: fc.id, name: fc.name, response: { answer: data.reply || "Hermes had no answer." } };
      } catch (e) {
        return { id: fc.id, name: fc.name, response: { error: "Couldn't reach Hermes: " + e.message } };
      }
    }));
    if (!S || S.ws.readyState !== WebSocket.OPEN) return;
    S.ws.send(JSON.stringify({ toolResponse: { functionResponses: responses } }));
    setState("speaking");
  }

  /* ── Wire protocol ───────────────────────────────────────────────────── */
  async function onMessage(ev) {
    const raw = typeof ev.data === "string" ? ev.data : await ev.data.text();
    let m; try { m = JSON.parse(raw); } catch (_) { return; }
    if (!S) return;

    if (m.setupComplete) { S.ready = true; setState("listening"); return; }
    if (m.toolCall) { answerTool(m.toolCall.functionCalls || []); return; }
    if (m.goAway) { say("hermes", " (session time is nearly up)"); return; }

    const sc = m.serverContent;
    if (!sc) return;
    if (sc.interrupted) { flush(); setState("listening"); }
    if (sc.inputTranscription && sc.inputTranscription.text) say("you", sc.inputTranscription.text);
    if (sc.outputTranscription && sc.outputTranscription.text) say("hermes", sc.outputTranscription.text);
    const parts = (sc.modelTurn && sc.modelTurn.parts) || [];
    parts.forEach((p) => {
      if (p.inlineData && /^audio\//.test(p.inlineData.mimeType || "")) {
        if (S.state !== "speaking") setState("speaking");
        play(int16FromB64(p.inlineData.data));
      }
    });
    if (sc.turnComplete) {
      S.line = null;
      if (!S.sources.size && S.state !== "thinking") setState("listening");
    }
  }

  /* ── Start / stop ────────────────────────────────────────────────────── */
  async function start() {
    if (S) return;
    const entity = (window.HermesBiz && HermesBiz.current) || "all";
    S = { state: "connecting", entity, chatId: null, sources: new Set(), playAt: 0, micLevel: 0, line: null };
    ui().querySelector(".hx-live-log").innerHTML = "";
    setState("connecting");
    try {
      // Mic permission first, so a denial doesn't burn a token.
      S.stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      const r = await fetch("/api/voice/live/session", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ entity_id: entity }),
      });
      const sess = await r.json();
      if (!r.ok || sess.error) throw new Error(sess.error || "Couldn't start a voice session.");
      if (!S) return; // stopped while waiting

      S.out = new AudioContext({ sampleRate: OUT_RATE });
      S.analyser = S.out.createAnalyser();
      S.analyser.fftSize = 512;
      S.meterBuf = new Float32Array(S.analyser.fftSize);
      S.analyser.connect(S.out.destination);

      S.in = new AudioContext();
      const url = URL.createObjectURL(new Blob([WORKLET], { type: "application/javascript" }));
      await S.in.audioWorklet.addModule(url);
      URL.revokeObjectURL(url);
      S.mic = S.in.createMediaStreamSource(S.stream);
      S.node = new AudioWorkletNode(S.in, "hermes-mic");
      S.mic.connect(S.node);
      S.node.port.onmessage = (e) => {
        if (!S) return;
        S.micLevel = e.data.level;
        if (S.ready && S.ws.readyState === WebSocket.OPEN) {
          S.ws.send(JSON.stringify({ realtimeInput: { audio: { data: b64FromBuffer(e.data.pcm), mimeType: `audio/pcm;rate=${IN_RATE}` } } }));
        }
      };

      S.ws = new WebSocket(sess.ws_url + "?access_token=" + encodeURIComponent(sess.token));
      S.ws.onopen = () => S.ws.send(JSON.stringify({ setup: sess.setup }));
      S.ws.onmessage = onMessage;
      // onclose always follows onerror and carries Gemini's reason, so report there.
      S.ws.onclose = (e) => {
        if (!S) return;
        if (e.code === 1000) stop();
        else fail(e.reason || `Voice session closed (${e.code}).`);
      };
      meter();
    } catch (e) {
      fail(e && e.name === "NotAllowedError" ? "Microphone access was blocked — allow it in the browser to talk to Hermes." : (e.message || String(e)));
    }
  }

  function teardown() {
    if (!S) return;
    const s = S; S = null;
    cancelAnimationFrame(s.raf);
    try { s.ws && s.ws.close(1000); } catch (_) {}
    try { s.stream && s.stream.getTracks().forEach((t) => t.stop()); } catch (_) {}
    try { s.in && s.in.close(); } catch (_) {}
    try { s.out && s.out.close(); } catch (_) {}
    const rings = $("hud-rings");
    if (rings) { rings._energy && rings._energy(0.35); rings.style.removeProperty("--live-lvl"); }
  }
  function stop() { teardown(); setState("idle"); }
  function fail(msg) {
    teardown();
    setState("error", msg);
    const el = ui();
    el.classList.add("open");
    clearTimeout(fail.t);
    fail.t = setTimeout(() => { if (!S) setState("idle"); }, 6000);
  }
  function toggle() { S ? stop() : start(); }

  function init() {
    const face = document.querySelector(".hud-core-face");
    if (face && !$("hx-live-core")) {
      const tag = document.createElement("div");
      tag.id = "hx-live-core";
      tag.className = "hx-live-core";
      face.appendChild(tag);
      face.setAttribute("aria-label", "Talk to Hermes");
      face.title = "Talk to Hermes";
    }
    addEventListener("keydown", (e) => { if (e.key === "Escape" && S) stop(); });
    addEventListener("pagehide", teardown);
  }

  window.HermesLive = {
    toggle, start, stop,
    get active() { return !!S; },
    get state() { return S ? S.state : "idle"; },
    // Typed input into the live session — handy when there's no mic.
    send(text) {
      if (S && S.ready && text) S.ws.send(JSON.stringify({ realtimeInput: { text } }));
    },
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
