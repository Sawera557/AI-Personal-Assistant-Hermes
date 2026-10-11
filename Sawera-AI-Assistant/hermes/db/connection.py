"""Postgres connection helper for Hermes (AWS RDS).

Two auth modes, auto-selected:

* **Password** — if ``DB_PASSWORD`` (or ``PGPASSWORD``) is set, connect with it.
* **IAM auth** — otherwise generate a short-lived RDS auth token with boto3 and
  connect with that. No AWS CLI needed; uses the AWS credentials already in the
  environment. Tokens expire in ~15 min, so we generate a fresh one per connect.

SSL is always required (RDS IAM auth mandates it). Set ``PGSSLMODE=verify-full``
plus ``PGSSLROOTCERT`` to the RDS global bundle for full verification in prod.

The credential is never stored — only used to open the socket. Matches the spec:
"Passwords and keys live in a secrets vault — never in the app, code, or logs."
"""
from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager

import psycopg

from ..config import float_env, int_env

log = logging.getLogger("hermes.db.connection")

# Cache the generated IAM token (valid ~15 min) so we don't call boto3 on every
# connection. Refreshed comfortably before expiry.
_token_cache: dict[str, float | str | None] = {"token": None, "exp": 0.0}
_TOKEN_TTL = 600.0  # seconds (< RDS's 900s validity)


def _cfg() -> dict[str, str]:
    host = os.getenv("DBRDS_HOST", "").strip()
    return {
        "host": host,
        "port": os.getenv("DBRDS_PORT", "5432").strip() or "5432",
        "dbname": os.getenv("DB_NAME", "postgres").strip() or "postgres",
        "user": os.getenv("DBRDS_USER", "").strip() or "postgres",
        "region": os.getenv("AWS_REGION", "us-east-1").strip() or "us-east-1",
        "sslmode": os.getenv("PGSSLMODE", "require").strip() or "require",
        "sslrootcert": os.getenv("PGSSLROOTCERT", "").strip(),
    }


def _password(cfg: dict[str, str]) -> str:
    """Static password if provided, else a fresh RDS IAM auth token."""
    pw = os.getenv("DB_PASSWORD") or os.getenv("PGPASSWORD")
    if pw:
        return pw

    now = time.monotonic()
    if _token_cache["token"] and now < float(_token_cache["exp"]):
        return str(_token_cache["token"])

    import boto3  # lazy: only needed for IAM mode

    session = boto3.session.Session(region_name=cfg["region"])
    client = session.client("rds")
    token = client.generate_db_auth_token(
        DBHostname=cfg["host"],
        Port=int(cfg["port"]),
        DBUsername=cfg["user"],
        Region=cfg["region"],
    )
    _token_cache["token"] = token
    _token_cache["exp"] = now + _TOKEN_TTL
    return token


def connect() -> psycopg.Connection:
    """Open a new Postgres connection (caller owns its lifecycle).

    Retries transient failures (DNS hiccups, connection resets) with
    exponential backoff so a brief network blip doesn't surface as a dropped
    write or an empty result. Tune with DB_CONNECT_RETRIES / DB_CONNECT_BACKOFF
    (base seconds) / DB_CONNECT_BACKOFF_MAX (per-attempt cap).

    Deliberately does NOT retry anything past the connection itself — retrying
    a whole read-modify-write operation (e.g. re-running an INSERT after a
    mid-transaction drop) risks duplicate writes. `cursor()` closes the
    connection and rolls back on any error instead; the caller decides whether
    to retry the operation from scratch.
    """
    cfg = _cfg()
    if not cfg["host"]:
        raise RuntimeError("DBRDS_HOST is not set — cannot connect to Postgres.")

    attempts = max(1, int_env("DB_CONNECT_RETRIES", 5))
    backoff_base = float_env("DB_CONNECT_BACKOFF", 0.5)
    backoff_max = float_env("DB_CONNECT_BACKOFF_MAX", 3.0)
    last_exc: Exception | None = None
    for i in range(attempts):
        kwargs: dict[str, object] = {
            "host": cfg["host"],
            "port": cfg["port"],
            "dbname": cfg["dbname"],
            "user": cfg["user"],
            "password": _password(cfg),
            "sslmode": cfg["sslmode"],
            "connect_timeout": 10,
        }
        if cfg["sslrootcert"]:
            kwargs["sslrootcert"] = cfg["sslrootcert"]
        try:
            conn = psycopg.connect(**kwargs)
            from pgvector.psycopg import register_vector  # lazy: only needed once connected

            try:
                # Adapts Python list[float] <-> Postgres `vector` on this connection.
                # Fails on a database where `CREATE EXTENSION vector` hasn't run yet
                # (e.g. the very first connect, used to apply that migration) —
                # degrade rather than block connecting, since scripts/init_db.py
                # itself needs a working connection to create the extension.
                register_vector(conn)
            except psycopg.ProgrammingError:
                conn.rollback()
                log.warning(
                    "pgvector extension not found yet — vector columns won't adapt "
                    "on this connection until migrations are applied."
                )
            if i > 0:
                log.info("DB connect succeeded on attempt %d/%d", i + 1, attempts)
            return conn
        except (psycopg.OperationalError, OSError) as exc:
            # OSError covers socket.gaierror (DNS "getaddrinfo failed").
            last_exc = exc
            if i < attempts - 1:
                delay = min(backoff_base * (2 ** i), backoff_max)
                log.warning(
                    "DB connect attempt %d/%d failed (%s); retrying in %.1fs",
                    i + 1, attempts, exc, delay,
                )
                time.sleep(delay)
            else:
                log.error("DB connect failed after %d attempts: %s", attempts, exc)
    raise last_exc  # type: ignore[misc]


# ── Connection pool ──────────────────────────────────────────────────────────
# Opening a fresh RDS connection is EXPENSIVE (~2s: TLS + IAM auth + pgvector
# type registration + first-statement warmup), and the app opened one per query.
# This is a small thread-safe pool of reusable connections so that cost is paid
# once and amortised. Idle connections older than DB_POOL_IDLE are recycled
# (guards against the server dropping a long-idle socket). Disable with DB_POOL=0.
_pool: list[tuple[psycopg.Connection, float]] = []   # (conn, released_at monotonic)
_pool_lock = threading.Lock()
_POOL_MAX = int_env("DB_POOL_MAX", 8)
_POOL_IDLE = float_env("DB_POOL_IDLE", 280.0)  # seconds; under typical server idle timeouts
_POOL_ENABLED = os.getenv("DB_POOL", "1").strip().lower() not in {"0", "false", "no", "off"}


def _pool_acquire() -> psycopg.Connection:
    """A live connection — reused from the pool if one is available and fresh,
    otherwise newly opened. Recycles closed / long-idle connections."""
    if _POOL_ENABLED:
        now = time.monotonic()
        with _pool_lock:
            while _pool:
                conn, released_at = _pool.pop()
                if conn.closed or (now - released_at) > _POOL_IDLE:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                return conn
    return connect()


def _pool_release(conn: psycopg.Connection, reusable: bool) -> None:
    """Return a clean connection to the pool, or close it (on error / full pool /
    pooling disabled). A connection that errored is never reused — its transaction
    state is unknown."""
    if not _POOL_ENABLED or not reusable or conn.closed:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        return
    with _pool_lock:
        if len(_pool) < _POOL_MAX:
            _pool.append((conn, time.monotonic()))
            return
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass


@contextmanager
def cursor(commit: bool = True):
    """Context manager yielding a cursor over a POOLED connection.

    Commits on clean exit (unless commit=False), rolls back on error. On success
    the connection is returned to the pool for reuse; on error it's rolled back
    and discarded (never reused with an unknown transaction state). Set DB_POOL=0
    to fall back to a fresh connection per call.
    """
    conn = _pool_acquire()
    reusable = True
    try:
        with conn.cursor() as cur:
            yield cur
        if commit:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        reusable = False
        try:
            if not conn.closed:
                conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        _pool_release(conn, reusable)


def ping() -> str:
    """Return the server version string — a quick connectivity check."""
    with cursor(commit=False) as cur:
        cur.execute("SELECT version();")
        return cur.fetchone()[0]
