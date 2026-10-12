import asyncio
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        b=await p.chromium.launch(channel="chrome",headless=True,args=["--use-angle=swiftshader","--enable-unsafe-swiftshader"])
        pg=await b.new_page(viewport={"width":1920,"height":1080})
        logs=[]; pg.on("console",lambda m: logs.append(m.type+": "+m.text[:160]))
        pg.on("pageerror",lambda e: logs.append("PAGEERR: "+str(e)[:200]))
        for r,n in [("/","home"),("/system","system"),("/billing","billing")]:
            await pg.goto("http://localhost:5000"+r)
            for t in (3,8,15):
                await pg.wait_for_timeout(4000 if t==3 else 5000 if t==8 else 7000)
                await pg.screenshot(path=f"shots/p_{n}_{t}.png")
        print("\n".join(logs[:40]))
        await b.close()
asyncio.run(main())
