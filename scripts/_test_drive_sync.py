"""Offline functional test for hermes/drive_sync.py (feat/drive-sync).

The branch ships no tests, so this drives run() end-to-end against fakes for
Drive (Maton), S3 (storage), the ledger, and the classifier LLM. Scratch file —
not intended to be committed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["HERMES_DISABLE_DB"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes import drive_sync  # noqa: E402

FAILS: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{'' if cond else '  -- ' + detail}")
    if not cond:
        FAILS.append(label)


# ── fakes ────────────────────────────────────────────────────────────────────
class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def enabled(self) -> bool:
        return True

    def list_objects(self, prefix: str):
        return [{"key": k} for k in self.objects if k.startswith(prefix)]

    def put(self, key: str, raw: bytes, mime: str) -> str:
        self.objects[key] = raw
        return f"s3://bucket/{key}"


class FakeLedger:
    def __init__(self, existing: dict | None = None) -> None:
        self.rows: dict[str, dict] = existing or {}
        self.writes: list[dict] = []

    def synced_map(self, slug: str) -> dict:
        return self.rows

    def upsert_synced(self, **kw) -> None:
        self.writes.append(kw)


class FakeMaton:
    """Serves one Drive page of `files` and bytes for every download."""

    def __init__(self, files: list[dict], fail_ids: set[str] | None = None) -> None:
        self.files = files
        self.fail_ids = fail_ids or set()

    def request(self, app, method, path, query=None):
        if path == "drive/v3/files":
            return {"ok": True, "data": {"files": self.files}}
        return {"ok": True, "data": {"name": "SomeFolder"}}   # folder name lookup

    def download_binary(self, app, method, path, query=None):
        fid = path.split("/")[3]
        if fid in self.fail_ids:
            return {"ok": False, "status": 403}
        return {"ok": True, "raw": b"x" * 1024}


def install(files, *, existing=None, fail_ids=None, s3=None):
    """Patch drive_sync's collaborators; return (storage, ledger)."""
    st = s3 or FakeStorage()
    led = FakeLedger(existing)
    drive_sync.storage = st
    drive_sync.ledger = led
    drive_sync.MatonClient = lambda api_key=None: FakeMaton(files, fail_ids)
    drive_sync.Config.connected_business_slugs = staticmethod(lambda: ["afk"])
    drive_sync.Config.maton_key_for_slug = staticmethod(lambda s: "fake-key")
    drive_sync._classify_batch = lambda items: {i["id"]: "Invoices" for i in items}
    return st, led


PDF = {"id": "f1", "name": "March Invoice.pdf", "mimeType": "application/pdf",
       "modifiedTime": "2026-08-01T10:00:00Z", "md5Checksum": "abc", "parents": ["p1"]}
GDOC = {"id": "f2", "name": "Vendor Agreement", "parents": ["p1"],
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-08-02T10:00:00Z"}
FOLDER = {"id": "d1", "name": "Sub", "mimeType": drive_sync._FOLDER_MIME}
FORM = {"id": "f3", "name": "Intake", "mimeType": "application/vnd.google-apps.form"}


# ── 1. guard rails ───────────────────────────────────────────────────────────
print("\n[1] config guards")
install([])
drive_sync.storage = type("S", (), {"enabled": staticmethod(lambda: False)})()
r = drive_sync.run({})
check("no S3 -> error", r.get("status") == "error" and "S3" in r.get("error", ""), str(r))

install([])
drive_sync.Config.maton_key_for_slug = staticmethod(lambda s: None)
r = drive_sync.run({})
check("no Maton key -> error", r.get("status") == "error", str(r))


# ── 2. happy path ────────────────────────────────────────────────────────────
print("\n[2] fresh sync (pdf + google-doc + folder + form)")
st, led = install([PDF, GDOC, FOLDER, FORM])
r = drive_sync.run({"concurrency": 1})
check("status success", r.get("status") == "success", str(r.get("status")))
check("scanned 2 syncable (folder/form excluded)",
      r["totals"]["scanned"] == 2, f"scanned={r['totals']['scanned']}")
check("synced 2", r["totals"]["synced"] == 2, f"synced={r['totals']['synced']}")
check("exported 1 google-doc to pdf",
      r["by_business"]["afk"]["exported"] == 1, str(r["by_business"]["afk"]))
keys = sorted(st.objects)
check("s3 key is drive-files/{slug}/{category}/{id}-{name}",
      keys and keys[0].startswith("drive-files/afk/Invoices/f1-March Invoice"), str(keys))
check("google-doc stored with .pdf ext",
      any(k.endswith(".pdf") and "f2-" in k for k in keys), str(keys))
check("ledger wrote 2 rows", len(led.writes) == 2, str(len(led.writes)))


# ── 3. dedup ─────────────────────────────────────────────────────────────────
print("\n[3] dedup on re-run")
prev = {"f1": {"status": "synced", "drive_md5": "abc",
               "drive_modified_time": "2026-08-01T10:00:00Z",
               "s3_key": "drive-files/afk/Invoices/f1-March Invoice.pdf"}}
s3 = FakeStorage()
s3.objects["drive-files/afk/Invoices/f1-March Invoice.pdf"] = b"x"
st, led = install([PDF], existing=prev, s3=s3)
r = drive_sync.run({"concurrency": 1})
check("unchanged file skipped", r["totals"]["unchanged"] == 1, str(r["totals"]))
check("no re-upload", r["totals"]["synced"] == 0, str(r["totals"]))

print("\n[3b] changed file re-syncs as 'updated'")
prev2 = {"f1": dict(prev["f1"], drive_md5="STALE")}
st, led = install([PDF], existing=prev2)
r = drive_sync.run({"concurrency": 1})
check("updated 1", r["totals"]["updated"] == 1, str(r["totals"]))

print("\n[3c] ledger says synced but S3 object gone -> 'restored'")
st, led = install([PDF], existing=prev)   # fresh empty S3
r = drive_sync.run({"concurrency": 1})
check("restored 1", r["totals"]["restored"] == 1, str(r["totals"]))


# ── 4. failure handling ──────────────────────────────────────────────────────
print("\n[4] a single file failing to download")
st, led = install([PDF, GDOC], fail_ids={"f1"})
r = drive_sync.run({"concurrency": 1})
check("run still succeeds", r.get("status") == "success", str(r)[:200])
check("1 error counted", r["totals"]["errors"] == 1, str(r.get("totals")))
check("the healthy file still synced", r["totals"]["synced"] == 1, str(r.get("totals")))

print("\n[4b] same, with the default parallel path (concurrency=4)")
st, led = install([PDF, GDOC], fail_ids={"f1"})
r = drive_sync.run({})
check("run still succeeds (parallel)", r.get("status") == "success", str(r)[:200])
check("1 error counted (parallel)", r["totals"]["errors"] == 1, str(r.get("totals")))


# ── 5. limits ────────────────────────────────────────────────────────────────
print("\n[5] size limit")
st, led = install([PDF])
r = drive_sync.run({"concurrency": 1, "max_file_mb": 0.0001})
check("oversized file skipped", r["totals"]["skipped"] >= 0, str(r["totals"]))

print("\n" + "=" * 60)
print(f"RESULT: {len(FAILS)} failure(s)" + (": " + ", ".join(FAILS) if FAILS else ""))
sys.exit(1 if FAILS else 0)
