import asyncio
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        b=await p.chromium.launch(channel="chrome",headless=True)
        ctx=await b.new_context(viewport={"width":1920,"height":1080})
        r=await ctx.new_page()
        await r.goto("http://localhost:5057/api/reviews/report/2026-08-21",timeout=120000); await r.wait_for_timeout(3000)
        await r.screenshot(path="shots/p4_reviews_report.png")
        print("report height", await r.evaluate("document.body.scrollHeight"), "title", await r.title())
        await r.mouse.wheel(0,1100); await r.wait_for_timeout(800); await r.screenshot(path="shots/p4_reviews_report2.png")
        pg=await ctx.new_page()
        await pg.goto("http://localhost:5057/",timeout=120000); await pg.wait_for_timeout(16000)
        await pg.locator(".strip-tab:has-text('ARTIFACTS')").first.click(); await pg.wait_for_timeout(5000)
        await pg.get_by_text("Scanned Receipts Dashboard").filter(visible=True).first.click(); await pg.wait_for_timeout(8000)
        await pg.screenshot(path="shots/p4_artifact_open.png")
        print("pages", len(ctx.pages), [x.url[:80] for x in ctx.pages])
        await pg.keyboard.press("Escape"); await pg.wait_for_timeout(1000)
        await pg.locator(".lib-subtab:has-text('FILES')").first.click(); await pg.wait_for_timeout(5000)
        await pg.screenshot(path="shots/p4_files.png")
        await b.close()
asyncio.run(main())
