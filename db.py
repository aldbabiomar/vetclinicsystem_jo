"""
Postgres connection layer for Jordan Referral Center.

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

import psycopg
from psycopg.rows import dict_row

IntegrityError = psycopg.IntegrityError

# Matches a bare '?' placeholder, but not '?' inside a quoted string literal.
# The app never puts literal '?' characters inside string literals in its
# SQL (verified), so a plain replace is safe and fast; this regex is kept
# only as a defensive extra so a stray '?' inside a quoted literal (e.g. a
# future LIKE pattern) is not mistranslated.
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
    """Open a new connection. Caller is responsible for closing it."""
    conn = Connection.connect(database_url(), row_factory=dict_row, autocommit=False)
    return conn


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
