"""LIVE end-to-end test: run REAL agent turns (same path as the chat UI).

Exercises the full stack — system prompt assembled with context packs + memory +
federated RAG, then a real model call. Makes real (paid) Anthropic calls.
Read-only intent. Run:  PYTHONUTF8=1 python scripts/_test_agent_turns.py
"""
from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes.agent import run_turn  # noqa: E402

CASES = [
    ("afk", "What problems came up in the recent supervisor meetings about reporting?"),
    ("nh", "What US compliance or listing rules apply to the Nour Hammam buildout materials?"),
    ("afk", "What is the tractor's insurance premium?"),  # NEGATIVE CONTROL — no such data
]


def run(biz, q):
    print("\n" + "=" * 74)
    print(f"[{biz}]  Q: {q}")
    print("-" * 74)
    session = str(uuid.uuid4())
    result = run_turn(session, q, active_business=biz)
    print(result.get("reply") or "(no reply)")
    tools = result.get("tool_calls") or []
    if tools:
        print(f"\n  (tools used: {[t.get('tool') for t in tools]})")


for biz, q in CASES:
    try:
        run(biz, q)
    except Exception as e:  # noqa: BLE001
        print(f"  ERROR: {e}")

print("\n\nDONE.")
