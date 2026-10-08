"""One read for history plus conversation state, and persist before `done`.

TURN_COST_PLAN step 0.6 (bugs B22, B23). What must hold:

  * `store.get_with_state(key)` returns exactly what `get(key)` returns, plus a
    TurnState describing the previous assistant reply — on BOTH backends, and
    identically for the same transcript (one shared derivation in
    hermes/turn_state.py, so they cannot drift).
  * A failed read is explicit: ok=False, never a silent "new chat" (B23).
  * The Postgres version is one statement: the existing SELECT plus projected
    metadata columns and a scalar user-row count — not a second round-trip.
  * Every pending kind the router's rule 3 needs is detected: a held Guardrail
    #1 confirmation, an email_with_attachment draft, a Maton OAuth link.
  * stream_turn reads through get_with_state when the store has it and falls
    back to get() otherwise, with the provider payload and `turn_index`
    unchanged either way.
  * /api/chat/stream writes the turn to the store BEFORE the `done` event goes
    out (B22), and keeps the background extraction writes after it.

Offline: Postgres is a fake cursor over an in-memory table that answers the
exact statements record_turn / get / get_with_state issue; the provider, the
retrieval blocks and the background writers are stubbed.
"""
from __future__ import annotations

import asyncio
import copy
import json
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from hermes import agent, context_mode, credits, guardrails, health, models, turn_state
from hermes.db import conversations as pg
from hermes.memory import ConversationStore
from hermes.turn_state import (
    PENDING_DRAFT,
    PENDING_GUARDRAIL,
    PENDING_OAUTH,
    TurnState,
    pending_from,
    route_tier,
)

HAIKU = "claude-haiku-4-5-20251001"


# ---- a fake Postgres ------------------------------------------------------
class FakeDB:
    """An in-memory `hermes_messages` table behind a psycopg-shaped cursor.

    It answers only the statements the conversation store issues, matched on
    their leading text, so a changed query shows up as a loud failure rather
    than a silently wrong answer. jsonb is stored decoded, the way psycopg 3
    hands it back; `->` yields the decoded value and `->>` the text."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.rows: list[dict[str, Any]] = []
        self.statements: list[str] = []

    @contextmanager
    def cursor(self, commit: bool = True):
        yield FakeCursor(self)


class FakeCursor:
    def __init__(self, db: FakeDB) -> None:
        self.db = db
        self._result: list[tuple] = []

    def _session(self, session_id: str) -> list[dict[str, Any]]:
        return [r for r in self.db.rows if r["session_id"] == session_id]

    def execute(self, sql: str, params: tuple = ()) -> None:
        if self.db.fail:
            raise RuntimeError("connection reset by peer")
        s = " ".join(sql.split())
        self.db.statements.append(s)
        if s.startswith("SELECT user_id FROM users"):
            self._result = [("00000000-0000-0000-0000-00000000000a",)]
        elif s.startswith("INSERT INTO hermes_sessions"):
            self._result = []
        elif s.startswith("SELECT COALESCE(max(seq), 0) FROM hermes_messages"):
            (session_id,) = params
            self._result = [(max((r["seq"] for r in self._session(session_id)), default=0),)]
        elif s.startswith("INSERT INTO hermes_messages") and "'user'" in s:
            session_id, _entity, content, seq = params
            self.db.rows.append({"session_id": session_id, "role": "user",
                                 "content": content, "seq": seq, "metadata": {}})
        elif s.startswith("INSERT INTO hermes_messages") and "'assistant'" in s:
            session_id, _entity, content, seq, _model, meta = params
            self.db.rows.append({"session_id": session_id, "role": "assistant",
                                 "content": content, "seq": seq, "metadata": json.loads(meta)})
        elif s.startswith("INSERT INTO ai_runs"):
            self._result = []
        elif s.startswith(
            "SELECT role, content, metadata->'tool_calls', metadata->'route', "
            "metadata->>'stop_reason', metadata->'pending', (SELECT count(*) FROM hermes_messages u "
            "WHERE u.session_id = %s AND u.role = 'user') FROM hermes_messages "
            "WHERE session_id = %s AND role IN ('user', 'assistant') ORDER BY seq DESC LIMIT %s"
        ):
            count_session, session_id, limit = params
            user_count = sum(1 for r in self._session(count_session) if r["role"] == "user")
            window = sorted(self._session(session_id), key=lambda r: r["seq"], reverse=True)[:limit]
            self._result = [
                (r["role"], r["content"], r["metadata"].get("tool_calls"),
                 r["metadata"].get("route"), r["metadata"].get("stop_reason"),
                 r["metadata"].get("pending"), user_count)
                for r in window
            ]
        elif s.startswith(
            "SELECT role, content FROM hermes_messages WHERE session_id = %s "
            "AND role IN ('user', 'assistant') ORDER BY seq DESC LIMIT %s"
        ):
            session_id, limit = params
            window = sorted(self._session(session_id), key=lambda r: r["seq"], reverse=True)[:limit]
            self._result = [(r["role"], r["content"]) for r in window]
        else:
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


@pytest.fixture(autouse=True)
def _history_window(monkeypatch):
    """Pin the replay window so the developer's .env can't shrink it under a
    test; the window tests override it explicitly."""
    monkeypatch.setenv("HERMES_HISTORY_MESSAGES", "40")


@pytest.fixture
def db(monkeypatch) -> FakeDB:
    fake = FakeDB()
    monkeypatch.setattr(pg, "cursor", fake.cursor)
    return fake


# ---- transcripts ----------------------------------------------------------
CHAT_ROUTE = {"mode": "auto", "tier": "chat", "final": "chat",
              "escalated": False, "reason_codes": ["greeting"]}
ESCALATED_ROUTE = {"mode": "auto", "tier": "chat", "final": "full",
                   "escalated": True, "reason_codes": ["tool_use"]}
FULL_ROUTE = {"mode": "auto", "tier": "full", "final": "full",
              "escalated": False, "reason_codes": ["business"]}

READ_CALL = {"tool": "search_business_knowledge", "input": {"query": "rent"},
             "ok": True, "label": "Searching"}
GUARDRAIL_CALL = {"tool": "send_email", "input": {"to": "a@example.com"}, "ok": True,
                  "label": "Sending", "awaiting_confirmation": True}
DRAFT_CALL = {"tool": "email_with_attachment", "input": {"to": "a@example.com"},
              "ok": True, "label": "Drafting"}
SENT_DRAFT_CALL = {"tool": "email_with_attachment",
                   "input": {"to": "a@example.com", "confirm": True, "draft_id": "d1"},
                   "ok": True, "label": "Sending"}
OAUTH_CALL = {"tool": "maton_create_connection", "input": {"app": "hubspot"},
              "ok": True, "label": "Connecting"}


def _result(reply: str, *, tool_calls=None, stop_reason="end_turn", route=None) -> dict[str, Any]:
    """A stream_turn `done` result (plus the step 1.2 `route` when given)."""
    out: dict[str, Any] = {
        "reply": reply, "thinking": None, "tool_calls": tool_calls or [],
        "stop_reason": stop_reason, "usage": {"input_tokens": 10, "output_tokens": 2},
        "cost_usd": 0.0001, "model_used": HAIKU,
    }
    if route is not None:
        out["route"] = route
    return out


TRANSCRIPTS: dict[str, list[tuple[str, dict[str, Any]]]] = {
    "empty": [],
    "full_with_tool": [
        ("rent status for GAK?", _result("Two tenants are late.", tool_calls=[READ_CALL])),
    ],
    "chat_run_after_full": [
        ("rent status for GAK?", _result("Two tenants are late.", tool_calls=[READ_CALL])),
        ("thanks", _result("Any time!", route=CHAT_ROUTE)),
        ("good night", _result("Good night!", route=CHAT_ROUTE)),
    ],
    "escalated_chat_breaks_the_run": [
        ("hi", _result("Hello!", route=CHAT_ROUTE)),
        ("hello", _result("I checked your calendar.", tool_calls=[READ_CALL],
                          route=ESCALATED_ROUTE)),
    ],
    "guardrail_pending": [
        ("hi", _result("Hello!", route=CHAT_ROUTE)),
        ("email Amal", _result("Confirm and I'll send it.", tool_calls=[GUARDRAIL_CALL],
                               route=FULL_ROUTE)),
    ],
    "draft_pending": [
        ("send Amal the lease", _result("Draft ready — send it?", tool_calls=[DRAFT_CALL])),
    ],
    "oauth_pending": [
        ("connect hubspot", _result("Open this link to connect.", tool_calls=[OAUTH_CALL])),
    ],
    "truncated_reply": [
        ("summarise the year", _result("Q1 was", stop_reason="max_tokens")),
    ],
    "long_chat_run": [
        (f"hi {i}", _result(f"hello {i}", route=CHAT_ROUTE)) for i in range(5)
    ],
}


def _fill(store, key: str, session_id: str, turns) -> None:
    for user_text, result in turns:
        store.record_turn(key, None, session_id, user_text, copy.deepcopy(result))


def _memory_store(key: str, session_id: str, turns) -> ConversationStore:
    """An in-memory store holding the same conversation, with its working
    message list set the way stream_turn leaves it (text-only here)."""
    store = ConversationStore()
    _fill(store, key, session_id, turns)
    messages: list[dict[str, Any]] = []
    for user_text, result in turns:
        messages.append({"role": "user", "content": [{"type": "text", "text": user_text}]})
        messages.append({"role": "assistant",
                         "content": [{"type": "text", "text": result["reply"]}]})
    store.set(key, messages)
    return store


# ---- the read itself ------------------------------------------------------
def test_a_raising_cursor_is_an_explicit_failed_read(monkeypatch):
    """B23: get() returns [] on a DB error, the same as a new chat. The state
    says the read failed, so nothing downstream mistakes it for one."""
    fake = FakeDB(fail=True)
    monkeypatch.setattr(pg, "cursor", fake.cursor)
    store = pg.PostgresConversationStore()
    key = f"gak:{uuid.uuid4()}"

    messages, state = store.get_with_state(key)

    assert messages == [] == store.get(key)  # the messages get() would return
    assert state.ok is False
    assert state == turn_state.FAILED


def test_the_in_memory_store_reports_a_failed_derivation_but_keeps_history(monkeypatch):
    key, sid = "all:s1", "s1"
    store = _memory_store(key, sid, TRANSCRIPTS["full_with_tool"])

    def boom(*_a, **_k):
        raise RuntimeError("corrupt transcript entry")

    monkeypatch.setattr("hermes.memory.state_from_rows", boom)
    messages, state = store.get_with_state(key)

    assert state.ok is False
    assert messages == store.get(key) and messages  # history still comes back


def test_a_new_conversation_reads_ok_and_empty(db):
    for store in (pg.PostgresConversationStore(), ConversationStore()):
        messages, state = store.get_with_state(str(uuid.uuid4()))
        assert messages == []
        assert state == TurnState()  # ok=True, nothing said, nothing pending
        assert state.ok is True and state.pending is None and state.user_turns == 0


def test_a_non_uuid_key_on_postgres_is_empty_not_failed(db):
    """There is genuinely nothing stored under a non-uuid key (get() says the
    same without touching the DB), so it must not read as a failure."""
    messages, state = pg.PostgresConversationStore().get_with_state("__scheduled__:task-7")
    assert (messages, state.ok) == ([], True)
    assert db.statements == []


def test_postgres_reads_history_and_state_in_one_statement(db):
    store = pg.PostgresConversationStore()
    sid = str(uuid.uuid4())
    key = f"gak:{sid}"
    _fill(store, key, sid, TRANSCRIPTS["chat_run_after_full"])
    db.statements.clear()

    messages, state = store.get_with_state(key)

    assert len(db.statements) == 1, db.statements  # one round-trip, not two
    assert "metadata->'tool_calls'" in db.statements[0]
    assert "count(*)" in db.statements[0]
    # The history the model sees is byte-identical to get()'s.
    assert messages == store.get(key)
    assert state.ok and state.user_turns == 3


# ---- pending kinds --------------------------------------------------------
@pytest.mark.parametrize(
    "calls, kind",
    [
        ([GUARDRAIL_CALL], PENDING_GUARDRAIL),
        ([DRAFT_CALL], PENDING_DRAFT),
        ([OAUTH_CALL], PENDING_OAUTH),
        ([READ_CALL, DRAFT_CALL], PENDING_DRAFT),
        ([OAUTH_CALL, GUARDRAIL_CALL], PENDING_GUARDRAIL),  # first kind in rule order wins
        ([{**DRAFT_CALL, "ok": False}], PENDING_DRAFT),  # conservative: a failed prepare still counts
        ([SENT_DRAFT_CALL], None),  # confirm=true is the send, not a pending draft
        ([READ_CALL], None),
        ([{**GUARDRAIL_CALL, "awaiting_confirmation": False}], None),
        ([], None),
    ],
    ids=["guardrail", "draft", "oauth", "draft-after-read", "guardrail-first",
         "failed-draft", "sent-draft", "read-only", "not-awaiting", "no-tools"],
)
def test_pending_from_detects_each_open_workflow(calls, kind):
    assert pending_from(calls) == kind
    # A whole metadata dict, a JSON string of one, and a tuple are all accepted.
    assert pending_from({"tool_calls": calls, "stop_reason": "end_turn"}) == kind
    assert pending_from(json.dumps({"tool_calls": calls})) == kind
    assert pending_from(tuple(calls)) == kind


@pytest.mark.parametrize("junk", [None, "", "not json", 7, {"tool_calls": "x"}, ["x", None]])
def test_pending_from_never_raises_on_junk(junk):
    assert pending_from(junk) is None


@pytest.mark.parametrize(
    "name, kind",
    [("guardrail_pending", PENDING_GUARDRAIL), ("draft_pending", PENDING_DRAFT),
     ("oauth_pending", PENDING_OAUTH), ("full_with_tool", None)],
)
def test_each_pending_kind_survives_a_round_trip_through_both_stores(db, name, kind):
    sid = str(uuid.uuid4())
    key = f"all:{sid}"
    postgres = pg.PostgresConversationStore()
    _fill(postgres, key, sid, TRANSCRIPTS[name])
    memory = _memory_store(key, sid, TRANSCRIPTS[name])

    assert postgres.get_with_state(key)[1].pending == kind
    assert memory.get_with_state(key)[1].pending == kind


# ---- parity: same transcript, same state ----------------------------------
@pytest.mark.parametrize("name", sorted(TRANSCRIPTS))
def test_postgres_and_in_memory_give_identical_state(db, monkeypatch, name):
    # A small window so the long CHAT run is cut by it on both backends alike.
    monkeypatch.setenv("HERMES_HISTORY_MESSAGES", "6")
    sid = str(uuid.uuid4())
    key = f"gak:{sid}"
    turns = TRANSCRIPTS[name]
    postgres = pg.PostgresConversationStore()
    _fill(postgres, key, sid, turns)
    memory = _memory_store(key, sid, turns)

    pg_messages, pg_state = postgres.get_with_state(key)
    mem_messages, mem_state = memory.get_with_state(key)

    assert pg_state == mem_state
    assert pg_state.ok is True
    assert pg_state.user_turns == len(turns)  # whole conversation, not the window
    # Postgres history is the text replay of the same window get() returns.
    assert pg_messages == postgres.get(key)
    assert mem_messages == memory.get(key)


def test_the_state_describes_the_last_reply():
    sid = "s"
    _, state = _memory_store(sid, sid, TRANSCRIPTS["chat_run_after_full"]).get_with_state(sid)
    assert state.last_assistant_text == "Good night!"
    assert state.last_tool_calls == ()
    assert state.last_stop_reason == "end_turn"
    assert state.last_route == CHAT_ROUTE
    assert state.trailing_chat_exchanges == (("thanks", "Any time!"), ("good night", "Good night!"))
    assert state.pending is None

    _, state = _memory_store(sid, sid, TRANSCRIPTS["full_with_tool"]).get_with_state(sid)
    assert state.last_tool_calls == (READ_CALL,)
    assert state.last_route is None  # written before routing existed: reads as FULL
    assert state.trailing_chat_exchanges == ()

    _, state = _memory_store(sid, sid, TRANSCRIPTS["truncated_reply"]).get_with_state(sid)
    assert state.last_stop_reason == "max_tokens"


def test_an_escalated_chat_reply_ends_the_chat_run():
    """Hard invariant 3: CHAT history never includes a FULL reply, and an
    escalated CHAT turn's reply WAS produced on FULL."""
    _, state = _memory_store("s", "s", TRANSCRIPTS["escalated_chat_breaks_the_run"]).get_with_state("s")
    assert state.trailing_chat_exchanges == ()
    assert route_tier(state.last_route) == "full"


def test_the_chat_run_is_bounded_by_the_history_window(monkeypatch):
    monkeypatch.setenv("HERMES_HISTORY_MESSAGES", "5")  # odd: window opens on an assistant row
    _, state = _memory_store("s", "s", TRANSCRIPTS["long_chat_run"]).get_with_state("s")
    assert state.trailing_chat_exchanges == (("hi 3", "hello 3"), ("hi 4", "hello 4"))
    assert state.user_turns == 5


@pytest.mark.parametrize(
    "route, tier",
    [(CHAT_ROUTE, "chat"), (ESCALATED_ROUTE, "full"), ({"tier": "LIGHT"}, "light"),
     ("chat", "chat"), (None, None), ({}, None), (["chat"], None), (" ", None)],
)
def test_route_tier(route, tier):
    assert route_tier(route) == tier


def test_record_turn_stores_stop_reason_and_route_only_when_given():
    store = ConversationStore()
    store.record_turn("k", None, "k", "hi", {"reply": "hello"})
    store.record_turn("k", None, "k", "hi", _result("hello", route=CHAT_ROUTE))
    plain, routed = [t for t in store.transcript("k") if t["role"] == "assistant"]
    assert "stop_reason" not in plain and "route" not in plain
    assert routed["stop_reason"] == "end_turn" and routed["route"] == CHAT_ROUTE


def test_postgres_metadata_is_unchanged_when_there_is_no_route(db):
    """Today's results carry no `route`, so the persisted metadata keys stay
    exactly what they were before this step."""
    store = pg.PostgresConversationStore()
    sid = str(uuid.uuid4())
    _fill(store, sid, sid, TRANSCRIPTS["full_with_tool"])
    assistant = [r for r in db.rows if r["role"] == "assistant"][0]
    assert set(assistant["metadata"]) == {"tool_calls", "stop_reason", "usage", "cost_usd"}


# ---- stream_turn reads through get_with_state -----------------------------
class _GetOnlyStore:
    """A store fake from before step 0.6: get/set/attachments, no get_with_state."""

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self._messages = messages
        self.saved: list[dict[str, Any]] | None = None

    def get(self, key):
        return copy.deepcopy(self._messages)

    def set(self, key, messages):
        self.saved = messages

    def attachments(self, key):
        return []


class _SpyStore(ConversationStore):
    def __init__(self) -> None:
        super().__init__()
        self.reads: list[str] = []

    def get(self, key):
        self.reads.append("get")
        return super().get(key)

    def get_with_state(self, key):
        self.reads.append("get_with_state")
        return super().get_with_state(key)


OFFERED = {
    "name": "search_business_knowledge",
    "description": "Search the business knowledge base.",
    "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
}


def _run_stream_turn(monkeypatch, store) -> tuple[list[dict], list[int | None], dict]:
    """One turn whose model calls a tool once, then answers. Returns what the
    provider was handed, the `turn` guardrails.check saw, and the result."""
    credits.clear()
    calls: list[dict] = []
    turns_seen: list[int | None] = []
    script = [
        {"content": [{"type": "tool_use", "id": "tu_1", "name": "search_business_knowledge",
                      "input": {"query": "rent"}}], "stop_reason": "tool_use"},
        {"content": [{"type": "text", "text": "Two tenants are late."}], "stop_reason": "end_turn"},
    ]

    def provider(model_id, tools, system, messages, *args, **kwargs):
        calls.append({"system": system, "tools": copy.deepcopy(tools),
                      "messages": copy.deepcopy(messages)})
        step = script[min(len(calls), len(script)) - 1]
        for block in step["content"]:
            if block.get("type") == "text":
                yield {"type": "text", "text": block["text"]}
        yield {"type": "_final", "content": copy.deepcopy(step["content"]),
               "stop_reason": step["stop_reason"],
               "usage": {"input_tokens": 100, "output_tokens": 10}}

    def check(session_id, tool_name, tool_input, active_business=None, interactive=True, turn=None,
              approved_tokens=None):
        turns_seen.append(turn)
        return guardrails.Decision(True, "", None, "allowed")

    monkeypatch.setattr(models, "_configured", lambda: HAIKU)
    monkeypatch.setattr(context_mode, "enabled", lambda: True)
    monkeypatch.setattr(agent, "build_tools", lambda *a, **k: [OFFERED])
    monkeypatch.setattr(agent, "_system_prompt", lambda *a, **k: "system")
    monkeypatch.setattr(health, "guarded", lambda signal, fn, default="": default)
    monkeypatch.setattr(agent, "_anthropic_turn", provider)
    monkeypatch.setattr(agent, "dispatch", lambda *a, **k: ('{"results": []}', False, None, None))
    monkeypatch.setattr(guardrails, "check", check)
    monkeypatch.setattr(agent, "store", store)
    events = list(agent.stream_turn("sess", "how is rent looking?"))
    credits.clear()
    return calls, turns_seen, events[-1]["result"]


def test_stream_turn_reads_once_through_get_with_state_with_an_unchanged_payload(monkeypatch):
    prior = TRANSCRIPTS["chat_run_after_full"]
    spy = _SpyStore()
    _fill(spy, "sess", "sess", prior)
    legacy_messages: list[dict[str, Any]] = []
    for user_text, result in prior:
        legacy_messages.append({"role": "user", "content": [{"type": "text", "text": user_text}]})
        legacy_messages.append({"role": "assistant",
                                "content": [{"type": "text", "text": result["reply"]}]})
    spy.set("sess", copy.deepcopy(legacy_messages))
    legacy = _GetOnlyStore(legacy_messages)

    new_calls, new_turns, new_result = _run_stream_turn(monkeypatch, spy)
    old_calls, old_turns, old_result = _run_stream_turn(monkeypatch, legacy)

    assert spy.reads == ["get_with_state"]  # one read; get() is not also called
    # Byte-identical provider requests whichever read path the store offers.
    assert new_calls == old_calls
    assert len(new_calls) == 2
    # turn_index is unchanged (B5 is a separate fix): today's count of user
    # messages in the replayed history, the new message included.
    assert new_turns == old_turns == [4]
    assert new_result["reply"] == old_result["reply"] == "Two tenants are late."


# ---- /api/chat/stream persists before `done` -----------------------------
class _RecordingStore:
    """Records the order of store writes against everything else that happens."""

    def __init__(self, timeline: list[str]) -> None:
        self.timeline = timeline
        self.recorded: list[tuple] = []

    def get(self, key):
        return []

    def locked_business_slug(self, key):
        return None

    def record_turn(self, key, entity_id, session_id, user_text, result):
        self.timeline.append("record_turn")
        self.recorded.append((key, entity_id, session_id, user_text, result))


def _drive_stream(app, body: dict, timeline: list[str]) -> list[str]:
    """Call the ASGI app directly so every body chunk is timestamped into the
    SAME timeline the fake store writes to, at the moment it is sent. (The
    TestClient buffers the whole response, so it cannot show ordering.)"""
    chunks: list[str] = []
    raw = json.dumps(body).encode()

    async def run() -> None:
        sent_request = False
        never = asyncio.Event()

        async def receive():
            nonlocal sent_request
            if not sent_request:
                sent_request = True
                return {"type": "http.request", "body": raw, "more_body": False}
            await never.wait()  # the client never disconnects mid-stream

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                text = message["body"].decode()
                chunks.append(text)
                for line in text.splitlines():
                    if line.startswith("data: "):
                        timeline.append("sse:" + json.loads(line[6:])["type"])

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "http", "path": "/api/chat/stream",
            "raw_path": b"/api/chat/stream", "root_path": "", "query_string": b"",
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(raw)).encode())],
            "client": ("127.0.0.1", 5000), "server": ("127.0.0.1", 8000),
        }
        await app(scope, receive, send)

    asyncio.run(run())
    return chunks


def test_the_stream_endpoint_persists_the_turn_before_done(monkeypatch):
    import app as app_module

    timeline: list[str] = []
    store = _RecordingStore(timeline)
    final = _result("Hello!")

    def fake_stream_turn(key, content, voice, active_business, allowed_apps, effort):
        yield {"type": "text", "text": "Hello!"}
        yield {"type": "done", "result": copy.deepcopy(final)}

    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "stream_turn", fake_stream_turn)
    monkeypatch.setattr(app_module, "extract_async",
                        lambda **kw: timeline.append("extract_async"))
    monkeypatch.setattr(app_module, "record_turn_async",
                        lambda **kw: timeline.append("record_turn_async"))

    sid = str(uuid.uuid4())
    _drive_stream(app_module.app, {"message": "hi", "session_id": sid, "entity_id": "gak"},
                  timeline)

    assert timeline == [
        "sse:text",
        "record_turn",          # persisted first (B22) ...
        "sse:done",             # ... so a fast follow-up reads this turn
        "extract_async",        # background writes stay after the stream
        "record_turn_async",
    ]
    assert store.recorded == [
        (f"gak:{sid}", app_module.to_entity_slug("gak"), sid, "hi", final),
    ]


def test_a_failing_history_write_still_delivers_done(monkeypatch):
    import app as app_module

    timeline: list[str] = []
    store = _RecordingStore(timeline)

    def broken_record_turn(*_a, **_k):
        timeline.append("record_turn")
        raise RuntimeError("disk full")

    store.record_turn = broken_record_turn

    def fake_stream_turn(*_a, **_k):
        yield {"type": "done", "result": _result("Hello!")}

    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "stream_turn", fake_stream_turn)
    monkeypatch.setattr(app_module, "extract_async", lambda **kw: None)
    monkeypatch.setattr(app_module, "record_turn_async", lambda **kw: None)

    _drive_stream(app_module.app, {"message": "hi", "session_id": str(uuid.uuid4())}, timeline)

    assert timeline == ["record_turn", "sse:done"]


def test_a_stream_without_done_is_still_recorded_once(monkeypatch):
    """Today's contract for a generator that ends without `done`: the turn is
    recorded (with an empty result) after the stream, exactly once."""
    import app as app_module

    timeline: list[str] = []
    store = _RecordingStore(timeline)

    def fake_stream_turn(*_a, **_k):
        yield {"type": "text", "text": "partial"}

    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "stream_turn", fake_stream_turn)
    monkeypatch.setattr(app_module, "extract_async", lambda **kw: None)
    monkeypatch.setattr(app_module, "record_turn_async", lambda **kw: None)

    _drive_stream(app_module.app, {"message": "hi", "session_id": str(uuid.uuid4())}, timeline)

    assert timeline == ["sse:text", "record_turn"]
    assert store.recorded[0][4] == {}


def test_the_json_endpoint_already_persists_before_it_responds(monkeypatch):
    """/api/chat returns only after record_turn — the order 0.6 gives the
    stream endpoint. Guard it so it stays that way."""
    import app as app_module
    from fastapi.testclient import TestClient

    timeline: list[str] = []
    store = _RecordingStore(timeline)

    def fake_run_turn(*_a, **_k):
        timeline.append("run_turn")
        return _result("Hello!")

    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "run_turn", fake_run_turn)
    monkeypatch.setattr(app_module, "extract_async", lambda **kw: timeline.append("extract_async"))
    monkeypatch.setattr(app_module, "record_turn_async",
                        lambda **kw: timeline.append("record_turn_async"))

    r = TestClient(app_module.app).post(
        "/api/chat", json={"message": "hi", "session_id": str(uuid.uuid4())},
    )
    assert r.status_code == 200, r.text
    assert timeline == ["run_turn", "record_turn", "extract_async", "record_turn_async"]


# ---- held approvals round-trip through BOTH stores (B6) --------------------
HELD = [{
    "tool": "send_email",
    "input": {"to": "client@example.com", "subject": "Invoice", "body": "Please pay."},
    "approval_token": "3.0123456789abcdef",
    "summary": 'Send an email to client@example.com — subject "Invoice"',
    "carried": 0,
}]


def _held_turns(store, key, sid):
    store.record_turn(key, None, sid, "Email the client.", {
        "reply": "Shall I send it?", "tool_calls": [GUARDRAIL_CALL], "pending": HELD,
    })


def test_postgres_persists_and_reads_back_what_was_held(db):
    """The production path B6 exists for: metadata.pending written by
    record_turn and projected back by get_with_state's one SELECT."""
    store = pg.PostgresConversationStore()
    sid = str(uuid.uuid4())
    key = f"gak:{sid}"
    _held_turns(store, key, sid)
    assert db.rows[-1]["metadata"]["pending"] == HELD
    _, state = store.get_with_state(key)
    assert list(state.last_pending) == HELD
    # And it never reaches the sidebar's transcript.
    assert all("pending" not in t for t in store.transcript(key))


def test_in_memory_and_postgres_agree_on_what_was_held(db):
    pg_store, mem_store = pg.PostgresConversationStore(), ConversationStore()
    sid = str(uuid.uuid4())
    key = f"gak:{sid}"
    _held_turns(pg_store, key, sid)
    _held_turns(mem_store, key, sid)
    assert pg_store.get_with_state(key)[1].last_pending == mem_store.get_with_state(key)[1].last_pending


def test_a_later_reply_with_nothing_held_clears_it(db):
    store = pg.PostgresConversationStore()
    sid = str(uuid.uuid4())
    key = f"gak:{sid}"
    _held_turns(store, key, sid)
    store.record_turn(key, None, sid, "no", {"reply": "Okay, I won't.", "tool_calls": []})
    assert store.get_with_state(key)[1].last_pending == ()
