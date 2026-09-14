"""Health-signal tracking: fail open, but count it.

These run offline — no API keys, no database — so they belong in CI. The
condition they guard against is real: memory extraction silently stopped for a
month because every failure path returned a default and nothing counted it.
"""
import pytest

from hermes import health


@pytest.fixture(autouse=True)
def _clean():
    health.reset()
    yield
    health.reset()


def test_guarded_returns_value_and_records_ok():
    assert health.guarded("rag", lambda: "hit") == "hit"
    assert health.snapshot(include_db=False)["signals"]["rag"]["ok"] == 1


def test_guarded_swallows_the_exception_exactly_as_before():
    """The whole point is that behaviour does NOT change — only visibility."""
    assert health.guarded("rag", lambda: 1 / 0, default="") == ""


def test_a_failure_is_counted_not_lost():
    health.guarded("rag", lambda: 1 / 0)
    sig = health.snapshot(include_db=False)["signals"]["rag"]
    assert sig["failed"] == 1
    assert sig["state"] == "degraded"
    assert "ZeroDivisionError" in sig["last"]["detail"]


def test_empty_is_distinguished_from_failed():
    """A quiet turn and a broken index both return "" — they must not look the
    same, which is precisely what hid the original outage."""
    health.guarded("rag", lambda: "")
    sig = health.snapshot(include_db=False)["signals"]["rag"]
    assert sig["empty"] == 1 and sig["failed"] == 0


def test_a_degraded_signal_reaches_the_degraded_list():
    health.guarded("pinecone", lambda: 1 / 0)
    assert any("Pinecone" in p for p in health.snapshot(include_db=False)["degraded"])


def test_one_quiet_turn_is_not_an_alert():
    """No crying wolf: a single empty result must not report degradation, or
    the health page becomes noise people learn to ignore."""
    health.guarded("conversation_write", lambda: "")
    assert health.snapshot(include_db=False)["degraded"] == []


def test_persistently_empty_eventually_alerts():
    for _ in range(health.EMPTY_MIN_RUNS):
        health.record("conversation_write", "empty")
    snap = health.snapshot(include_db=False)
    assert snap["signals"]["conversation_write"]["state"] == "always-empty"
    assert any("never returned anything" in p for p in snap["degraded"])


def test_signals_where_empty_is_normal_never_alert():
    """Most chats have no attachments; recall is correctly quiet on small talk."""
    for name in health.EMPTY_IS_NORMAL:
        for _ in range(health.EMPTY_MIN_RUNS * 2):
            health.record(name, "empty")
    assert health.snapshot(include_db=False)["degraded"] == []


def test_unused_signal_is_not_reported_healthy():
    """A step that never ran is unknown, not fine."""
    assert health.snapshot(include_db=False)["signals"]["rag"]["state"] == "unused"


def test_record_never_raises_into_a_turn():
    """Health tracking must not become a new way for a chat turn to fail."""
    health.record("rag", "ok", detail=object())
    health.record("not-a-known-signal", "ok")


def test_every_signal_has_a_human_label():
    snap = health.snapshot(include_db=False)["signals"]
    assert set(snap) == set(health.SIGNALS)
    assert all(v["label"] for v in snap.values())
