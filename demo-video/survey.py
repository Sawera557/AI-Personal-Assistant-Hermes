import asyncio, json, sys
from playwright.async_api import async_playwright
ROUTES=["/","/system","/agent","/billing","/meetings","/context","/tasks","/multi-agents","/usage","/settings"]
async def main():
    async with async_playwright() as p:
        b=await p.chromium.launch(channel="chrome",headless=True)
        pg=await b.new_page(viewport={"width":1920,"height":1080})
        errs=[]; pg.on("console",lambda m: m.type=="error" and errs.append(m.text[:150]))
        for r in ROUTES:
            await pg.goto("http://localhost:5000"+r); await pg.wait_for_timeout(4000)
            name=r.strip("/") or "home"
            await pg.screenshot(path=f"shots/{name}.png")
            info=await pg.evaluate("""()=>[...document.querySelectorAll('button,a,[role=button],select,[role=tab]')].filter(e=>e.offsetParent&&e.getBoundingClientRect().width>0).map(e=>(e.id?'#'+e.id+' ':'')+(e.className&&typeof e.className=='string'?'.'+e.className.split(' ')[0]+' ':'')+'|'+(e.innerText||e.title||e.getAttribute('aria-label')||'').trim().slice(0,40)).slice(0,60)""")
            print("==",r); print("\n".join(info))
        print("ERRORS",errs[:10])
        await b.close()
asyncio.run(main())
