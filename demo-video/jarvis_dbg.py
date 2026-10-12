import asyncio, time, sys
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True, args=["--autoplay-policy=no-user-gesture-required"])
        ctx = await b.new_context(viewport={"width":1920,"height":1080})
        await ctx.add_init_script(path="fakemic.js")
        await ctx.route("**/__demo/*", lambda r: r.fulfill(path="audio/"+r.request.url.rsplit("/",1)[1], content_type="audio/wav"))
        pg = await ctx.new_page()
        t0=time.time()
        T=lambda: f"{time.time()-t0:6.1f}s"
        pg.on("pageerror", lambda e: print(T(),"PAGEERR", str(e)[:300]))
        pg.on("console", lambda m: print(T(),"CONSOLE", m.type, m.text[:300]))
        def onws(ws):
            print(T(),"WS open", ws.url[:80])
            ws.on("close", lambda w: print(T(),"WS close"))
            ws.on("framereceived", lambda f: print(T(),"WS<", (f if isinstance(f,str) else f.decode('utf-8','replace'))[:250]))
            ws.on("socketerror", lambda e: print(T(),"WS err", e))
        pg.on("websocket", onws)
        pg.on("response", lambda r: "/api/" in r.url and print(T(),"HTTP", r.status, r.url[-60:]))
        await pg.goto("http://localhost:5000/"); await pg.wait_for_timeout(15000)
        await pg.click(".hud-core-face")
        for i in range(30):
            s=await pg.evaluate("document.getElementById('hx-live')?.dataset.state")
            if s=="listening": break
            await asyncio.sleep(0.5)
        await pg.wait_for_timeout(800)
        print(T(),"SAY", await pg.evaluate("__say('/__demo/q1.wav')"))
        for i in range(120):
            s=await pg.evaluate("(()=>{const e=document.getElementById('hx-live');return e.dataset.state+' | '+e.querySelector('.hx-live-state').textContent})()")
            print(T(),"STATE",s) if i%4==0 or 'error' in s else None
            if 'error' in s or 'idle' in s: break
            await asyncio.sleep(0.5)
        await b.close()
asyncio.run(main())
