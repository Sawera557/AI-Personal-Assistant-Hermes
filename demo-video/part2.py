"""Storyboard part 2 — everything after Jarvis: live agent use, run reports,
the reviews scraper and its report, artifacts, and skins."""
import asyncio, sys, time

REHEARSE = "--rehearse" in sys.argv

AGENT_Q = "Which apps are you connected to right now?"


async def storyboard2(D, log):
    pg, T = D.pg, D.text

    async def cur(x, y, ms=800):          # for content inside iframes (viewer panels)
        await pg.evaluate(f"__cur.move({x},{y},{ms})")
        await asyncio.sleep(ms / 1000)
    await pg.evaluate("__cur.move(960,620,10)")
    await asyncio.sleep(1.0)
    log("splice")

    # S4 — Agent, used for real
    await D.tab("AGENT", settle=1.5)
    await D.click(D.vis("#reset-btn"), settle=1.0)
    narr = D.say("n09a_agent")
    await asyncio.sleep(1.5)
    await D.point(D.vis("#agent-mode-agent"), hover=False)
    await asyncio.sleep(2.0)
    await D.click(D.vis("#input"), settle=0.3)
    await pg.keyboard.type(AGENT_Q, delay=55)
    await narr
    await asyncio.sleep(0.3)
    if REHEARSE:
        await pg.fill("#input", "")
        log("agent-skipped (rehearsal)")
    else:
        await D.click(D.vis("#send-btn"), settle=0.2)
    log("agent-sent")
    await D.narrate("n09b_agent_wait", tail=0.2)
    end = time.time() + 400
    while time.time() < end and await pg.evaluate("document.getElementById('send-btn').disabled"):
        await asyncio.sleep(0.5)
    log("agent-done")
    await asyncio.sleep(1.0)

    async def chat_wheel(n):
        await pg.mouse.move(1000, 600)
        for _ in range(n):
            await pg.mouse.wheel(0, 90); await asyncio.sleep(0.1)
    await D.narrate("n09c_agent_answer", beats=[(0.15, lambda: chat_wheel(6)), (0.55, lambda: chat_wheel(6))], tail=0.6)

    # S5 — Billing
    await D.tab("BILLING", settle=1.0)
    await D.click(D.vis(".reg-tab:has-text('REGISTER')"), settle=2.5)
    await D.narrate("n10_billing", beats=[
        (0.10, lambda: D.point(T("monthly run-rate"))),
        (0.45, lambda: D.point(T("past due"))),
        (0.75, lambda: D.point(T("unverified"))),
    ], tail=0.2)
    await D.narrate("n10b_proposal", beats=[
        (0.0, lambda: D.point(T("Approve & write to sheet"), ms=1000, hover=False)),
        (0.55, lambda: D.point(T("See the document"))),
    ], tail=0.3)
    await D.click(D.vis(".reg-tab:has-text('DOCUMENTS')"), settle=1.5)
    await D.narrate("n10c_documents", beats=[(0.3, lambda: D.point(T("Check payments"), hover=False))], tail=0.4)

    # S6 — Meeting Copilot
    await D.tab("MEETING", settle=1.5)
    await D.narrate("n11_meeting", beats=[
        (0.05, lambda: D.point(D.vis("#v2-meeting-pick"))),
        (0.45, lambda: D.point(T("Nour Hammam x"))),
        (0.75, lambda: D.point(T("Review Kenya"))),
    ], tail=0.4)

    # S7 — Context
    await D.tab("CONTEXT", settle=1.0)
    await D.click(D.vis("#context-subtab-memories"), settle=2.0)
    await D.narrate("n12_memories", beats=[(0.45, lambda: D.point(T("Rent and money figures")))], tail=0.3)
    await D.click(D.vis("#context-subtab-skills"), settle=1.5)
    await D.narrate("n12b_skills", beats=[
        (0.25, lambda: D.point(T("Monthly Digest"))),
        (0.45, lambda: D.point(T("Month-End Close"))),
        (0.65, lambda: D.point(T("Competitor Analysis"))),
    ], tail=0.4)

    # S8 — Multi-agents
    await D.tab("MULTI-AGENTS", settle=2.0)
    await D.narrate("n13_agents", beats=[
        (0.15, lambda: D.point(T("Commercial Leasing"))),
        (0.30, lambda: D.point(T("Farm Operations"))),
        (0.45, lambda: D.point(T("Markets & Buyers"))),
        (0.58, lambda: D.point(T("House Scout"))),
        (0.80, lambda: D.point(T("read-only"), hover=False)),
    ], tail=0.4)

    # S9 — Scheduled tasks, then a real run report
    await D.tab("TASKS", settle=2.0)
    await D.narrate("n14_tasks", beats=[
        (0.15, lambda: D.point(T("Urgent email triage"))),
        (0.30, lambda: D.point(T("Daily attention report"))),
        (0.45, lambda: D.point(T("AFK payroll cash check"))),
        (0.70, lambda: D.point(T("Kenya Window morning brief"))),
    ], tail=0.2)
    await D.click(T("Kenya Window morning brief"), settle=3.0)
    run = pg.locator(".tk-run-head:has(.tk-run-badge.success)").filter(visible=True).first
    await D.click(run, settle=1.2)
    rep = pg.locator(".tk-run-reply").filter(visible=True).first
    await rep.evaluate("e => e.scrollIntoView({behavior: 'smooth', block: 'start'})")
    log("task-report")

    async def rep_wheel(n):
        bx = await rep.bounding_box()
        if bx:
            await pg.mouse.move(bx["x"] + bx["width"] / 2, min(bx["y"] + 200, 800))
            for _ in range(n):
                await pg.mouse.wheel(0, 90); await asyncio.sleep(0.12)
    await D.narrate("n14b_report", beats=[
        (0.05, lambda: D.point(rep, hover=False)),
        (0.40, lambda: rep_wheel(6)),
        (0.70, lambda: rep_wheel(6)),
    ], tail=0.6)
    await D.click(D.vis("#tk-detail-close"), settle=1.0)

    # S10 — Jobs: the Google reviews scraper and its report
    await D.tab("JOBS", settle=2.0)
    narr = D.say("n18_jobs")
    await asyncio.sleep(1.2)
    await D.click(T("Google Reviews Scraper"), settle=2.5)
    await D.click(D.vis("#jb-runs .tk-run-head"), settle=1.2)
    await D.point(D.vis(".jb-counts"), hover=False)
    await narr
    await asyncio.sleep(0.3)
    await D.click(D.vis(".jb-report-link"), settle=2.5)   # opens in Hermes's side viewer
    log("reviews-report")

    async def rwheel(n):
        await pg.mouse.move(1540, 700)
        for _ in range(n):
            await pg.mouse.wheel(0, 110); await asyncio.sleep(0.09)
    narr = D.say("n18b_report")
    await cur(1720, 580)
    await asyncio.sleep(1.5)
    await rwheel(7)       # executive summary
    await asyncio.sleep(3.0)
    await rwheel(10)      # markets
    await asyncio.sleep(3.0)
    await rwheel(8)       # most-wanted services
    await narr
    await asyncio.sleep(1.0)
    await D.click(D.vis(".hx-v-btn[title^='Close']"), settle=0.8)
    await D.click(D.vis("#jb-detail-close"), settle=0.8)

    # S11 — Artifacts: a live dashboard, then generated files
    await D.tab("ARTIFACTS", settle=2.5)
    await D.click(T("Scanned Receipts Dashboard"), settle=3.0)
    log("artifact-open")

    async def vwheel(n):
        await pg.mouse.move(1500, 650)
        for _ in range(n):
            await pg.mouse.wheel(0, 100); await asyncio.sleep(0.1)
    await D.narrate("n19_artifacts", beats=[
        (0.10, lambda: cur(1256, 420)),
        (0.35, lambda: cur(1500, 640)),
        (0.55, lambda: vwheel(7)),
    ], tail=0.4)
    await D.click(D.vis(".hx-v-btn[title^='Close']"), settle=1.0)
    await D.click(D.vis(".lib-subtab:has-text('FILES')"), settle=2.0)
    await D.narrate("n19b_files", beats=[
        (0.2, lambda: D.point(T("Steam_Signal_Consolidated"))),
        (0.6, lambda: D.point(T("GLOBALGAP_IFA_Kenya_Report"))),
    ], tail=0.4)

    # S12 — Usage, Settings
    await D.tab("USAGE", settle=2.0)
    await D.narrate("n15_usage", beats=[
        (0.10, lambda: D.point(T("Total credits"))),
        (0.30, lambda: D.point(T("Total cost"))),
        (0.60, lambda: D.point(D.vis(".usage-chip:has-text('LAST 7 DAYS')"), hover=False)),
    ], tail=0.3)
    await D.tab("SETTINGS", settle=2.0)
    await D.narrate("n16_settings", beats=[
        (0.18, lambda: D.point(T("Confirm before write"))),
        (0.40, lambda: D.point(T("Business data isolation"))),
        (0.62, lambda: D.point(T("Block secrets"))),
        (0.80, lambda: D.point(T("Limit sends and deletes"))),
    ], tail=0.4)

    # S13 — Skins
    await D.tab("HOME", settle=2.0)
    stock, holo = D.vis("[data-skin='stock']"), D.vis("[data-skin='holo']")

    async def tab_quick(label):
        await D.click(D.vis(f".strip-tab:has-text('{label}')"), settle=0.2)
    await D.narrate("n20_skins", beats=[
        (0.05, lambda: D.point(holo)),
        (0.32, lambda: D.click(stock, settle=0.1)),
        (0.55, lambda: tab_quick("TASKS")),
        (0.78, lambda: tab_quick("HOME")),
    ], tail=1.5)
    await D.click(holo, settle=2.0)
    log("skin-back")

    # S14 — Outro
    await D.narrate("n17_outro", beats=[(0.3, lambda: D.point(D.vis("#hud-rings"), ms=1200, hover=False))], tail=3.0)
    log("end")
