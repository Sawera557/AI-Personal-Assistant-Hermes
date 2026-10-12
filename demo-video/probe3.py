import asyncio, json
from playwright.async_api import async_playwright
async def shot(pg, n): await pg.screenshot(path=f"shots/p3_{n}.png")
async def main():
    async with async_playwright() as p:
        b=await p.chromium.launch(channel="chrome",headless=True)
        ctx=await b.new_context(viewport={"width":1920,"height":1080})
        pg=await ctx.new_page()
        await pg.goto("http://localhost:5057/",timeout=120000); await pg.wait_for_timeout(16000)
        tab=lambda t: pg.locator(f".strip-tab:has-text('{t}')").first.click()
        # Tasks -> Daily attention report
        await tab("TASKS"); await pg.wait_for_timeout(4000)
        await pg.get_by_text("Daily attention report").filter(visible=True).first.click(); await pg.wait_for_timeout(5000)
        await shot(pg,"task_detail")
        heads=pg.locator(".tk-run-head").filter(visible=True)
        print("task runs:", await heads.count())
        if await heads.count():
            await pg.locator(".tk-run-head:has(.tk-run-badge.success)").first.click(); await pg.wait_for_timeout(1500)
            r=pg.locator(".tk-run-reply").filter(visible=True).first
            await r.scroll_into_view_if_needed(); await shot(pg,"task_run_open")
            print("run text:", (await r.inner_text())[:900])
        print("close btns:", await pg.evaluate("[...document.querySelectorAll('button')].filter(e=>e.offsetParent&&/close|×|✕/i.test((e.getAttribute('aria-label')||'')+e.innerText)).map(e=>(e.id||'')+'.'+e.className+'|'+(e.getAttribute('aria-label')||e.innerText))"))
        await pg.keyboard.press("Escape"); await pg.wait_for_timeout(800)
        # Jobs -> reviews scraper
        await tab("JOBS"); await pg.wait_for_timeout(4000); await shot(pg,"jobs")
        await pg.get_by_text("Google Reviews Scraper").filter(visible=True).first.click(); await pg.wait_for_timeout(5000)
        await shot(pg,"job_detail")
        heads=pg.locator("#jb-runs .tk-run-head").filter(visible=True)
        print("job runs:", await heads.count())
        if await heads.count():
            await heads.first.click(); await pg.wait_for_timeout(1500); await shot(pg,"job_run_open")
            print("job run html:", (await pg.locator("#jb-runs .tk-run-body").first.inner_html())[:900])
        await pg.keyboard.press("Escape"); await pg.wait_for_timeout(800)
        # Artifacts
        await tab("ARTIFACTS"); await pg.wait_for_timeout(5000); await shot(pg,"artifacts")
        print("artifact buttons:", await pg.evaluate("[...document.querySelectorAll('#app [class*=art] button, #app [class*=art] a')].filter(e=>e.offsetParent).map(e=>e.className+'|'+e.innerText.trim().slice(0,30)).slice(0,20)"))
        # Skin
        await pg.locator("[data-skin='stock']").first.click(); await pg.wait_for_timeout(2500); await shot(pg,"skin_stock")
        await tab("HOME"); await pg.wait_for_timeout(3000); await shot(pg,"skin_stock_home")
        await pg.locator("[data-skin='holo']").first.click(); await pg.wait_for_timeout(1500)
        await b.close()
asyncio.run(main())
