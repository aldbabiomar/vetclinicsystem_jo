"""
The upgrade path: does a database built by an old release end up matching
a fresh install after updating?

This is the riskiest thing the in-app updater does. `_run_schema_sync()`
runs the release's own schema-apply logic against the live clinic
database, with `check=True` — so anything that raises there
does not degrade, it aborts the update and rolls back. A clinic on an
affected version can never update again, and nothing tells them why.

That is not hypothetical. These tests were written after finding exactly
that: schema_postgres.sql created an index over `sales(idempotency_key)`,
a column that only arrives via an ALTER TABLE in the migration list, which
runs *afterwards*. On a fresh install the CREATE TABLE carries the column
so the index worked; on an upgrade it raised UndefinedColumn, aborting the
schema apply and leaving every table defined below that line uncreated.

Each test builds a throwaway database from a tagged release's schema, runs
the real upgrade, and compares against a fresh install. No mocks — the
same two functions the updater calls.

Needs a throwaway Postgres; skips cleanly without one. See conftest.py.
"""
import os
import re
import subprocess
import sys
import pathlib
import uuid

import pytest

from conftest import needs_db, TEST_DB_URL


pytestmark = needs_db

REPO = pathlib.Path(__file__).parent.parent


def _tags():
    out = subprocess.run(["git", "tag"], cwd=REPO, capture_output=True, text=True).stdout.split()
    def key(t):
        try:
            return [int(x) for x in t.lstrip("v").split(".")]
        except ValueError:
            return [0]
    return sorted([t for t in out if re.fullmatch(r"v\d+\.\d+\.\d+", t)], key=key)


TAGS = _tags()


def _schema_at(tag):
    r = subprocess.run(["git", "show", f"{tag}:schema_postgres.sql"],
                       cwd=REPO, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


@pytest.fixture
def scratch_db():
    """An empty database on the same server, dropped afterwards.

    Created through a separate autocommit connection because CREATE
    DATABASE cannot run inside a transaction.
    """
    import psycopg
    admin_url = re.sub(r"/[^/]+$", "/postgres", TEST_DB_URL)
    name = f"migtest_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(admin_url, autocommit=True) as con:
        con.execute(f'CREATE DATABASE "{name}"')
    yield re.sub(r"/[^/]+$", f"/{name}", TEST_DB_URL), name
    with psycopg.connect(admin_url, autocommit=True) as con:
        con.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _apply_sql(url, sql_text):
    import psycopg
    with psycopg.connect(url, autocommit=True) as con:
        con.execute(sql_text)


def _upgrade(url):
    """Exactly what updater._run_schema_sync() runs, in a subprocess so a
    failure surfaces the same way it would during a real update."""
    # sys.executable, not a bare "python3": the upgrade needs the app's
    # dependencies, and the interpreter running the tests is the one that
    # has them. Inherit the environment and override only the database.
    env = dict(os.environ, DATABASE_URL=url, SECRET_KEY="migration-test")
    # JO's updater runs apply_schema() alone — apply_incremental_migrations()
    # takes a connection here and is called from inside apply_schema(), unlike
    # IQ where the updater invokes both. A real structural divergence, so this
    # mirrors JO's own _run_schema_sync() rather than IQ's.
    return subprocess.run(
        [sys.executable, "-c", "import setup; setup.apply_schema()"],
        cwd=REPO, capture_output=True, text=True, env=env,
    )


def _tables(url):
    import psycopg
    with psycopg.connect(url) as con:
        rows = con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' ORDER BY 1").fetchall()
    return {r[0] for r in rows}


def _columns(url):
    import psycopg
    with psycopg.connect(url) as con:
        rows = con.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema='public'").fetchall()
    return {(r[0], r[1]) for r in rows}


# ---------------------------------------------------------------------------

def test_there_are_tagged_releases_to_upgrade_from():
    assert len(TAGS) >= 5, f"expected a release history to test against, found {TAGS}"


@pytest.mark.parametrize("tag", [TAGS[0], TAGS[len(TAGS)//2]] if len(TAGS) >= 2 else TAGS)
def test_upgrading_from_an_old_release_succeeds_and_converges(tag, scratch_db, db):
    """The whole point. Build the old release's database, run the real
    upgrade, and require both that it did not fail and that the result
    matches a fresh install — table for table, column for column.

    Convergence matters as much as success: a partial upgrade that exits 0
    but leaves tables missing is the shape that hid here before, because
    every table below the failing statement simply never appeared.
    """
    old_schema = _schema_at(tag)
    assert old_schema, f"could not read schema_postgres.sql at {tag}"
    url, _name = scratch_db
    _apply_sql(url, old_schema)

    result = _upgrade(url)
    assert result.returncode == 0, (
        f"upgrading from {tag} FAILED — an in-app update from this version would "
        f"abort and roll back:\n{(result.stderr or result.stdout)[-1500:]}")

    fresh_tables = _tables(TEST_DB_URL)
    upgraded_tables = _tables(url)
    missing = fresh_tables - upgraded_tables
    assert not missing, (
        f"upgrading from {tag} left table(s) that a fresh install has: {sorted(missing)}")

    fresh_cols = {c for c in _columns(TEST_DB_URL) if c[0] in fresh_tables}
    upgraded_cols = {c for c in _columns(url) if c[0] in fresh_tables}
    missing_cols = fresh_cols - upgraded_cols
    assert not missing_cols, (
        f"upgrading from {tag} left column(s) missing: {sorted(missing_cols)[:20]}")


def test_the_migrations_are_idempotent(scratch_db):
    """The updater runs these on every single update, so they meet an
    already-migrated database far more often than a stale one."""
    url, _ = scratch_db
    _apply_sql(url, _schema_at(TAGS[0]))
    first = _upgrade(url)
    assert first.returncode == 0, first.stderr[-800:]
    second = _upgrade(url)
    assert second.returncode == 0, (
        f"running the migrations twice failed the second time:\n{second.stderr[-1000:]}")
    third = _upgrade(url)
    assert third.returncode == 0, "third run failed — the migrations are not idempotent"


def test_no_migration_failure_is_recorded_after_an_upgrade(scratch_db):
    """apply_incremental_migrations() catches a failing statement, records it
    in a settings key and carries on — so an upgrade can 'succeed' with
    silent damage. Nothing should ever be in there."""
    import psycopg
    url, _ = scratch_db
    _apply_sql(url, _schema_at(TAGS[0]))
    assert _upgrade(url).returncode == 0
    with psycopg.connect(url) as con:
        row = con.execute("SELECT value FROM settings WHERE key='migration_failures'").fetchone()
    assert row is None or not (row[0] or "").strip(), f"migrations recorded failures: {row}"


def test_no_index_in_the_schema_file_depends_on_a_migration_added_column():
    """A static guard for the exact bug these tests were written after.

    apply_schema() runs before apply_incremental_migrations(). Anything in
    schema_postgres.sql that references a column only added by the migration
    list works on a fresh install (the CREATE TABLE carries it) and raises on
    every upgrade — aborting the whole schema apply. Such an index belongs in
    the migration list, beside the ALTER TABLE that adds its column.

    This runs without a database, so it fails fast and points straight at the
    cause rather than at a mysterious upgrade failure.
    """
    schema = (REPO / "schema_postgres.sql").read_text(encoding="utf-8")
    setup_py = (REPO / "setup.py").read_text(encoding="utf-8")
    migrated = {(m.group(1), m.group(2)) for m in
                re.finditer(r"ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (\w+)", setup_py)}
    assert migrated, "expected the migration list to add columns"

    offenders = []
    for m in re.finditer(
            r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS (\w+) ON (\w+)\s*\(([^)]*)\)", schema):
        index_name, table, cols = m.group(1), m.group(2), m.group(3)
        for col in (c.strip().split()[0] for c in cols.split(",") if c.strip()):
            if (table, col) in migrated:
                offenders.append(f"{index_name} on {table}({col})")
    assert not offenders, (
        "schema_postgres.sql indexes a column that only a migration adds, which "
        "raises on every upgrade:\n  " + "\n  ".join(offenders))
