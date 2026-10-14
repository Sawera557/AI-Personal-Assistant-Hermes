import asyncio
from playwright.async_api import async_playwright
TABS=[("AGENT",None),("BILLING","OVERVIEW"),("BILLING","REGISTER"),("BILLING","DOCUMENTS"),("MEETING",None),("CONTEXT","#context-subtab-memories"),("CONTEXT","#context-subtab-skills"),("TASKS",None),("MULTI-AGENTS",None),("USAGE",None),("SETTINGS",None),("ARTIFACTS",None),("FILES",None)]
async def main():
    async with async_playwright() as p:
        b=await p.chromium.launch(channel="chrome",headless=True)
        pg=await b.new_page(viewport={"width":1920,"height":1080})
        await pg.goto("http://localhost:5057/", timeout=90000); await pg.wait_for_timeout(15000)
        for i,(tab,sub) in enumerate(TABS):
            await pg.click(f".strip-tab:has-text('{tab}')"); await pg.wait_for_timeout(5000)
            if sub:
                sel = sub if sub.startswith("#") else f".reg-tab:has-text('{sub}')"
                await pg.click(sel); await pg.wait_for_timeout(5000)
            await pg.screenshot(path=f"shots/s2_{i:02d}_{tab}_{(sub or '').strip('#')}.png")
        await b.close()
asyncio.run(main())
