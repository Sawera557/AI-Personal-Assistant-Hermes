import asyncio, time, sys
from playwright.async_api import async_playwright
HEADLESS = "--headed" not in sys.argv
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=HEADLESS, args=["--autoplay-policy=no-user-gesture-required"])
        ctx = await b.new_context(viewport={"width":1920,"height":1080})
        await ctx.add_init_script(path="fakemic.js")
        await ctx.route("**/__demo/*", lambda r: r.fulfill(path="audio/"+r.request.url.rsplit("/",1)[1], content_type="audio/wav"))
        pg = await ctx.new_page()
        pg.on("pageerror", lambda e: print("PAGEERR", str(e)[:200]))
        pg.on("console", lambda m: m.type in ("error","warning") and print("CONSOLE", m.text[:200]))
        await pg.goto("http://localhost:5000/"); await pg.wait_for_timeout(15000)
        t0 = time.time()
        st = lambda: pg.evaluate("(document.getElementById('hx-live')||{dataset:{}}).dataset.state||'none'")
        await pg.click(".hud-core-face")
        last=None
        async def wait_state(target, timeout):
            nonlocal last
            end=time.time()+timeout
            while time.time()<end:
                s=await st()
                if s!=last: print(f"{time.time()-t0:6.1f}s state={s}"); last=s
                if s in target: return s
                await asyncio.sleep(0.25)
            return None
        await wait_state({"listening","error"}, 30)
        await pg.wait_for_timeout(800)
        for q in ("q1.wav","q2.wav"):
            print(f"{time.time()-t0:6.1f}s SAY {q}", await pg.evaluate(f"__say('/__demo/{q}')"))
            await wait_state({"thinking","speaking"}, 20)
            await wait_state({"listening"}, 120)
            # make sure it stays listening (answer finished)
            await pg.wait_for_timeout(2500)
            await wait_state({"listening"}, 60)
        await pg.screenshot(path="shots/jarvis.png")
        print(await pg.evaluate("[...document.querySelectorAll('.hx-live-line')].map(e=>e.className+' :: '+e.innerText).join(' || ')"))
        await b.close()
asyncio.run(main())
