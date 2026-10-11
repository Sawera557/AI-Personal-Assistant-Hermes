"""Scheduled tasks and the two Settings switches (TURN_COST_PLAN step 0.3,
owner decision §8 Q2).

  * Apps access OFF: a scheduled run is NOT run. It is recorded as a 'skipped'
    run whose reply says "Apps access is off", nothing is delivered, and a
    date-window task keeps its cursor so the skipped day isn't lost. Before the
    split the Apps switch did nothing at all, so a task kept running with full
    tools while the owner believed every app was off.
  * Business context OFF: ignored. An unattended turn (interactive=False) gets
    full context and its tools. Before, Context off silently stripped a
    scheduled run of every tool — it "succeeded" with an answer that never
    looked anything up.
  * One reading per fire: execute_and_record reads the Apps switch once and
    hands that reading to run_task and on to the agent. A second read could
    contradict the first if the owner flips the switch mid-fire — a windowed
    day claimed and then skipped (lost), or a run the scheduler chose to
    execute going out tool-less and being delivered as a "success".
  * AI meeting notes, the other unattended caller of §8 Q2: Apps off returns
    an explicit skipped result without calling the agent, and the endpoint
    refuses (409) without overwriting the meeting's saved notes. They run as an
    unattended read-only job (B19), so the Context switch doesn't strip them.

Offline: the run repository, delivery and the provider are stubbed; nothing
reaches a database or the network.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from hermes import agent, apps_access, context_mode, context_packs, credits, models, scheduler
from hermes.memory import ConversationStore

HAIKU = "claude-haiku-4-5-20251001"
TASK_ID = "11111111-1111-1111-1111-111111111111"


def _task(**extra: Any) -> dict[str, Any]:
    return {
        "task_id": TASK_ID,
        "name": "Overdue rent report",
        "prompt": "Summarise any overdue rent and report it.",
        "business": "nh",
        "autonomy": "readonly",
        "enabled": True,
        "schedule": {"frequency": "daily", "times": ["07:00"]},
        "timezone": "America/Chicago",
        **extra,
    }


class _Repo:
    """Stand-in for hermes.db.scheduled_tasks: records every write."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def claim_due(self, task_id, next_run_at, now=None):
        self.calls.append(("claim_due", task_id))
        return True

    def claim_due_window(self, task_id, scheduled_next, now=None):
        self.calls.append(("claim_due_window", task_id))
        return date(2026, 10, 1)

    def start_run(self, task_id, trigger="schedule"):
        self.calls.append(("start_run", trigger))
        return "22222222-2222-2222-2222-222222222222"

    def finish_run(self, run_id, task_id, **kw):
        self.calls.append(("finish_run", kw))

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def finished(self) -> dict[str, Any]:
        return next(kw for name, kw in self.calls if name == "finish_run")


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch) -> _Repo:
    import hermes.db.scheduled_tasks as tasks_db
    import hermes.notify as notify

    fake = _Repo()
    for name in ("claim_due", "claim_due_window", "start_run", "finish_run"):
        monkeypatch.setattr(tasks_db, name, getattr(fake, name))
    fake.delivered = []  # type: ignore[attr-defined]
    monkeypatch.setattr(
        notify, "deliver_run_result",
        lambda task, run_id, result: fake.delivered.append(result),  # type: ignore[attr-defined]
    )
    return fake


def _forbid_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    def must_not_run(*a: Any, **kw: Any) -> dict[str, Any]:
        raise AssertionError("the agent ran while Apps access was off")

    monkeypatch.setattr(agent, "run_turn", must_not_run)


# ---- Apps access off: skip, record, don't deliver --------------------------
def test_run_task_skips_without_running_when_apps_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apps_access, "enabled", lambda: False)
    _forbid_agent(monkeypatch)
    result = scheduler.run_task(_task())
    assert result["skipped"] == scheduler.APPS_OFF_REASON == "Apps access is off"
    assert result["stop_reason"] == "skipped"
    assert result["reply"].startswith("Skipped: Apps access is off")
    assert result["tool_calls"] == [] and result["cost_usd"] == 0.0


@pytest.mark.parametrize("trigger", ["schedule", "manual"])
def test_execute_and_record_records_a_skipped_run(
    trigger: str, repo: _Repo, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(apps_access, "enabled", lambda: False)
    _forbid_agent(monkeypatch)

    summary = scheduler.execute_and_record(_task(), trigger=trigger)

    assert summary["status"] == "skipped"
    assert summary["reason"] == "Apps access is off"
    assert summary["run_id"]
    finished = repo.finished()
    assert finished["status"] == "skipped"
    assert finished["reply"].startswith("Skipped: Apps access is off")
    assert finished["set_next"] is False
    assert repo.delivered == [], "a skipped run must not deliver anything"
    # A scheduled fire still claims, so the schedule moves on instead of
    # re-firing (and re-recording a skip) on every poll.
    assert ("claim_due" in repo.names()) == (trigger == "schedule")


def test_windowed_task_keeps_its_cursor_while_apps_off(repo: _Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    """Claiming a window day advances the cursor; a skipped run would then lose
    that day for good. So the skip takes the plain claim (next_run_at only)."""
    monkeypatch.setattr(apps_access, "enabled", lambda: False)
    _forbid_agent(monkeypatch)

    task = _task(window_pace="scheduled", window_cursor="2026-10-01")
    summary = scheduler.execute_and_record(task, trigger="schedule")

    assert summary["status"] == "skipped"
    assert "claim_due_window" not in repo.names()
    assert "claim_due" in repo.names()
    assert "_window_day" not in task


def test_apps_on_runs_and_delivers_as_before(repo: _Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apps_access, "enabled", lambda: True)
    ran: list[dict[str, Any]] = []
    decided: list[bool | None] = []

    def fake_run_task(task: dict[str, Any], *, apps_on: bool | None = None) -> dict[str, Any]:
        ran.append(task)
        decided.append(apps_on)
        return {"reply": "Two invoices overdue.", "tool_calls": [], "stop_reason": "end_turn"}

    monkeypatch.setattr(scheduler, "run_task", fake_run_task)
    task = _task(window_pace="scheduled", window_cursor="2026-10-01")
    summary = scheduler.execute_and_record(task, trigger="schedule")

    assert summary["status"] == "success"
    assert ran and ran[0]["_window_day"] == "2026-10-01"
    assert decided == [True], "execute_and_record must hand run_task its own reading"
    assert "claim_due_window" in repo.names()
    assert repo.finished()["status"] == "success"
    assert len(repo.delivered) == 1


# ---- Business context off: ignored by unattended runs ----------------------
def _install_agent(monkeypatch: pytest.MonkeyPatch, *, apps: bool, context: bool) -> list[dict[str, Any]]:
    """Real run_task -> run_turn -> stream_turn, with the provider, retrieval
    and every DB reader stubbed. Returns the captured provider calls."""
    import hermes.conversation_retrieve as conversation_retrieve
    import hermes.db.conversation_memory as conversation_memory
    import hermes.db.playbooks as playbooks
    import hermes.memory as memory
    import hermes.rag as rag

    calls: list[dict[str, Any]] = []

    def fake_turn(model_id, tools, system, messages, *a, **kw):
        calls.append({"tools": [t.get("name") for t in tools], "system": system})
        yield {"type": "text", "text": "Done."}
        yield {
            "type": "_final", "content": [{"type": "text", "text": "Done."}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    monkeypatch.setattr(agent, "_anthropic_turn", fake_turn)
    monkeypatch.setattr(agent, "_openai_turn", fake_turn)
    monkeypatch.setattr(agent, "build_memory_block", lambda **kw: "SEMANTICMEM")
    monkeypatch.setattr(agent, "build_always_block", lambda **kw: "ALWAYSMEM")
    monkeypatch.setattr(context_packs, "packs_for", lambda *a, **kw: "PACKBODY")
    monkeypatch.setattr(rag, "build_context_block", lambda **kw: "RAGDOC")
    monkeypatch.setattr(conversation_retrieve, "build_history_block", lambda **kw: "")
    monkeypatch.setattr(conversation_memory, "owner_user_id", lambda: "owner")
    monkeypatch.setattr(playbooks, "playbook_for_task", lambda task_id: None)

    credits.clear()
    monkeypatch.setattr(models, "_configured", lambda: HAIKU)
    monkeypatch.setattr(apps_access, "enabled", lambda: apps)
    monkeypatch.setattr(context_mode, "enabled", lambda: context)
    monkeypatch.delenv("HERMES_SPLIT_TOGGLES", raising=False)
    monkeypatch.delenv("HERMES_STANDING_RULES_CORE", raising=False)

    store = ConversationStore()
    monkeypatch.setattr(agent, "store", store)
    monkeypatch.setattr(memory, "store", store)
    return calls


def test_scheduled_run_ignores_context_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Context off, Apps on, split flag at its default (off): interactive chat
    would lose its tools here — a scheduled run must not."""
    calls = _install_agent(monkeypatch, apps=True, context=False)
    result = scheduler.run_task(_task())

    assert result["reply"] == "Done."
    assert calls, "the run never reached the provider"
    assert calls[0]["tools"], "Context off stripped a scheduled run's tools"
    system = calls[0]["system"]
    for marker in ("PACKBODY", "SEMANTICMEM", "RAGDOC"):
        assert marker in system, f"scheduled run lost its {marker}"
    assert "business context is switched off" not in system


def test_interactive_turn_still_honours_context_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The contrast that makes the test above meaningful: the same switches on
    an interactive turn keep today's plain-chat behaviour."""
    calls = _install_agent(monkeypatch, apps=True, context=False)
    agent.run_turn("chat-session", "hi", active_business="nh")
    assert calls[0]["tools"] == []
    assert "PACKBODY" not in calls[0]["system"]


def test_meeting_notes_run_as_an_unattended_read_only_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """generate_ai_notes reads untrusted Gmail/Drive content with nobody there
    to approve anything (B19), so it runs like the scheduler: interactive=False
    with readonly autonomy. It therefore ignores the Context switch (it keeps
    its read tools with Context off) and is offered no write, send, delete,
    task-authoring or guardrail tool. (Apps off is covered by
    test_meeting_notes_skip_while_apps_off.)"""
    from hermes import meeting_copilot, tools as hermes_tools

    calls = _install_agent(monkeypatch, apps=True, context=False)
    out = meeting_copilot.generate_ai_notes(
        business_slug="nh", business_name="Nour Hammam", meeting_title="Weekly sync",
    )
    assert out["notes"] == "Done."
    offered = set(calls[0]["tools"])
    assert {"maton_request", "search_business_knowledge"} <= offered
    assert not offered & (hermes_tools._WRITE_TOOLS | hermes_tools._INTERACTIVE_ONLY_TOOLS)


def test_scheduled_run_with_apps_off_never_reaches_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_agent(monkeypatch, apps=False, context=True)
    result = scheduler.run_task(_task())
    assert result["skipped"] == "Apps access is off"
    assert calls == []


def test_skipped_result_has_the_agent_result_shape() -> None:
    """Every caller renders a run result the same way; the skip must not need
    a special case beyond its status."""
    skipped = scheduler._apps_off_result()
    real_keys = {"reply", "thinking", "tool_calls", "stop_reason", "usage", "cost_usd", "model_used"}
    assert real_keys <= set(skipped)
    assert isinstance(skipped["usage"], dict) and skipped["model_used"] is None


# ---- One reading of the Apps switch per fire / per notes run ---------------
def _flips_off_after_first_read(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The owner switching Apps off a moment after the first read: on for the
    first read, off for every read after it. Returns the read log."""
    reads: list[int] = []

    def enabled() -> bool:
        reads.append(1)
        return len(reads) == 1

    monkeypatch.setattr(apps_access, "enabled", enabled)
    return reads


def test_one_reading_decides_the_whole_scheduled_fire(
    repo: _Repo, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """execute_and_record read Apps ON and claimed a window day; a flip after
    that read must not make run_task (or the agent) skip or strip the run —
    the claimed day would be lost, or a tool-less ADVISOR answer delivered as a
    success. The flip takes effect at the next fire."""
    calls = _install_agent(monkeypatch, apps=True, context=True)
    reads = _flips_off_after_first_read(monkeypatch)

    task = _task(window_pace="scheduled", window_cursor="2026-10-01")
    summary = scheduler.execute_and_record(task, trigger="schedule")

    assert len(reads) == 1, f"the Apps switch was read {len(reads)} times for one fire"
    assert summary["status"] == "success", summary
    assert "claim_due_window" in repo.names()
    assert repo.finished()["status"] == "success"
    assert calls and calls[0]["tools"], "the run the scheduler decided to execute lost its tools"
    assert "apps access is switched off" not in calls[0]["system"]
    assert len(repo.delivered) == 1


def test_run_task_with_a_decided_switch_does_not_reread(monkeypatch: pytest.MonkeyPatch) -> None:
    """apps_on=False from the caller skips without consulting the switch; a
    direct caller (apps_on omitted) still reads it, as before."""
    def must_not_read() -> bool:
        raise AssertionError("run_task re-read the Apps switch its caller already read")

    monkeypatch.setattr(apps_access, "enabled", must_not_read)
    _forbid_agent(monkeypatch)
    assert scheduler.run_task(_task(), apps_on=False)["skipped"] == "Apps access is off"


def test_meeting_notes_skip_while_apps_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apps off: no provider call, an explicit skipped result, empty notes —
    never a tool-less ADVISOR reply that the endpoint would save over the
    meeting's good notes. Same wording as the scheduler's skipped runs."""
    from hermes import meeting_copilot

    calls = _install_agent(monkeypatch, apps=False, context=True)
    out = meeting_copilot.generate_ai_notes(
        business_slug="nh", business_name="Nour Hammam", meeting_title="Weekly sync",
    )
    assert out == {"notes": "", "tool_calls": [], "skipped": scheduler.APPS_OFF_REASON}
    assert calls == [], "meeting notes reached the provider while Apps access was off"


def test_meeting_notes_read_the_switch_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reading that let the notes run is the one the agent uses."""
    from hermes import meeting_copilot

    calls = _install_agent(monkeypatch, apps=True, context=True)
    reads = _flips_off_after_first_read(monkeypatch)
    out = meeting_copilot.generate_ai_notes(
        business_slug="nh", business_name="Nour Hammam", meeting_title="Weekly sync",
    )
    assert len(reads) == 1
    assert out["notes"] == "Done." and "skipped" not in out
    assert calls and calls[0]["tools"]


def test_ai_notes_endpoint_refuses_and_keeps_saved_notes_while_apps_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app as app_module
    from fastapi.testclient import TestClient

    monkeypatch.setattr(apps_access, "enabled", lambda: False)
    _forbid_agent(monkeypatch)
    saved: list[Any] = []
    monkeypatch.setattr(app_module.meetings_db, "save_ai_notes", lambda *a, **kw: saved.append(a))
    monkeypatch.setattr(
        app_module, "list_businesses", lambda: [{"slug": "nh", "name": "Nour Hammam"}],
    )

    r = TestClient(app_module.app).post(
        "/api/meetings/sessions/33333333-3333-3333-3333-333333333333/ai-notes",
        json={"business": "nh", "meeting_title": "Weekly sync"},
    )

    assert r.status_code == 409, r.text
    body = r.json()
    assert body["skipped"] == "Apps access is off"
    # The meeting page alerts `error` verbatim (static/meeting.js).
    assert body["error"].startswith("Apps access is off.")
    assert saved == [], "a skipped notes run must not overwrite the saved notes"
