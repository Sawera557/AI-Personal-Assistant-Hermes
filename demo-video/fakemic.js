(() => {
  const md = navigator.mediaDevices; if (!md) return;
  const orig = md.getUserMedia.bind(md);
  let ctx = null, dest = null;
  window.__demoLog = [];
  window.__say = async (url) => {
    const ab = await ctx.decodeAudioData(await (await fetch(url)).arrayBuffer());
    const src = ctx.createBufferSource(); src.buffer = ab;
    src.connect(dest); src.connect(ctx.destination); src.start();
    window.__demoLog.push(["say", url, performance.now()]);
    return ab.duration;
  };
  md.getUserMedia = async (c) => {
    if (c && c.audio && !c.video) {
      ctx = ctx || new AudioContext({ sampleRate: 48000 });
      await ctx.resume();
      dest = ctx.createMediaStreamDestination();
      return dest.stream;
    }
    return orig(c);
  };
})();
