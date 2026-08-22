"""
Jordan Referral Center — one-command setup.
Works the same way on macOS and Windows.

    python3 setup.py

What it does, in order:
  1. Checks Docker is installed and running (prints install instructions if not).
  2. Creates .env from .env.example if you don't have one yet (with a fresh
     random SECRET_KEY).
  3. Starts the PostgreSQL container (docker compose up -d) and waits for it
     to be ready.
  4. Creates the database schema if it isn't there yet, AND applies any
     columns/tables added since your database was first set up — this runs
     every time, so a schema update never requires remembering to run a
     separate migration script by hand.
  5. If the database is empty, seeds it from seed_data.json.
  6. Prints next steps.

Safe to re-run any time — every step skips itself if already done.
"""
import os
import secrets
import shutil
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def step(msg):
    print(f"\n== {msg}")


def run(cmd, **kwargs):
    print("  $ " + " ".join(cmd))
    return subprocess.run(cmd, cwd=BASE_DIR, **kwargs)


def check_docker():
    step("Checking Docker")
    if not shutil.which("docker"):
        print(
            "Docker was not found on this computer.\n\n"
            "Install Docker Desktop (free) from:\n"
            "  https://www.docker.com/products/docker-desktop/\n"
            "then run this script again. Docker Desktop works the same way "
            "on macOS and Windows."
        )
        sys.exit(1)
    result = run(["docker", "info"], capture_output=True, text=True)
    if result.returncode != 0:
        print(
            "Docker is installed but doesn't seem to be running.\n"
            "Start Docker Desktop, wait for it to finish launching, then "
            "run this script again."
        )
        sys.exit(1)
    print("  Docker is installed and running.")


def ensure_env_file():
    step("Checking configuration (.env)")
    env_path = os.path.join(BASE_DIR, ".env")
    example_path = os.path.join(BASE_DIR, ".env.example")
    if os.path.exists(env_path):
        print("  .env already exists — leaving it as-is.")
        return
    with open(example_path) as f:
        content = f.read()
    content = content.replace("change-me", secrets.token_hex(32))
    with open(env_path, "w") as f:
        f.write(content)
    print("  Created .env with a fresh secret key.")


def start_postgres():
    step("Starting PostgreSQL (Docker)")
    compose = ["docker", "compose"]
    result = run(compose + ["version"], capture_output=True, text=True)
    if result.returncode != 0:
        compose = ["docker-compose"]  # older standalone binary
    run(compose + ["up", "-d"], check=True)

    print("  Waiting for the database to be ready...")
    for _ in range(60):
        r = run(
            compose + ["exec", "-T", "db", "pg_isready", "-U", "jrc", "-d", "jrc"],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            print("  PostgreSQL is ready.")
            return
        time.sleep(2)
    print("  PostgreSQL didn't become ready in time — check `docker compose logs db`.")
    sys.exit(1)


def load_dotenv_now():
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"))


def apply_schema():
    step("Setting up the database schema")
    import db as dbmod
    con = dbmod.connect()
    schema_path = os.path.join(BASE_DIR, "schema_postgres.sql")
    with open(schema_path) as f:
        sql_text = f.read()
    dbmod.run_script(con, sql_text)
    con.commit()

    # Must run before apply_incremental_migrations(): the users.role ->
    # users.role_id backfill below matches by role NAME, so Admin/Vet/
    # Reception need to already exist as real `roles` rows first.
    import auth
    auth.seed_default_roles_and_permissions(con)

    apply_incremental_migrations(con)
    con.close()
    print("  Schema is up to date.")


# Every column/index added to a table that already existed in an earlier
# version of the schema (as opposed to a brand-new table, which
# schema_postgres.sql's CREATE TABLE IF NOT EXISTS already handles) needs an
# explicit ALTER statement here. CREATE TABLE IF NOT EXISTS only skips
# tables that already exist — it does NOT retroactively add a new column to
# one. Without this step, a schema change only reaches an existing database
# if someone remembers to run that specific migration script by hand; this
# runs automatically every time setup.py does (i.e. every app launch), so
# that can't happen. Every statement here must stay purely additive and
# safe to run unlimited times — never a data reset or a one-time transform
# (those stay as their own separate, manually-run scripts).
INCREMENTAL_SCHEMA_STATEMENTS = [
    "ALTER TABLE visits ADD COLUMN IF NOT EXISTS weight_kg DOUBLE PRECISION",
    "ALTER TABLE visits ADD COLUMN IF NOT EXISTS bcs INTEGER CHECK (bcs BETWEEN 1 AND 9)",
    "ALTER TABLE inpatient_cases ADD COLUMN IF NOT EXISTS weight_kg DOUBLE PRECISION",
    "ALTER TABLE inpatient_cases ADD COLUMN IF NOT EXISTS bcs INTEGER CHECK (bcs BETWEEN 1 AND 9)",
    "ALTER TABLE price_list ADD COLUMN IF NOT EXISTS can_discount INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS boarding_id INTEGER",
    "CREATE INDEX IF NOT EXISTS idx_payments_boarding ON payments(boarding_id)",

    # --- RBAC migration: users.role (TEXT, hardcoded Admin/Vet/Reception)
    # -> users.role_id (FK into the new roles table). Safe to run on every
    # launch: once `role` is dropped below, the whole DO block becomes a
    # no-op (the IF EXISTS check never touches the dropped column).
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS role_id TEXT REFERENCES roles(id)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS custom_discount_cap INTEGER CHECK (custom_discount_cap BETWEEN 0 AND 100)",
    """
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='role') THEN
            UPDATE users u SET role_id = r.id FROM roles r WHERE r.name = u.role AND u.role_id IS NULL;
            ALTER TABLE users ALTER COLUMN role_id SET NOT NULL;
            ALTER TABLE users DROP COLUMN role;
        END IF;
    END $$;
    """,
    "CREATE INDEX IF NOT EXISTS idx_users_role ON users(role_id)",
]


def apply_incremental_migrations(con):
    for stmt in INCREMENTAL_SCHEMA_STATEMENTS:
        con.execute(stmt)
    con.commit()


def migrate_or_seed():
    step("Loading data")
    import db as dbmod
    con = dbmod.connect()
    existing = con.execute("SELECT COUNT(*) AS n FROM owners").fetchone()["n"]
    con.close()

    if existing:
        print("  Database already has data in it — skipping seed.")
        return

    print("  No existing data found — building a fresh database from seed_data.json...")
    run([sys.executable, "import_seed.py"], check=True)


def ensure_dependencies():
    step("Checking Python dependencies")
    req = os.path.join(BASE_DIR, "requirements.txt")
    # Plain install first; if the system Python refuses (macOS's
    # "externally-managed-environment" restriction on Homebrew/python.org
    # installs is the common case), retry with --user before giving up.
    attempts = [
        [sys.executable, "-m", "pip", "install", "-q", "-r", req],
        [sys.executable, "-m", "pip", "install", "-q", "--user", "-r", req],
    ]
    for cmd in attempts:
        if subprocess.run(cmd).returncode == 0:
            print("  Dependencies are installed.")
            return
    print(
        "\nCouldn't install the required Python packages automatically.\n"
        "Try running this by hand and then re-run setup.py:\n\n"
        f"  {sys.executable} -m pip install -r requirements.txt\n\n"
        "If that reports an 'externally-managed-environment' error, add\n"
        "--break-system-packages to the command above, or use a virtual\n"
        "environment (python3 -m venv .venv && source .venv/bin/activate).\n"
    )
    sys.exit(1)


def main():
    ensure_dependencies()
    check_docker()
    ensure_env_file()
    start_postgres()
    load_dotenv_now()
    apply_schema()
    migrate_or_seed()

    print(
        "\nAll set. Start the app with:\n"
        "  python3 app.py\n"
        "\n(macOS: double-click 'Start Jordan Referral Center.command'."
        "  Windows: double-click 'Start Jordan Referral Center.bat'.)\n"
    )


if __name__ == "__main__":
    main()
