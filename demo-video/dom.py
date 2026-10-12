import asyncio
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        b=await p.chromium.launch(channel="chrome",headless=True)
        pg=await b.new_page(viewport={"width":1920,"height":1080})
        await pg.goto("http://localhost:5057/",timeout=90000); await pg.wait_for_timeout(15000)
        print("HOME:\n"+await pg.evaluate(open("probe_home.js").read()))
        await pg.click(".strip-tab:has-text('SYSTEM')"); await pg.wait_for_timeout(9000)
        print("SYSTEM:\n"+await pg.evaluate(open("probe_sys.js").read()))
        await b.close()
asyncio.run(main())
