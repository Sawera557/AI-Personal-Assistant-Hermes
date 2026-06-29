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

import os
import time
from contextlib import contextmanager

import psycopg

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

    Retries transient failures (DNS hiccups, connection resets) so a cold start
    doesn't surface as an empty result. Tune with DB_CONNECT_RETRIES.
    """
    cfg = _cfg()
    if not cfg["host"]:
        raise RuntimeError("DBRDS_HOST is not set — cannot connect to Postgres.")

    attempts = max(1, int(os.getenv("DB_CONNECT_RETRIES", "3")))
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
            return psycopg.connect(**kwargs)
        except (psycopg.OperationalError, OSError) as exc:
            # OSError covers socket.gaierror (DNS "getaddrinfo failed").
            last_exc = exc
            if i < attempts - 1:
                time.sleep(0.4 * (i + 1))
    raise last_exc  # type: ignore[misc]


@contextmanager
def cursor(commit: bool = True):
    """Context manager yielding a cursor over a fresh connection.

    Commits on clean exit (unless commit=False), rolls back on error, always
    closes. Fine for scripts and request-scoped work; swap for a pool under load.
    """
    conn = connect()
    try:
        with conn.cursor() as cur:
            yield cur
        if commit:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ping() -> str:
    """Return the server version string — a quick connectivity check."""
    with cursor(commit=False) as cur:
        cur.execute("SELECT version();")
        return cur.fetchone()[0]
