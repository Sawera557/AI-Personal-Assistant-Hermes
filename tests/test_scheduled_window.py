"""Tests for the Scheduled-Task DATE WINDOW (backfill) logic.

All offline / pure — the window math lives in `hermes.scheduler` precisely so it
can be verified without a database or the agent. Covers:

* `advance_window` — the per-run cursor advance + next_run decision, across the
  catchup / scheduled paces and the range-exhausted boundary.
* `build_run_prompt` — the single-day guardrail injected for a windowed run.
* `describe_window` / `window_batch_position` — the human summaries.
* `initial_next_run` — a catchup window fires immediately.

Run directly:

    python tests/test_scheduled_window.py
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes import scheduler as sch  # noqa: E402

NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
SCHEDULED_NEXT = datetime(2026, 7, 18, 13, 0, tzinfo=timezone.utc)

_passed = 0


def check(label: str, cond: bool) -> None:
    global _passed
    assert cond, f"FAILED: {label}"
    _passed += 1
    print(f"  ok  {label}")


def test_advance_catchup_midrange():
    # Mid-range catchup: claims the cursor day, advances by 1, re-arms to NOW.
    claimed, new_cursor, new_next = sch.advance_window(
        date(2026, 6, 17), date(2026, 7, 17), 1, "catchup", NOW, SCHEDULED_NEXT
    )
    check("catchup claims the cursor day", claimed == date(2026, 6, 17))
    check("catchup advances cursor by step", new_cursor == date(2026, 6, 18))
    check("catchup re-arms next_run to now", new_next == NOW)


def test_advance_catchup_last_day_stops():
    # Claiming the final day advances the cursor past end -> stop firing.
    claimed, new_cursor, new_next = sch.advance_window(
        date(2026, 7, 17), date(2026, 7, 17), 1, "catchup", NOW, SCHEDULED_NEXT
    )
    check("last day is still processed", claimed == date(2026, 7, 17))
    check("cursor moves past end", new_cursor == date(2026, 7, 18))
    check("exhausted range stops firing (next=None)", new_next is None)


def test_advance_scheduled_uses_schedule():
    claimed, new_cursor, new_next = sch.advance_window(
        date(2026, 6, 17), date(2026, 7, 17), 1, "scheduled", NOW, SCHEDULED_NEXT
    )
    check("scheduled pace uses the scheduled slot", new_next == SCHEDULED_NEXT)
    check("scheduled still advances the cursor", new_cursor == date(2026, 6, 18))


def test_advance_step_and_open_ended():
    # step=7 weekly-ish, open-ended (no end) never exhausts.
    claimed, new_cursor, new_next = sch.advance_window(
        date(2026, 6, 1), None, 7, "scheduled", NOW, SCHEDULED_NEXT
    )
    check("step advances by N days", new_cursor == date(2026, 6, 8))
    check("open-ended never stops", new_next == SCHEDULED_NEXT)


def test_advance_no_cursor():
    claimed, new_cursor, new_next = sch.advance_window(
        None, date(2026, 7, 17), 1, "catchup", NOW, SCHEDULED_NEXT
    )
    check("no cursor -> nothing claimed", claimed is None and new_next is None)


def test_full_backfill_walk():
    # Simulate the whole June 17 -> July 17 drain: one claim per run, feeding the
    # advanced cursor back in, until it stops. Expect exactly 31 processed days.
    cursor = date(2026, 6, 17)
    processed = []
    guard = 0
    while cursor is not None and guard < 1000:
        guard += 1
        claimed, cursor, nxt = sch.advance_window(
            cursor, date(2026, 7, 17), 1, "catchup", NOW, SCHEDULED_NEXT
        )
        processed.append(claimed)
        if nxt is None:
            break
    check("processes every day inclusive (31)", len(processed) == 31)
    check("first day is the start", processed[0] == date(2026, 6, 17))
    check("last day is the end", processed[-1] == date(2026, 7, 17))
    check("no gaps/dupes", processed == [date(2026, 6, 17) + __import__("datetime").timedelta(days=i) for i in range(31)])


def test_batch_position():
    task = {"window_start": "2026-06-17", "window_end": "2026-07-17", "window_step": 1}
    check("batch position of start day", sch.window_batch_position(task, date(2026, 6, 17)) == (1, 31))
    check("batch position of end day", sch.window_batch_position(task, date(2026, 7, 17)) == (31, 31))
    check("batch position mid", sch.window_batch_position(task, date(2026, 6, 20)) == (4, 31))


def test_build_prompt_injects_single_day():
    task = {
        "name": "NH WhatsApp backfill",
        "timezone": "America/Chicago",
        "autonomy": "readonly",
        "prompt": "Find Nour Hammam mentions in WhatsApp and write a report.",
        "window_start": "2026-06-17",
        "window_end": "2026-07-17",
        "window_step": 1,
        "window_pace": "catchup",
        "_window_day": "2026-06-17",
    }
    p = sch.build_run_prompt(task)
    check("prompt names the exact single day", "June 17, 2026" in p)
    check("prompt says exactly one day", "EXACTLY ONE day" in p)
    check("prompt shows batch position", "batch 1 of 31" in p)
    check("prompt keeps the READ-ONLY guardrail", "READ-ONLY" in p)
    check("prompt keeps the user's task", "Nour Hammam" in p)

    # No window day -> no window block (ordinary task path unchanged).
    plain = sch.build_run_prompt({"name": "x", "prompt": "hi", "autonomy": "readonly"})
    check("no window block without _window_day", "SINGLE DAY" not in plain)


def test_describe_window():
    base = {"window_pace": "catchup", "window_start": "2026-06-17",
            "window_end": "2026-07-17", "window_step": 1, "window_cursor": "2026-06-20"}
    d = sch.describe_window(base)
    check("describe shows Backfill + range", d.startswith("Backfill") and "Jun 17" in d and "Jul 17" in d)
    check("describe shows days left", "left)" in d)

    done = dict(base, window_cursor="2026-07-18")
    check("describe shows complete when cursor past end", sch.describe_window(done).endswith("complete"))

    check("no window -> None", sch.describe_window({"window_pace": None}) is None)


def test_initial_next_run_catchup_now():
    n = sch.initial_next_run({"frequency": "daily", "times": ["07:00"]}, "America/Chicago", True, "catchup")
    check("catchup initial next_run is ~now", n is not None and abs((n - datetime.now(timezone.utc)).total_seconds()) < 5)
    check("disabled -> None", sch.initial_next_run({}, "UTC", False, "catchup") is None)
    n2 = sch.initial_next_run({"frequency": "daily", "times": ["07:00"]}, "America/Chicago", True, None)
    check("no window uses schedule (future)", n2 is not None and n2 > datetime.now(timezone.utc))


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        print(f"\n{t.__name__}:")
        t()
    print(f"\nPASS: {_passed} checks passed across {len(tests)} tests.")
