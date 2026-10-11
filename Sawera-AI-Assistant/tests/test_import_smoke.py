"""Import smoke test — the guard against 'works locally, ImportError on deploy'.

The failure this prevents: code committed that imports a name/module which the
COMMITTED tree does not provide. It passes locally because the developer's dirty
working tree still has the defining change uncommitted, then crashes on Vercel
(which only ships committed files) with e.g.:

    ImportError: cannot import name 'ATTACHMENT_TEXT_PREFIX' from 'hermes.attachments'

This test imports exactly what the Vercel entrypoint imports (`app.py` and its
transitive graph), so a missing symbol fails HERE — in CI, in the pre-push hook,
and in the Vercel build guard — long before it can reach production.

Deliberately env-free: it sets HERMES_DISABLE_DB=1 and relies only on lazy
clients (no network, no DB, no secrets), so it behaves the same on a clean
checkout with no .env (i.e. the Vercel build) as it does locally.

Run directly:

    python tests/test_import_smoke.py
"""
from __future__ import annotations

import importlib
import os
import sys

os.environ["HERMES_DISABLE_DB"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_passed = 0


def check(label: str, cond: bool) -> None:
    global _passed
    assert cond, f"FAILED: {label}"
    _passed += 1
    print(f"  ok  {label}")


# The Vercel entrypoint and its top-level dependency. Importing `app` pulls in
# the whole serverless import graph (hermes.agent -> tools -> ...), so this is
# the same surface that crashed production.
_ENTRYPOINTS = ["app", "hermes.agent"]


def test_entrypoints_import():
    for mod in _ENTRYPOINTS:
        try:
            importlib.import_module(mod)
            ok = True
        except Exception as exc:  # noqa: BLE001 - report the real cause
            ok = False
            print(f"  !! import {mod} FAILED: {type(exc).__name__}: {exc}")
        check(f"import {mod}", ok)


def test_every_hermes_module_imports():
    """Also import every hermes/*.py so a broken symbol in a module NOT on the
    app entrypoint path still gets caught."""
    here = os.path.dirname(os.path.abspath(__file__))
    hermes_dir = os.path.join(os.path.dirname(here), "hermes")
    names = sorted(
        f[:-3] for f in os.listdir(hermes_dir)
        if f.endswith(".py") and not f.startswith("_")
    )
    for name in names:
        mod = f"hermes.{name}"
        try:
            importlib.import_module(mod)
            ok = True
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"  !! import {mod} FAILED: {type(exc).__name__}: {exc}")
        check(f"import {mod}", ok)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        print(f"\n{t.__name__}:")
        t()
    print(f"\nPASS: {_passed} imports verified.")
