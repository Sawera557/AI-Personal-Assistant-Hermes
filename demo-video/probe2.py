import asyncio, time
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        b=await p.chromium.launch(channel="chrome",headless=True)
        pg=await b.new_page(viewport={"width":1920,"height":1080})
        pg.on("pageerror",lambda e: print("PAGEERR",str(e)[:200]))
        await pg.goto("http://localhost:5000/"); await pg.wait_for_timeout(15000)
        t=time.time()
        await pg.click(".strip-tab:has-text('SYSTEM')")
        for i in range(8):
            await pg.wait_for_timeout(3000)
            n=await pg.evaluate("(document.getElementById('schema-view')||{}).innerHTML?.length||0")
            print(round(time.time()-t),"schema html len",n)
            if n>2000: break
        await pg.wait_for_timeout(3000)
        await pg.screenshot(path="shots/p2_system.png")
        await b.close()
asyncio.run(main())
