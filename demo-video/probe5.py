import asyncio
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        b=await p.chromium.launch(channel="chrome",headless=True)
        ctx=await b.new_context(viewport={"width":1920,"height":1080})
        pg=await ctx.new_page()
        await pg.goto("http://localhost:5057/",timeout=120000); await pg.wait_for_timeout(16000)
        await pg.locator(".strip-tab:has-text('JOBS')").first.click(); await pg.wait_for_timeout(2500)
        l=pg.get_by_text("Google Reviews Scraper").filter(visible=True).first; await l.wait_for(timeout=60000); await l.click(); await pg.wait_for_timeout(3000)
        heads=pg.locator("#jb-runs .tk-run-head").filter(visible=True)
        print("heads", await heads.count())
        await heads.first.click(); await pg.wait_for_timeout(1500)
        await pg.screenshot(path="shots/p5_job.png")
        links=pg.locator(".jb-report-link")
        print("links total", await links.count(), "visible", await links.filter(visible=True).count())
        print([await links.nth(i).get_attribute("href") for i in range(min(3,await links.count()))])
        print("first run body:", (await pg.locator("#jb-runs .tk-run-body").first.inner_text())[:300])
        await b.close()
# asyncio.run(main())

async def main2():
    async with async_playwright() as p:
        ctx=await p.chromium.launch_persistent_context("chrome-profile",channel="chrome",headless=True,viewport={"width":1920,"height":1080})
        await ctx.add_init_script(path="overlay.js")
        pg=ctx.pages[0]
        await pg.goto("http://localhost:5057/",timeout=120000); await pg.wait_for_timeout(16000)
        await pg.locator(".strip-tab:has-text('JOBS')").first.click()
        l=pg.get_by_text("Google Reviews Scraper").filter(visible=True).first; await l.wait_for(timeout=60000); await l.click()
        h=pg.locator("#jb-runs .tk-run-head").filter(visible=True).first; await h.wait_for(timeout=60000); await h.click(); await pg.wait_for_timeout(1000)
        link=pg.locator(".jb-report-link").filter(visible=True).first
        print("link visible:", await link.is_visible(), await link.bounding_box())
        pg.on("popup", lambda p: print("POPUP", p.url))
        ctx.on("page", lambda p: print("PAGE", p.url))
        await link.click(); await pg.wait_for_timeout(5000)
        print("pages:", [x.url for x in ctx.pages])
        await pg.screenshot(path="shots/p5_after_click.png")
        await ctx.close()
asyncio.run(main2())
