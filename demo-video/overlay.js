// Demo harness injected before the app loads. Test-only: a visible cursor,
// an in-page audio player for narration, and a scripted microphone for Jarvis.
(() => {
  // ── shared audio context: narration + spoken questions play to the speakers
  let ctx = null, micDest = null;
  const ac = () => (ctx = ctx || new AudioContext({ sampleRate: 48000 }));
  const buffers = {};
  async function load(url) {
    if (!buffers[url]) buffers[url] = await ac().decodeAudioData(await (await fetch(url)).arrayBuffer());
    return buffers[url];
  }
  window.__preload = async (urls) => { await ac().resume(); await Promise.all(urls.map(load)); return true; };
  // play(url, {mic}) -> resolves with duration once playback has finished
  window.__play = async (url, opts = {}) => {
    await ac().resume();
    const buf = await load(url);
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(ctx.destination);
    if (opts.mic && micDest) src.connect(micDest);
    return new Promise((res) => { src.onended = () => res(buf.duration); src.start(); });
  };

  // sync marker: white flash + 1 kHz beep at the same instant
  window.__sync = async () => {
    await ac().resume();
    const f = document.createElement("div");
    f.style.cssText = "position:fixed;inset:0;background:#fff;z-index:2147483647";
    document.body.appendChild(f);
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.frequency.value = 1000; g.gain.value = 0.5; o.connect(g); g.connect(ctx.destination);
    o.start(); o.stop(ctx.currentTime + 0.25);
    await new Promise((r) => setTimeout(r, 250)); f.remove();
  };

  // ── scripted microphone: getUserMedia(audio) returns a stream we feed
  const md = navigator.mediaDevices;
  if (md) {
    const orig = md.getUserMedia.bind(md);
    md.getUserMedia = async (c) => {
      if (c && c.audio && !c.video) {
        await ac().resume();
        micDest = ctx.createMediaStreamDestination();
        return micDest.stream;
      }
      return orig(c);
    };
  }

  // ── visible cursor
  const css = `
    #demo-cursor{position:fixed;left:0;top:0;width:26px;height:26px;z-index:2147483647;pointer-events:none;
      transform:translate(960px,540px);transition:transform 0.8s cubic-bezier(.45,.05,.25,1);filter:drop-shadow(0 2px 4px rgba(0,0,0,.6))}
    .demo-ripple{position:fixed;width:16px;height:16px;margin:-8px 0 0 -8px;border-radius:50%;z-index:2147483646;pointer-events:none;
      border:2px solid rgba(120,220,255,.95);animation:demo-rip .6s ease-out forwards}
    @keyframes demo-rip{from{transform:scale(.4);opacity:1}to{transform:scale(3.2);opacity:0}}`;
  function mount() {
    if (document.getElementById("demo-cursor")) return;
    const st = document.createElement("style"); st.textContent = css; document.head.appendChild(st);
    const c = document.createElement("div"); c.id = "demo-cursor";
    c.innerHTML = '<svg viewBox="0 0 24 24" width="26" height="26"><path d="M4 2l15 11.5-6.6 1.1 3.9 7.4-2.9 1.5-3.9-7.5L4 21z" fill="#fff" stroke="#0b1620" stroke-width="1.4" stroke-linejoin="round"/></svg>';
    document.body.appendChild(c);
  }
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", mount) : mount();
  window.__cur = {
    x: 960, y: 540,
    move(x, y, ms = 800) {
      mount();
      const c = document.getElementById("demo-cursor");
      c.style.transitionDuration = ms + "ms";
      c.style.transform = `translate(${x - 4}px,${y - 2}px)`;
      this.x = x; this.y = y;
    },
    ripple() {
      const r = document.createElement("div"); r.className = "demo-ripple";
      r.style.left = this.x + "px"; r.style.top = this.y + "px";
      document.body.appendChild(r); setTimeout(() => r.remove(), 700);
    },
  };
})();
