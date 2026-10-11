"""Create / update the Hermes Postgres schema on AWS RDS.

Runs every ``*.sql`` file in ``hermes/db/migrations`` in filename order, exactly
once each, tracked in a ``schema_migrations`` table. Safe to re-run: already
applied files are skipped.

Usage:
    python scripts/init_db.py            # apply pending migrations
    python scripts/init_db.py --status   # show applied vs pending, apply nothing
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the project root importable when run as `python scripts/init_db.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes.config import Config  # noqa: E402  (loads .env)
from hermes.db import connect  # noqa: E402

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "hermes" / "db" / "migrations"

_TRACK_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
"""


def _migration_files() -> list[Path]:
    return sorted(_MIGRATIONS_DIR.glob("*.sql"))


def _applied(cur) -> set[str]:
    cur.execute("SELECT filename FROM schema_migrations;")
    return {row[0] for row in cur.fetchall()}


def main() -> int:
    status_only = "--status" in sys.argv
    files = _migration_files()
    if not files:
        print(f"No migrations found in {_MIGRATIONS_DIR}")
        return 1

    _ = Config  # ensure .env is loaded before we connect
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(_TRACK_TABLE)
            conn.commit()
            done = _applied(cur)

            pending = [f for f in files if f.name not in done]
            print(f"Migrations: {len(files)} total, {len(done)} applied, "
                  f"{len(pending)} pending.")

            if status_only:
                for f in files:
                    mark = "[x]" if f.name in done else "[ ]"
                    print(f"  {mark} {f.name}")
                return 0

            for f in pending:
                print(f"  -> applying {f.name} ...", end="", flush=True)
                sql = f.read_text(encoding="utf-8")
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s);",
                    (f.name,),
                )
                conn.commit()
                print(" done.")

            print("All migrations applied." if pending else "Nothing to do — up to date.")
            cur.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public';"
            )
            print(f"Public tables now present: {cur.fetchone()[0]}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
