"""HTTP routes for review reports — serves the per-day HTML intelligence report.

Each reviews-scraper run stores a self-contained report at
``<REVIEWS_S3_PREFIX>/reports/<date>.html`` in S3 (see hermes/reviews_report.py).
This router serves it back at a stable, clickable URL — the "open report" link
shown on each run in the Jobs UI. Regenerating the same date overwrites the
object, so the link always shows that day's latest cumulative data.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse

from . import reviews_report

router = APIRouter(prefix="/api/reviews", tags=["reviews"])


@router.get("/report/{date}")
async def get_report(date: str):
    """Return the stored HTML report for a date (YYYY-MM-DD)."""
    html = await run_in_threadpool(reviews_report.load_report_html, date)
    if html is None:
        return JSONResponse(
            {"error": f"No report found for {date}. It's generated when the reviews job runs."},
            status_code=404,
        )
    return HTMLResponse(html)
