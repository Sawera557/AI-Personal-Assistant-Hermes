"""Drive Hermes in real Chrome (kiosk, full screen) and record screen + system audio.

usage:
  python record.py --rehearse         headless, no recording, no Jarvis questions (selector check)
  python record.py --take N           headed full-screen take -> takes/take_N.mkv + takes/take_N.json
"""
import asyncio, ctypes, json, queue, subprocess, sys, threading, time
from playwright.async_api import async_playwright

BASE = "http://localhost:5057"
REHEARSE = "--rehearse" in sys.argv
TAKE = sys.argv[sys.argv.index("--take") + 1] if "--take" in sys.argv else "0"
SKIP_JARVIS = "--no-jarvis" in sys.argv or REHEARSE
PICKUP = "--pickup" in sys.argv
PART2 = "--part2" in sys.argv
DUR = {k.split("/")[-1][:-4]: v for k, v in json.load(open("audio/durations.json")).items()}
EVENTS = []
T0 = [None]


def log(ev_name, **kw):
    t = time.time() - T0[0] if T0[0] else -1
    EVENTS.append({"t": round(t, 3), "ev": ev_name, **kw})
    print(f"{t:7.2f}  {ev_name} {kw if kw else ''}", flush=True)


# ── screen + loopback audio recorder ─────────────────────────────────────────
class Recorder:
    def __init__(self, path):
        import pyaudiowpatch as pa
        self.pa = pa
        self.p = pa.PyAudio()
        dev = self.p.get_default_wasapi_loopback()
        self.rate, self.ch = int(dev["defaultSampleRate"]), dev["maxInputChannels"]
        self.dev, self.path, self.q = dev, path, queue.Queue()

    def start(self):
        pa = self.pa
        out_idx = self.p.get_default_output_device_info()["index"]
        # WASAPI loopback stops delivering frames during silence; a silent output stream keeps it flowing
        self.keep = self.p.open(format=pa.paInt16, channels=self.ch, rate=self.rate, output=True,
                                output_device_index=out_idx, frames_per_buffer=1024,
                                stream_callback=lambda d, n, ti, st: (b"\0" * n * self.ch * 2, pa.paContinue))
        port = 5999
        self.ff = subprocess.Popen(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-thread_queue_size", "4096", "-f", "gdigrab", "-framerate", "30", "-draw_mouse", "0", "-i", "desktop",
             "-thread_queue_size", "4096", "-f", "s16le", "-ar", str(self.rate), "-ac", str(self.ch),
             "-i", f"tcp://127.0.0.1:{port}?listen=1",
             "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16",
             "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", self.path],
            stdin=subprocess.PIPE)
        import socket
        for _ in range(100):
            try:
                self.sock = socket.create_connection(("127.0.0.1", port)); break
            except OSError:
                time.sleep(0.05)
        self.alive = True
        self.writer = threading.Thread(target=self._writer, daemon=True); self.writer.start()
        self.inp = self.p.open(format=pa.paInt16, channels=self.ch, rate=self.rate, input=True,
                               input_device_index=self.dev["index"], frames_per_buffer=1024,
                               stream_callback=lambda d, n, ti, st: (self.q.put(d), (None, pa.paContinue))[1])

    def _writer(self):
        while self.alive or not self.q.empty():
            try:
                self.sock.sendall(self.q.get(timeout=0.2))
            except queue.Empty:
                pass

    def stop(self):
        self.inp.stop_stream(); self.inp.close()
        self.alive = False
        self.writer.join(timeout=10)
        self.sock.close()
        time.sleep(0.5)
        self.ff.stdin.write(b"q"); self.ff.stdin.flush()
        try:
            self.ff.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.ff.kill()
        self.keep.stop_stream(); self.keep.close(); self.p.terminate()


# ── page helpers ─────────────────────────────────────────────────────────────
class Demo:
    def __init__(self, pg):
        self.pg = pg

    def vis(self, sel):
        return self.pg.locator(sel).filter(visible=True).first

    def text(self, t):
        return self.pg.get_by_text(t, exact=False).filter(visible=True).first

    async def point(self, loc, ms=700, hover=True):
        try:
            await loc.wait_for(state="visible", timeout=15000)
            await loc.scroll_into_view_if_needed(timeout=3000)
            box = await loc.bounding_box(timeout=3000)
        except Exception as e:
            log("miss", target=str(loc)[-90:]); return False
        if not box:
            log("miss", target=str(loc)[:80]); return False
        x = box["x"] + min(box["width"] / 2, 120)
        y = box["y"] + min(box["height"] / 2, 30)
        await self.pg.evaluate(f"__cur.move({x:.0f},{y:.0f},{ms})")
        await asyncio.sleep(ms / 1000 + 0.05)
        if hover:
            try: await self.pg.mouse.move(x, y, steps=4)
            except Exception: pass
        return True

    async def click(self, loc, ms=700, settle=1.2):
        if await self.point(loc, ms):
            await self.pg.evaluate("__cur.ripple()")
            await asyncio.sleep(0.12)
            await loc.click(timeout=5000)
            await asyncio.sleep(settle)

    async def tab(self, label, settle=2.0):
        await self.click(self.vis(f".strip-tab:has-text('{label}')"), settle=settle)
        log("tab", name=label)

    def say(self, key):
        log("say", key=key, dur=DUR[key])
        return asyncio.ensure_future(self.pg.evaluate(f"__play('/__demo/{key}.wav')"))

    async def narrate(self, key, beats=(), tail=0.5):
        """Play a narration clip; beats = [(fraction_of_clip, coroutine_factory)] run on schedule."""
        task = self.say(key)
        t0, d = time.time(), DUR[key]
        for frac, fn in beats:
            wait = t0 + frac * d - time.time()
            if wait > 0: await asyncio.sleep(wait)
            try: await fn()
            except Exception as e: log("beat-error", err=str(e)[:120])
        await task
        await asyncio.sleep(tail)

    async def scroll(self, sel, dy, ms=900):
        await self.pg.evaluate(
            "([s,dy])=>{const e=[...document.querySelectorAll(s)].find(x=>x.offsetParent&&x.scrollHeight>x.clientHeight)||document.scrollingElement;e.scrollBy({top:dy,behavior:'smooth'})}",
            [sel, dy])
        await asyncio.sleep(ms / 1000)

    async def live_state(self):
        return await self.pg.evaluate("(document.getElementById('hx-live')||{dataset:{}}).dataset.state||'none'")

    async def wait_state(self, targets, timeout):
        end = time.time() + timeout
        while time.time() < end:
            s = await self.live_state()
            if s in targets: return s
            await asyncio.sleep(0.2)
        return None

    async def wait_answer_done(self, timeout=150):
        """Speaking finished: state stays 'listening' for 2.5s."""
        end = time.time() + timeout
        calm = None
        while time.time() < end:
            s = await self.live_state()
            if s == "listening":
                calm = calm or time.time()
                if time.time() - calm > 2.5: return True
            elif s == "error":
                return False
            else:
                calm = None
            await asyncio.sleep(0.2)
        return False


# ── the storyboard ───────────────────────────────────────────────────────────
async def storyboard(D: Demo):
    pg = D.pg
    T = D.text

    if PICKUP:
        await D.tab("TASKS", settle=2.0)
        await D.point(T("Next:"), hover=False)
        await asyncio.sleep(1.0)
    else:
        # S0 — cold open: replay the boot intro, then why Hermes exists
        await asyncio.sleep(1.0)
        await D.click(D.vis("#hx-tb-replay"), settle=0.3)
        log("boot-replay")
        await asyncio.sleep(1.2)
        await D.narrate("n00_intro", tail=0.4)
        await D.narrate("n01_why", beats=[
            (0.18, lambda: D.point(D.vis(".hud-mod:has-text('CREDIT LOAD')"))),
            (0.40, lambda: D.point(D.vis("#hud-rings"), hover=False)),
            (0.62, lambda: D.point(D.vis("#hud-alerts"))),
            (0.80, lambda: D.point(D.vis("#hud-conns"))),
        ], tail=0.6)

        # S1 — System
        await D.tab("SYSTEM", settle=1.0)
        await pg.wait_for_selector(".ax-node[data-n='web']", timeout=30000)
        await D.narrate("n02_system", beats=[(0.35, lambda: D.point(D.text("How Hermes is built"), hover=False))], tail=0.3)
        node = lambda n: D.vis(f".ax-node[data-n='{n}']")
        await D.narrate("n03_columns", beats=[
            (0.02, lambda: D.point(node("web"))),
            (0.09, lambda: D.point(node("voice"))),
            (0.15, lambda: D.point(node("meet"))),
            (0.24, lambda: D.point(node("api"))),
            (0.35, lambda: D.point(node("agents"))),
            (0.44, lambda: D.point(node("model"))),
            (0.55, lambda: D.point(node("guard"))),
            (0.66, lambda: D.point(node("mem"))),
            (0.72, lambda: D.point(node("skills"))),
            (0.78, lambda: D.point(node("ctx"))),
            (0.84, lambda: D.point(node("pg"))),
            (0.92, lambda: D.point(node("pine"))),
        ], tail=0.3)
        await D.point(D.vis("#fx-trace"), ms=600)
        await pg.evaluate("__cur.ripple()")
        await D.vis("#fx-trace").click()
        log("trace")
        await D.narrate("n04_trace", beats=[(0.75, lambda: D.point(node("maton"), hover=False))], tail=2.5)

        # S2 — Home HUD
        await D.tab("HOME", settle=2.0)
        await D.narrate("n06_home", beats=[
            (0.12, lambda: D.point(D.vis(".hud-mod:has-text('KNOWLEDGE BASE')"))),
            (0.30, lambda: D.point(D.vis(".hud-mod:has-text('CREDIT LOAD')"))),
            (0.48, lambda: D.point(D.vis("#hud-sched"))),
            (0.64, lambda: D.point(D.vis("#hud-conns"))),
            (0.82, lambda: D.point(D.vis("#hud-alerts"))),
        ], tail=0.3)

        # S3 — Jarvis
        orb = D.vis(".hud-core-face")
        await D.narrate("n07_jarvis", beats=[(0.45, lambda: D.point(orb, ms=900))], tail=0.2)
        if not SKIP_JARVIS:
            await pg.evaluate("__cur.ripple()")
            await orb.click()
            log("jarvis-click")
            st = await D.wait_state({"listening", "error"}, 40)
            log("jarvis-state", state=st)
            await asyncio.sleep(1.2)
            await pg.evaluate("__cur.move(1500,980,900)")
            for q in ("q1", "q2"):
                log("ask", q=q)
                await pg.evaluate(f"__play('/__demo/{q}.wav',{{mic:true}})")
                s = await D.wait_state({"thinking", "speaking", "error"}, 30)
                log("jarvis-react", q=q, state=s)
                s = await D.wait_state({"speaking", "error"}, 150) if s == "thinking" else s
                log("jarvis-speaking", q=q, state=s)
                ok = await D.wait_answer_done(150)
                log("jarvis-done", q=q, ok=ok)
            await D.narrate("n08_jarvis_how", tail=0.3)
            await D.click(D.vis(".hx-live-stop"), settle=1.5)
            log("jarvis-end")
        else:
            await D.narrate("n08_jarvis_how", tail=0.3)

        # S4 — Agent chat (the voice conversation is saved as a regular chat)
        await D.tab("AGENT", settle=2.0)
        await D.click(D.vis(".convo-open"), settle=1.5)
        await D.narrate("n09_agent", beats=[
            (0.15, lambda: D.point(D.vis(".convo-open"))),
            (0.55, lambda: D.point(D.vis("#voice-btn"))),
            (0.80, lambda: D.point(D.vis("#agent-mode-agent"))),
        ], tail=0.4)

        # S5 — Billing
        await D.tab("BILLING", settle=1.0)
        await D.click(D.vis(".reg-tab:has-text('REGISTER')"), settle=2.5)
        await D.narrate("n10_billing", beats=[
            (0.10, lambda: D.point(T("monthly run-rate"))),
            (0.45, lambda: D.point(T("past due"))),
            (0.75, lambda: D.point(T("unverified"))),
        ], tail=0.2)
        approve = T("Approve & write to sheet")
        await D.narrate("n10b_proposal", beats=[
            (0.0, lambda: D.point(approve, ms=1000, hover=False)),
            (0.55, lambda: D.point(T("See the document"))),
        ], tail=0.3)
        await D.click(D.vis(".reg-tab:has-text('DOCUMENTS')"), settle=1.5)
        await D.narrate("n10c_documents", beats=[(0.3, lambda: D.point(T("Check payments"), hover=False))], tail=0.4)

        # S6 — Meeting Copilot
        await D.tab("MEETING", settle=1.5)
        await D.narrate("n11_meeting", beats=[
            (0.05, lambda: D.point(D.vis("#v2-meeting-pick"))),
            (0.45, lambda: D.point(T("Nour Hammam x"))),
            (0.75, lambda: D.point(T("Review Kenya"))),
        ], tail=0.4)

        # S7 — Context: memories, then skills
        await D.tab("CONTEXT", settle=1.0)
        await D.click(D.vis("#context-subtab-memories"), settle=2.0)
        await D.narrate("n12_memories", beats=[(0.45, lambda: D.point(T("Rent and money figures")))], tail=0.3)
        await D.click(D.vis("#context-subtab-skills"), settle=1.5)
        await D.narrate("n12b_skills", beats=[
            (0.25, lambda: D.point(T("Monthly Digest"))),
            (0.45, lambda: D.point(T("Month-End Close"))),
            (0.65, lambda: D.point(T("Competitor Analysis"))),
        ], tail=0.4)

        # S8 — Multi-agents
        await D.tab("MULTI-AGENTS", settle=2.0)
        await D.narrate("n13_agents", beats=[
            (0.15, lambda: D.point(T("Commercial Leasing"))),
            (0.30, lambda: D.point(T("Farm Operations"))),
            (0.45, lambda: D.point(T("Markets & Buyers"))),
            (0.58, lambda: D.point(T("House Scout"))),
            (0.80, lambda: D.point(T("read-only"), hover=False)),
        ], tail=0.4)

        # S9 — Scheduled tasks
        await D.tab("TASKS", settle=2.0)
        await D.narrate("n14_tasks", beats=[
            (0.15, lambda: D.point(T("Urgent email triage"))),
            (0.30, lambda: D.point(T("Daily attention report"))),
            (0.45, lambda: D.point(T("AFK payroll cash check"))),
            (0.60, lambda: D.point(T("GLOBALG.A.P. compliance"))),
            (0.85, lambda: D.point(T("Next:"), hover=False)),
        ], tail=0.4)

    log("splice")

    # S10 — Usage, then Settings
    await D.tab("USAGE", settle=2.0)
    await D.narrate("n15_usage", beats=[
        (0.10, lambda: D.point(T("Total credits"))),
        (0.30, lambda: D.point(T("Total cost"))),
        (0.60, lambda: D.point(D.vis(".usage-chip:has-text('LAST 7 DAYS')"), hover=False)),
    ], tail=0.3)
    await D.tab("SETTINGS", settle=2.0)
    await D.narrate("n16_settings", beats=[
        (0.18, lambda: D.point(T("Confirm before write"))),
        (0.40, lambda: D.point(T("Business data isolation"))),
        (0.62, lambda: D.point(T("Block secrets"))),
        (0.80, lambda: D.point(T("Limit sends and deletes"))),
    ], tail=0.4)

    # S11 — Outro
    await D.tab("HOME", settle=2.0)
    await D.narrate("n17_outro", beats=[(0.3, lambda: D.point(D.vis("#hud-rings"), ms=1200, hover=False))], tail=3.0)
    log("end")


async def main():
    clips = sorted(DUR)
    async with async_playwright() as p:
        args = ["--autoplay-policy=no-user-gesture-required", "--force-device-scale-factor=1",
                "--disable-infobars", "--no-first-run", "--no-default-browser-check",
                "--disable-features=Translate,MediaRouter", "--disable-notifications"]
        if not REHEARSE:
            args += ["--kiosk", "--window-position=0,0", "--window-size=1920,1080"]
        ctx = await p.chromium.launch_persistent_context(
            "chrome-profile", channel="chrome", headless=REHEARSE, args=args,
            ignore_default_args=["--enable-automation"],
            no_viewport=not REHEARSE, viewport={"width": 1920, "height": 1080} if REHEARSE else None)
        await ctx.add_init_script(path="overlay.js")
        await ctx.route("**/__demo/*", lambda r: r.fulfill(path="audio/" + r.request.url.rsplit("/", 1)[1], content_type="audio/wav"))
        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
        pg.on("pageerror", lambda e: log("pageerror", err=str(e)[:150]))
        await pg.goto(BASE + "/", timeout=120000)
        await pg.wait_for_timeout(16000)          # boot intro + HUD data
        await pg.evaluate("__preload(%s)" % json.dumps([f"/__demo/{c}.wav" for c in clips]))
        await pg.evaluate("__cur.move(1500,700,10)")
        if not REHEARSE:
            ctypes.windll.user32.SetCursorPos(1919, 1079)   # park the real cursor
        rec = None
        if not REHEARSE:
            rec = Recorder(f"takes/take_{TAKE}.mkv")
            rec.start()
        T0[0] = time.time()
        log("rec-start")
        if rec:
            await asyncio.sleep(1.5)
            await pg.evaluate("__sync()")
            log("sync")
        try:
            if PART2:
                from part2 import storyboard2
                await storyboard2(Demo(pg), log)
            else:
                await storyboard(Demo(pg))
        finally:
            if REHEARSE:
                await pg.screenshot(path="shots/rehearse_end.png")
            if rec:
                rec.stop()
            json.dump(EVENTS, open(f"takes/take_{TAKE}.json", "w"), indent=1)
            await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
