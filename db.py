"""
Postgres connection layer for VetClinicSystem JO.

This module exists so the rest of the codebase (app.py, logic.py, auth.py,
attachments.py, ...) can use a consistent, simple data-access style. It provides:

  - a psycopg (v3) connection whose .execute() accepts '?' as a placeholder
    (the style used throughout this codebase), translating it to Postgres's
    '%s' style before running it.
  - rows returned as plain dicts (via psycopg's dict_row): row["field"],
    row.get("field"), "field" in row.keys(), and Jinja's {{ row.field }}
    all work as expected.
  - IntegrityError re-exported for convenient importing at call sites.
"""
import os
import re
import threading

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

IntegrityError = psycopg.IntegrityError
# Re-exported so app.py can catch "the pool is exhausted" specifically
# (dbmod.PoolTimeout) and show a friendly "server is busy" message instead
# of a generic 500.
PoolTimeout = PoolTimeout

# Matches every bare '?' unconditionally — this is NOT quote-aware (a
# previous version of this comment claimed it was; it isn't). Safe today
# only because the app never puts a literal '?' character inside a SQL
# string literal (verified) — if a future LIKE pattern or similar ever
# needs one (e.g. "...LIKE '100%?'"), it would be silently mistranslated
# and desync the bound parameters. Make this quote-aware first if that
# ever comes up, rather than relying on this comment as documentation of
# safety it doesn't actually provide.
_PLACEHOLDER_RE = re.compile(r"\?")


def _translate(sql):
    return _PLACEHOLDER_RE.sub("%s", sql)


class Connection(psycopg.Connection):
    """psycopg Connection that accepts SQLite-style '?' placeholders."""

    def execute(self, query, params=None, **kwargs):
        return super().execute(_translate(query), params, **kwargs)


def database_url():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and fill it "
            "in, or run setup.py first."
        )
    return url


def connect():
    """
    Open a new, standalone connection outside the pool. Caller is
    responsible for closing it.

    Used only by code that doesn't run inside a normal web request and
    therefore has no g.db lifecycle to piggyback on: one-off maintenance
    scripts (setup.py, import_seed.py, generate_test_data.py) and the
    app's background scheduler (nightly backup). Those are low-frequency,
    long-or-uncertain-duration operations that don't belong sharing a
    small pool with request traffic, so they keep opening their own
    short-lived connections exactly as before.
    """
    conn = Connection.connect(database_url(), row_factory=dict_row, autocommit=False)
    return conn


# ---------------------------------------------------------------------------
# Connection pool — used for ordinary web request traffic (app.py's
# get_db()/close_db()). Previously every request opened a brand-new
# PostgreSQL connection with no limit; under a burst of LAN traffic
# (multiple clinic devices, each with Waitress's 8 worker threads) that
# could pile up faster than Postgres's own max_connections, degrading into
# connection-refused errors with no backpressure or bounded wait.
#
# A pool gives us three things a bare per-request connect() didn't:
#   - a hard cap on how many server-side connections this app can ever
#     hold open at once (DB_POOL_MAX_SIZE)
#   - reuse of already-open connections instead of a fresh TCP+auth
#     handshake on every single request
#   - a bounded wait (DB_POOL_TIMEOUT_SECONDS) with a clear error instead
#     of an unbounded hang when the pool is briefly exhausted
# ---------------------------------------------------------------------------
_pool = None
_pool_lock = threading.Lock()


def _pool_settings():
    """Read pool sizing from the environment with conservative defaults.
    A single-clinic LAN deployment rarely needs more than a handful of
    concurrent connections; these defaults comfortably cover Waitress's
    8 worker threads plus a few background-job connections without
    opening the door to unbounded growth."""
    min_size = int(os.environ.get("DB_POOL_MIN_SIZE", "2"))
    max_size = int(os.environ.get("DB_POOL_MAX_SIZE", "15"))
    timeout = float(os.environ.get("DB_POOL_TIMEOUT_SECONDS", "10"))
    max_lifetime = float(os.environ.get("DB_POOL_MAX_LIFETIME_SECONDS", "1800"))
    return min_size, max_size, timeout, max_lifetime


def init_pool():
    """Create the connection pool if it doesn't exist yet. Safe to call
    from multiple threads concurrently (e.g. two of Waitress's worker
    threads both handling the very first requests) — only one pool is
    ever created."""
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        min_size, max_size, timeout, max_lifetime = _pool_settings()
        _pool = ConnectionPool(
            conninfo=database_url(),
            connection_class=Connection,
            kwargs={"row_factory": dict_row, "autocommit": False},
            min_size=min_size,
            max_size=max_size,
            timeout=timeout,
            max_lifetime=max_lifetime,
            open=True,
        )
        return _pool


def get_pool():
    return _pool if _pool is not None else init_pool()


def getconn(timeout=None):
    """Borrow a connection from the pool. Raises db.PoolTimeout if none
    becomes free within the pool's configured timeout (or the timeout
    passed here) — callers should let this propagate to the normal error
    handler rather than hang."""
    return get_pool().getconn(timeout=timeout)


def putconn(conn):
    """Return a connection to the pool. The pool itself rolls back any
    transaction still open on the connection before reusing it, so a
    caller that forgot to commit/rollback can't leak state into the next
    request that borrows this connection — but every call site in this
    app should already have committed or rolled back explicitly before
    reaching this (see app.py's close_db)."""
    get_pool().putconn(conn)


def close_pool():
    """Closes the pool and every connection in it. Called on graceful
    shutdown; safe to call even if the pool was never created."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def next_id(db, prefix, width=3):
    """
    Atomically allocate the next sequential ID for a given prefix
    (e.g. 'P' -> P001, P002, ...; 'V' -> V001, V002, ...).

    This replaces the old MAX(id)+1-in-Python approach, which had a race
    condition: two people creating a record in the same instant could be
    handed the same ID. A single UPDATE...RETURNING is atomic under
    Postgres's row-level locking, so concurrent callers are serialized
    automatically and never collide.
    """
    row = db.execute(
        """
        INSERT INTO id_counters (prefix, next_val) VALUES (?, 2)
        ON CONFLICT (prefix) DO UPDATE SET next_val = id_counters.next_val + 1
        RETURNING next_val - 1 AS n
        """,
        (prefix,),
    ).fetchone()
    n = row["n"]
    return f"{prefix}{n:0{width}d}"


def seed_counter(db, prefix, current_max):
    """Used by the migration script to prime a counter from existing data."""
    db.execute(
        """
        INSERT INTO id_counters (prefix, next_val) VALUES (?, ?)
        ON CONFLICT (prefix) DO UPDATE SET next_val = GREATEST(id_counters.next_val, ?)
        """,
        (prefix, current_max + 1, current_max + 1),
    )


def run_script(con, sql_text):
    """
    Executes a multi-statement .sql file — psycopg executes one command at a
    time, so this splits on ';' — but only after stripping '--' line
    comments first, since a semicolon inside a comment (e.g. "for a date;
    a session becomes...") would otherwise be mistaken for a statement
    terminator and split a comment in half.
    """
    lines = []
    for line in sql_text.splitlines():
        idx = line.find("--")
        lines.append(line[:idx] if idx != -1 else line)
    cleaned = "\n".join(lines)
    for stmt in cleaned.split(";"):
        stmt = stmt.strip()
        if stmt:
            con.execute(stmt)
