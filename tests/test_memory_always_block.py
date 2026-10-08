"""The REAL memory_retrieve.build_always_block — the always-tier half of the
standing-rules core (TURN_COST_PLAN step 0.3, hard invariant 2).

Why this file exists: every prompt-level test of the standing-rules core
(tests/test_prompt_invariants.py, tests/test_scheduler_switches.py) stubs
agent.build_always_block, and everywhere else the real one returns "" at its
first line because conftest sets HERMES_DISABLE_DB=1. So the function that
actually ships was never executed by the suite: a regression in it — the
business lookup dropped, rows rendered under a key _format_block doesn't read,
the MEMORY_EXTRACT_ENABLED gate inverted — would silently strip the owner's
standing memories ("never contact tenants by text") from exactly the turns
where they are the only copy in the prompt (tools on, Business context off),
with every test still green.

Offline: the DB gate, the entity lookup and the memories repository are
stubbed at memory_retrieve's own seams; embedding is wired to fail, because
the always tier must never need one.
"""
from __future__ import annotations

from typing import Any

import pytest

from hermes import memory_retrieve
from hermes.config import Config

ENTITY_NH = "entity-nh"

ROWS: dict[str, list[dict[str, Any]]] = {
    "owner": [{"memory_id": "m-owner-1", "text": "Never contact tenants by text; email only."}],
    "business": [{"memory_id": "m-biz-1", "text": "CC the owner on every invoice email."}],
    # Fetched with no business filter (B15), so it must never reach the always
    # block of a locked turn — it could be another business's rule.
    "project": [{"memory_id": "m-proj-1", "text": "Other business's project rule."}],
}

EXPECTED_LOCKED = (
    "Standing rules & preferences:\n"
    "  - Never contact tenants by text; email only.\n"
    "Business:\n"
    "  - CC the owner on every invoice email."
)


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """A live-looking memory layer. Returns the log of repository calls."""
    calls: list[dict[str, Any]] = []

    def always_inject_memories(*, scope_type, entity_id=None, project_id=None, limit=None):
        calls.append({
            "scope_type": scope_type, "entity_id": entity_id,
            "project_id": project_id, "limit": limit,
        })
        if scope_type == "business" and entity_id != ENTITY_NH:
            return []
        return list(ROWS.get(scope_type, []))

    def no_embedding(*a: Any, **kw: Any) -> Any:
        raise AssertionError("the always tier must not embed anything")

    monkeypatch.setattr(memory_retrieve, "_db_off", lambda: False)
    monkeypatch.setattr(
        memory_retrieve, "_resolve_entity_id",
        lambda active_business: ENTITY_NH if active_business == "nh" else None,
    )
    monkeypatch.setattr(memory_retrieve.mem_repo, "always_inject_memories", always_inject_memories)
    monkeypatch.setattr(memory_retrieve, "embed", no_embedding)
    monkeypatch.setattr(Config, "MEMORY_EXTRACT_ENABLED", True)
    return calls


def test_locked_turn_renders_owner_and_business_rows_only(repo: list[dict[str, Any]]) -> None:
    block = memory_retrieve.build_always_block(active_business="nh")

    assert block == EXPECTED_LOCKED
    assert "Other business's project rule." not in block, "project rows leak across businesses (B15)"
    assert [c["scope_type"] for c in repo] == ["owner", "business"]
    assert repo[1]["entity_id"] == ENTITY_NH
    assert all(c["limit"] == Config.MEMORY_ALWAYS_INJECT_MAX for c in repo)


def test_matches_the_full_blocks_always_section_for_the_same_rows(
    repo: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same rows, same shape: the always-only block is exactly what the full
    memory block renders for the always tier (no message text, so no semantic
    tier; no project rows, which only the full block fetches)."""
    monkeypatch.setitem(ROWS, "project", [])
    always = memory_retrieve.build_always_block(active_business="nh")
    full = memory_retrieve.build_memory_block(active_business="nh", latest_user_text="")
    assert always == full == EXPECTED_LOCKED


@pytest.mark.parametrize("scope", [None, "all"])
def test_unlocked_turn_carries_owner_rows_and_never_looks_up_a_business(
    scope: str | None, repo: list[dict[str, Any]],
) -> None:
    block = memory_retrieve.build_always_block(active_business=scope)
    assert block == "Standing rules & preferences:\n  - Never contact tenants by text; email only."
    assert [c["scope_type"] for c in repo] == ["owner"]


def test_memory_kill_switch_returns_nothing_and_reads_nothing(
    repo: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Config, "MEMORY_EXTRACT_ENABLED", False)
    assert memory_retrieve.build_always_block(active_business="nh") == ""
    assert repo == []


def test_no_database_returns_nothing_and_reads_nothing(
    repo: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The suite's own default (HERMES_DISABLE_DB=1): "" without a lookup."""
    monkeypatch.setattr(memory_retrieve, "_db_off", lambda: True)
    assert memory_retrieve.build_always_block(active_business="nh") == ""
    assert repo == []


def test_a_failing_repository_fails_open_to_no_memory(
    repo: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(**kw: Any) -> list[dict[str, Any]]:
        raise RuntimeError("connection reset")

    monkeypatch.setattr(memory_retrieve.mem_repo, "always_inject_memories", broken)
    assert memory_retrieve.build_always_block(active_business="nh") == ""
