"""Numeric env vars must never crash the app at import.

Production outage 2026-08-14: a freshly-imported Vercel project had
HERMES_MAX_TOKENS set to an EMPTY string. os.getenv returns "" rather than
None for that, so `int(os.getenv("HERMES_MAX_TOKENS", "8000"))` raised
ValueError while the Config class body was being evaluated — app.py failed to
import and every single route returned 500 FUNCTION_INVOCATION_FAILED.

A blank or malformed value in a hosting dashboard is a routine mistake. It must
degrade to the documented default, not take the process down.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes.config import _float_env, _int_env  # noqa: E402


# ---- the exact production failure ----

def test_empty_string_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("HERMES_MAX_TOKENS", "")
    assert _int_env("HERMES_MAX_TOKENS", 8000) == 8000


def test_whitespace_only_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("HERMES_MAX_TOKENS", "   ")
    assert _int_env("HERMES_MAX_TOKENS", 8000) == 8000


def test_unset_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("HERMES_MAX_TOKENS", raising=False)
    assert _int_env("HERMES_MAX_TOKENS", 8000) == 8000


# ---- real values still win ----

@pytest.mark.parametrize("raw,expected", [("1234", 1234), (" 1234 ", 1234), ("0", 0), ("-5", -5)])
def test_valid_ints_are_honoured(monkeypatch, raw, expected):
    monkeypatch.setenv("SOME_INT", raw)
    assert _int_env("SOME_INT", 99) == expected


@pytest.mark.parametrize("raw,expected", [("0.35", 0.35), (" 0.35 ", 0.35), ("1", 1.0), ("0", 0.0)])
def test_valid_floats_are_honoured(monkeypatch, raw, expected):
    monkeypatch.setenv("SOME_FLOAT", raw)
    assert _float_env("SOME_FLOAT", 0.92) == expected


# ---- garbage degrades instead of raising ----

@pytest.mark.parametrize("raw", ["not-a-number", "8000abc", "8,000", "8.5", "None", "null"])
def test_garbage_ints_fall_back_without_raising(monkeypatch, raw):
    monkeypatch.setenv("SOME_INT", raw)
    assert _int_env("SOME_INT", 42) == 42


@pytest.mark.parametrize("raw", ["not-a-number", "0.3.5", "", "  "])
def test_garbage_floats_fall_back_without_raising(monkeypatch, raw):
    monkeypatch.setenv("SOME_FLOAT", raw)
    assert _float_env("SOME_FLOAT", 0.5) == 0.5


def test_garbage_is_logged_so_it_is_not_silent(monkeypatch, caplog):
    """Falling back must be visible — a silently ignored typo is its own bug."""
    monkeypatch.setenv("SOME_INT", "eight thousand")
    with caplog.at_level("WARNING", logger="hermes.config"):
        assert _int_env("SOME_INT", 8000) == 8000
    assert any("eight thousand" in r.getMessage() for r in caplog.records)


# ---- the whole class body must survive a hostile environment ----

BLANKABLE_NUMERIC_VARS = [
    "HERMES_MAX_TOKENS", "HERMES_MAX_TOKENS_CEILING", "HERMES_MAX_TOOL_ITERATIONS",
    "PORT", "SCHEDULER_POLL_SECONDS", "MEMORY_DUP_THRESHOLD", "MEMORY_UPDATE_THRESHOLD",
    "MEMORY_ALWAYS_INJECT_MAX", "MEMORY_SEMANTIC_TOP_K", "HERMES_RAG_TOP_K",
    "HERMES_RAG_SCORE_THRESHOLD", "HERMES_RAG_CONTEXT_MAX_CHARS",
    "HERMES_CONVO_MEMORY_TOP_K", "HERMES_CONVO_MEMORY_SCORE_THRESHOLD",
    "HERMES_CONVO_MEMORY_MAX_CHARS", "HERMES_CONVO_MEMORY_TTL_DAYS", "NUEXTRACT_TIMEOUT",
]


def test_config_imports_with_every_numeric_var_blank():
    """Simulates pasting .env.example (all values blank) into a hosting dashboard.

    Runs in a subprocess: a cold interpreter is exactly how Vercel imports the
    app, and reloading hermes.config in-process would hand other already-imported
    modules a stale Config class and break unrelated tests.
    """
    import json
    import subprocess

    child = f"""
import json, os, sys, types
# Stub dotenv BEFORE hermes.config imports it, so the developer's own .env
# cannot refill the values we are deliberately blanking.
_stub = types.ModuleType("dotenv")
_stub.load_dotenv = lambda *a, **kw: False
sys.modules["dotenv"] = _stub
for name in {BLANKABLE_NUMERIC_VARS!r}:
    os.environ[name] = ""
sys.path.insert(0, {str(ROOT)!r})
from hermes.config import Config
print(json.dumps({{
    "max_tokens": Config.MAX_TOKENS,
    "ceiling": Config.MAX_TOKENS_CEILING,
    "iterations": Config.MAX_TOOL_ITERATIONS,
    "rag_threshold": Config.RAG_SCORE_THRESHOLD,
    "port": Config.PORT,
}}))
"""
    proc = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"import crashed with blank vars:\n{proc.stderr[-1500:]}"

    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert got["max_tokens"] == 8000
    assert got["ceiling"] == 32000          # derived default: 4x the base budget
    assert got["iterations"] == 12
    assert got["rag_threshold"] == 0.30
    assert got["port"] == 5000
