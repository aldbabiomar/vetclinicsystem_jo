"""
VetClinicSystem JO — one-command setup.
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
            compose + ["exec", "-T", "db", "pg_isready", "-U", "vetclinicsystemjo", "-d", "vetclinicsystemjo"],
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
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_changed_at TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE sales ADD COLUMN IF NOT EXISTS idempotency_key TEXT",
    # backup_log has always accepted a triggered_by argument but had nowhere
    # to put it, so every existing row records NULL. Older rows stay NULL.
    "ALTER TABLE backup_log ADD COLUMN IF NOT EXISTS triggered_by TEXT",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_sales_idempotency_key ON sales(idempotency_key) WHERE idempotency_key IS NOT NULL",
    # NOTE: if this database already has two or more owners sharing the
    # same non-null phone number (the exact duplicate-owner bug this
    # index closes), this statement fails outright and the whole
    # incremental-migration run stops here. Find and merge/clear the
    # duplicates first (e.g. `SELECT phone, COUNT(*) FROM owners WHERE
    # phone IS NOT NULL GROUP BY phone HAVING COUNT(*) > 1`), then re-run
    # setup.py.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_owners_phone_unique ON owners(phone) WHERE phone IS NOT NULL",
    "ALTER TABLE inventory_list ADD COLUMN IF NOT EXISTS barcode_source TEXT CHECK (barcode_source IN ('manual','generated'))",
    # Every barcode on an existing install was created exclusively via the
    # old auto-generate-only route (manual entry didn't exist before this),
    # so backfill accordingly — without this, a pre-existing barcode would
    # be invisible to Bulk Barcode Print (now filtered to barcode_source=
    # 'generated') and could be silently overwritten by manual entry
    # without going through Remove Barcode first (whose guard checks
    # barcode_source, not just whether a barcode is set).
    "UPDATE inventory_list SET barcode_source='generated' WHERE barcode IS NOT NULL AND barcode_source IS NULL",

    # --- "Clean Up" feature — see CLEANUP_FEATURE_PLAN.md.
    "ALTER TABLE billing ADD COLUMN IF NOT EXISTS cleanup_amount NUMERIC(12,3) NOT NULL DEFAULT 0",
    "ALTER TABLE billing ADD COLUMN IF NOT EXISTS cleanup_applied_by TEXT",
    "ALTER TABLE inpatient_cases ADD COLUMN IF NOT EXISTS cleanup_amount NUMERIC(12,3) NOT NULL DEFAULT 0",
    "ALTER TABLE inpatient_cases ADD COLUMN IF NOT EXISTS cleanup_applied_by TEXT",
    "ALTER TABLE boarding_sessions ADD COLUMN IF NOT EXISTS cleanup_amount NUMERIC(12,3) NOT NULL DEFAULT 0",
    "ALTER TABLE boarding_sessions ADD COLUMN IF NOT EXISTS cleanup_applied_by TEXT",
    "ALTER TABLE sales ADD COLUMN IF NOT EXISTS cleanup_amount NUMERIC(12,3) NOT NULL DEFAULT 0",
    "ALTER TABLE sales ADD COLUMN IF NOT EXISTS cleanup_applied_by TEXT",
    "ALTER TABLE refunds ADD COLUMN IF NOT EXISTS cleanup_amount_at_refund NUMERIC(12,3) NOT NULL DEFAULT 0",

    # --- ORPHANED_RECORDS_AUDIT.md F-07 — distributor snapshot on sale_items.
    "ALTER TABLE sale_items ADD COLUMN IF NOT EXISTS distributor_id TEXT REFERENCES distributors(id)",

    # --- ORPHANED_RECORDS_AUDIT.md F-05/F-07/F-13/F-14 — CHECK constraints.
    # Postgres has no "ADD CONSTRAINT IF NOT EXISTS" — DROP IF EXISTS then
    # ADD, run every launch, is what makes each pair idempotent.
    "ALTER TABLE refunds DROP CONSTRAINT IF EXISTS refunds_anchor_ck",
    "ALTER TABLE refunds ADD CONSTRAINT refunds_anchor_ck CHECK ("
    "    (refund_type = 'retail'  AND sale_id IS NOT NULL"
    "        AND visit_id IS NULL AND inpatient_case_id IS NULL)"
    " OR (refund_type = 'service' AND sale_id IS NULL"
    "        AND (visit_id IS NOT NULL) <> (inpatient_case_id IS NOT NULL))"
    ")",
    "ALTER TABLE inventory_list DROP CONSTRAINT IF EXISTS inventory_consignment_needs_distributor_ck",
    "ALTER TABLE inventory_list ADD CONSTRAINT inventory_consignment_needs_distributor_ck "
    "CHECK (ownership_type <> 'Consignment' OR distributor_id IS NOT NULL)",
    "ALTER TABLE attachments DROP CONSTRAINT IF EXISTS attachments_one_anchor_ck",
    "ALTER TABLE attachments ADD CONSTRAINT attachments_one_anchor_ck "
    "CHECK ((visit_id IS NOT NULL) <> (inpatient_case_id IS NOT NULL))",
    "ALTER TABLE payments DROP CONSTRAINT IF EXISTS payments_one_anchor_ck",
    "ALTER TABLE payments ADD CONSTRAINT payments_one_anchor_ck CHECK ("
    "    (visit_id IS NOT NULL)::int"
    "  + (inpatient_case_id IS NOT NULL)::int"
    "  + (boarding_id IS NOT NULL)::int = 1"
    ")",

    # --- ORPHANED_RECORDS_AUDIT.md F-19 — RESTRICT FKs on 14 (+4 Clean Up)
    # user-referencing columns that had none. Same idempotent DROP/ADD
    # pattern; names match Postgres's own default unnamed-FK convention
    # (<table>_<column>_fkey), so these agree with a fresh CREATE TABLE.
    "ALTER TABLE inventory_transactions DROP CONSTRAINT IF EXISTS inventory_transactions_user_id_fkey",
    "ALTER TABLE inventory_transactions ADD CONSTRAINT inventory_transactions_user_id_fkey "
    "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT",
    "ALTER TABLE visits DROP CONSTRAINT IF EXISTS visits_created_by_fkey",
    "ALTER TABLE visits ADD CONSTRAINT visits_created_by_fkey "
    "FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE RESTRICT",
    "ALTER TABLE billing DROP CONSTRAINT IF EXISTS billing_discount_applied_by_fkey",
    "ALTER TABLE billing ADD CONSTRAINT billing_discount_applied_by_fkey "
    "FOREIGN KEY (discount_applied_by) REFERENCES users(id) ON DELETE RESTRICT",
    "ALTER TABLE billing DROP CONSTRAINT IF EXISTS billing_cleanup_applied_by_fkey",
    "ALTER TABLE billing ADD CONSTRAINT billing_cleanup_applied_by_fkey "
    "FOREIGN KEY (cleanup_applied_by) REFERENCES users(id) ON DELETE RESTRICT",
    "ALTER TABLE boarding_sessions DROP CONSTRAINT IF EXISTS boarding_sessions_created_by_fkey",
    "ALTER TABLE boarding_sessions ADD CONSTRAINT boarding_sessions_created_by_fkey "
    "FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE RESTRICT",
    "ALTER TABLE boarding_sessions DROP CONSTRAINT IF EXISTS boarding_sessions_cleanup_applied_by_fkey",
    "ALTER TABLE boarding_sessions ADD CONSTRAINT boarding_sessions_cleanup_applied_by_fkey "
    "FOREIGN KEY (cleanup_applied_by) REFERENCES users(id) ON DELETE RESTRICT",
    "ALTER TABLE boarding_incidents DROP CONSTRAINT IF EXISTS boarding_incidents_user_id_fkey",
    "ALTER TABLE boarding_incidents ADD CONSTRAINT boarding_incidents_user_id_fkey "
    "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT",
    "ALTER TABLE inpatient_cases DROP CONSTRAINT IF EXISTS inpatient_cases_discount_applied_by_fkey",
    "ALTER TABLE inpatient_cases ADD CONSTRAINT inpatient_cases_discount_applied_by_fkey "
    "FOREIGN KEY (discount_applied_by) REFERENCES users(id) ON DELETE RESTRICT",
    "ALTER TABLE inpatient_cases DROP CONSTRAINT IF EXISTS inpatient_cases_created_by_fkey",
    "ALTER TABLE inpatient_cases ADD CONSTRAINT inpatient_cases_created_by_fkey "
    "FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE RESTRICT",
    "ALTER TABLE inpatient_cases DROP CONSTRAINT IF EXISTS inpatient_cases_cleanup_applied_by_fkey",
    "ALTER TABLE inpatient_cases ADD CONSTRAINT inpatient_cases_cleanup_applied_by_fkey "
    "FOREIGN KEY (cleanup_applied_by) REFERENCES users(id) ON DELETE RESTRICT",
    "ALTER TABLE inpatient_updates DROP CONSTRAINT IF EXISTS inpatient_updates_user_id_fkey",
    "ALTER TABLE inpatient_updates ADD CONSTRAINT inpatient_updates_user_id_fkey "
    "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT",
    "ALTER TABLE inpatient_contact_log DROP CONSTRAINT IF EXISTS inpatient_contact_log_staff_user_id_fkey",
    "ALTER TABLE inpatient_contact_log ADD CONSTRAINT inpatient_contact_log_staff_user_id_fkey "
    "FOREIGN KEY (staff_user_id) REFERENCES users(id) ON DELETE RESTRICT",
    "ALTER TABLE inpatient_billing DROP CONSTRAINT IF EXISTS inpatient_billing_logged_by_fkey",
    "ALTER TABLE inpatient_billing ADD CONSTRAINT inpatient_billing_logged_by_fkey "
    "FOREIGN KEY (logged_by) REFERENCES users(id) ON DELETE RESTRICT",
    "ALTER TABLE sales DROP CONSTRAINT IF EXISTS sales_discount_applied_by_fkey",
    "ALTER TABLE sales ADD CONSTRAINT sales_discount_applied_by_fkey "
    "FOREIGN KEY (discount_applied_by) REFERENCES users(id) ON DELETE RESTRICT",
    "ALTER TABLE sales DROP CONSTRAINT IF EXISTS sales_cleanup_applied_by_fkey",
    "ALTER TABLE sales ADD CONSTRAINT sales_cleanup_applied_by_fkey "
    "FOREIGN KEY (cleanup_applied_by) REFERENCES users(id) ON DELETE RESTRICT",
    "ALTER TABLE refunds DROP CONSTRAINT IF EXISTS refunds_processed_by_fkey",
    "ALTER TABLE refunds ADD CONSTRAINT refunds_processed_by_fkey "
    "FOREIGN KEY (processed_by) REFERENCES users(id) ON DELETE RESTRICT",
    "ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_resource_id_fkey",
    "ALTER TABLE appointments ADD CONSTRAINT appointments_resource_id_fkey "
    "FOREIGN KEY (resource_id) REFERENCES users(id) ON DELETE RESTRICT",
    "ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_created_by_fkey",
    "ALTER TABLE appointments ADD CONSTRAINT appointments_created_by_fkey "
    "FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE RESTRICT",
]


def apply_incremental_migrations(con):
    """Each statement in its own savepoint — a single failing statement
    (e.g. the duplicate-owner-phone unique index, if a database already
    has a violating pair) used to abort the whole transaction, silently
    skipping every statement after it, on every single launch, forever.
    Now a failure is isolated to that one statement; the rest still apply.
    See ORPHANED_RECORDS_AUDIT.md F-22."""
    failures = []
    for stmt in INCREMENTAL_SCHEMA_STATEMENTS:
        try:
            with con.transaction():
                con.execute(stmt)
        except Exception as e:
            failures.append((stmt, str(e)))
    if failures:
        db_error_message = "; ".join(
            f"{stmt.split(chr(10))[0][:90]}: {err}" for stmt, err in failures
        )
        con.execute(
            "INSERT INTO settings (key, value) VALUES ('migration_failures', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (db_error_message,),
        )
        print(f"\n  !! {len(failures)} of {len(INCREMENTAL_SCHEMA_STATEMENTS)} incremental "
              f"statement(s) could not be applied:")
        for stmt, err in failures:
            print(f"     - {stmt.split(chr(10))[0][:90]}\n       {err}")
        print("     The app will still start, but the features these support may not work.")
        print("     Resolve the underlying data issue and restart.\n")
    else:
        con.execute("DELETE FROM settings WHERE key = 'migration_failures'")
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

    # In-app updates (Settings -> Updates) are on by default for every new
    # install — this switches onto the versioned-release layout
    # automatically, the same as running setup.py --enable-updates by hand
    # used to require. Skipped when already running from inside a managed
    # release, or when --no-enable-updates is passed (e.g. a plain local
    # dev checkout that deliberately wants to keep running in place).
    # "Already inside a managed release" is checked two ways: VETCLINICSYSTEMJO_DATA_DIR
    # is set when launched through the real launcher, but someone can also
    # cd into a release folder and run setup.py by hand with no env vars
    # set at all — the structural check (this folder is literally named
    # app_v* directly under a vetclinicsystemjo-releases/ folder) catches that case
    # too, since re-running enable_updates() from in there would resolve
    # data_dir/releases_dir relative to the WRONG parent and nest a second,
    # broken layout inside the first.
    in_release_folder = (
        os.path.basename(BASE_DIR).startswith("app_v")
        and os.path.basename(os.path.dirname(BASE_DIR)) == "vetclinicsystemjo-releases"
    )
    already_managed = bool(os.environ.get("VETCLINICSYSTEMJO_DATA_DIR")) or in_release_folder
    if already_managed or "--no-enable-updates" in sys.argv:
        # Managed installs still want their Desktop shortcut kept current;
        # only the layout switch below is a one-time thing.
        if already_managed:
            ensure_desktop_shortcut()
        print(
            "\nAll set. Start the app with:\n"
            "  python3 app.py\n"
            "\n(macOS: double-click 'Start VetClinicSystem JO.command'."
            "  Windows: double-click 'Start VetClinicSystem JO.bat'.)\n"
        )
        return

    enable_updates()


def ensure_desktop_shortcut(data_dir=None):
    """Creates (or refreshes) the Desktop shortcut — the fail-safe way to start
    the app when autostart didn't fire or was never turned on. Re-run on every
    setup.py pass, not just the first, so a shortcut someone deleted comes
    back and one left pointing at an old path gets corrected.

    Never fatal: an install that can't get a Desktop icon is still a perfectly
    working install, so this only ever reports what happened."""
    step("Desktop shortcut")
    try:
        import desktop_shortcut
    except ImportError as e:
        print(f"  Skipped — could not load desktop_shortcut.py ({e}).")
        return
    if not desktop_shortcut.is_supported():
        print("  Skipped — not supported on this operating system.")
        return
    ok, message = desktop_shortcut.create(data_dir)
    print(f"  {message}")


# ---------------------------------------------------------------------------
# One-time opt-in: switch this install onto the versioned-release layout
# the in-app updater (Settings -> Updates, updater.py) needs. Not run by
# default main() — an admin runs `python3 setup.py --enable-updates`
# deliberately, since it moves .env/logs/attachments out of this folder.
# See UPDATE_MECHANISM_PLAN.md §3 for the target layout.
# ---------------------------------------------------------------------------
_MACOS_LAUNCHER = """#!/bin/bash
# VetClinicSystem JO — supervisor launcher (macOS). Lives in vetclinicsystemjo-data/,
# OUTSIDE any versioned release folder, so it survives every update.
# Reads active_release.txt fresh on every loop iteration to know which
# vetclinicsystemjo-releases/app_vX.Y.Z/ to run, and restarts automatically if the
# app process exits for any reason — a crash, or the deliberate exit
# updater.py triggers after promoting a new release (see
# updater.py's _request_restart()). updater.py has already proven the new
# release boots and passes /health, on a throwaway port, before ever
# flipping the pointer that controls what this loop runs next — this
# script's only job is to keep something running and pick up that change.
set -u
DATA_DIR="$(cd "$(dirname "$0")" && pwd)"
RELEASES_DIR="$(cd "$DATA_DIR/../vetclinicsystemjo-releases" && pwd)"
POINTER="$DATA_DIR/active_release.txt"
PORT="${VETCLINICSYSTEMJO_PORT:-5050}"
opened_browser=false

echo "VetClinicSystem JO is running at http://127.0.0.1:$PORT"
echo "Leave this window open while you use the app."
echo "Close this window (or press Control-C) to stop it."
echo ""

while true; do
  ACTIVE=$(cat "$POINTER" 2>/dev/null || true)
  if [ -z "$ACTIVE" ] || [ ! -d "$RELEASES_DIR/$ACTIVE" ]; then
    echo "No valid release at $POINTER — can't start. Run setup.py --enable-updates again?"
    read -p "Press Return to close this window..."
    exit 1
  fi
  RELEASE_DIR="$RELEASES_DIR/$ACTIVE"
  echo "Starting $ACTIVE..."
  VETCLINICSYSTEMJO_DATA_DIR="$DATA_DIR" VETCLINICSYSTEMJO_RELEASES_DIR="$RELEASES_DIR" VETCLINICSYSTEMJO_PORT="$PORT" \\
    "$RELEASE_DIR/venv/bin/python3" "$RELEASE_DIR/app.py" &
  APP_PID=$!

  if [ "$opened_browser" = false ]; then
    ( sleep 1.5 && open "http://127.0.0.1:$PORT" ) &
    opened_browser=true
  fi

  wait "$APP_PID"
  echo "VetClinicSystem JO exited (code $?) — restarting in 2 seconds..."
  sleep 2
done
"""

_WINDOWS_LAUNCHER = """@echo off
REM VetClinicSystem JO — supervisor launcher (Windows). Lives in vetclinicsystemjo-data\\,
REM OUTSIDE any versioned release folder, so it survives every update.
REM Reads active_release.txt fresh on every loop iteration — see the
REM matching comment in the macOS launcher (Start VetClinicSystem JO.command) for
REM why this loop doesn't need its own health-check/rollback logic.
setlocal
set "DATA_DIR=%~dp0"
set "RELEASES_DIR=%DATA_DIR%..\\vetclinicsystemjo-releases"
set "POINTER=%DATA_DIR%active_release.txt"
if not defined VETCLINICSYSTEMJO_PORT set "VETCLINICSYSTEMJO_PORT=5050"
set "OPENED_BROWSER=0"

:loop
set /p ACTIVE=<"%POINTER%"
if not exist "%RELEASES_DIR%\\%ACTIVE%" (
  echo No valid release at %POINTER% — can't start. Run setup.py --enable-updates again?
  pause
  exit /b 1
)
set "RELEASE_DIR=%RELEASES_DIR%\\%ACTIVE%"
echo Starting %ACTIVE%...
set "VETCLINICSYSTEMJO_DATA_DIR=%DATA_DIR%"
set "VETCLINICSYSTEMJO_RELEASES_DIR=%RELEASES_DIR%"
if "%OPENED_BROWSER%"=="0" (
  start "" http://127.0.0.1:%VETCLINICSYSTEMJO_PORT%
  set "OPENED_BROWSER=1"
)
"%RELEASE_DIR%\\venv\\Scripts\\python.exe" "%RELEASE_DIR%\\app.py"
echo VetClinicSystem JO exited — restarting in 2 seconds...
timeout /t 2 /nobreak >nul
goto loop
"""


def _copy_release_snapshot(dest):
    """Copies the current codebase into dest, excluding everything that
    belongs to a specific machine/install rather than the versioned app
    itself (venv, .git, __pycache__, and anything already destined for
    vetclinicsystemjo-data/)."""
    exclude = {"venv", ".git", "__pycache__", "logs", ".env", "vetclinicsystemjo-data", "vetclinicsystemjo-releases"}
    shutil.copytree(
        BASE_DIR, dest,
        ignore=lambda src, names: [n for n in names if n in exclude or n.startswith(".env")],
    )


def enable_updates(data_dir=None, releases_dir=None):
    step("Switching to the versioned-release layout")
    parent = os.path.dirname(BASE_DIR)
    data_dir = os.path.abspath(data_dir or os.path.join(parent, "vetclinicsystemjo-data"))
    releases_dir = os.path.abspath(releases_dir or os.path.join(parent, "vetclinicsystemjo-releases"))
    pointer = os.path.join(data_dir, "active_release.txt")

    if os.path.isfile(pointer):
        print(f"  Already enabled — {pointer} exists.")
        ensure_desktop_shortcut(data_dir)
        print(f"  VETCLINICSYSTEMJO_DATA_DIR={data_dir}\n  VETCLINICSYSTEMJO_RELEASES_DIR={releases_dir}")
        return

    version_path = os.path.join(BASE_DIR, "VERSION")
    if not os.path.isfile(version_path):
        print("  No VERSION file in this codebase — can't determine the release name. Aborting.")
        sys.exit(1)
    version = open(version_path).read().strip()
    release_name = f"app_v{version}"
    release_path = os.path.join(releases_dir, release_name)

    print(f"  This will:\n"
          f"    - create {data_dir}/ (persistent: .env, logs, attachments, backups)\n"
          f"    - create {releases_dir}/{release_name}/ (a copy of this codebase)\n"
          f"    - move .env, logs/, attachments/uploads/ into {data_dir}/\n"
          f"    - write new launcher scripts into {data_dir}/\n"
          f"  This folder ({BASE_DIR}) is left as-is otherwise — nothing here is deleted.\n")

    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(releases_dir, exist_ok=True)
    os.makedirs(os.path.join(data_dir, "backups"), exist_ok=True)

    print(f"  Copying codebase into {release_path} ...")
    if os.path.isdir(release_path):
        shutil.rmtree(release_path)
    _copy_release_snapshot(release_path)

    print("  Creating this release's own virtual environment...")
    subprocess.run([sys.executable, "-m", "venv", os.path.join(release_path, "venv")], check=True)
    venv_py = os.path.join(release_path, "venv", "Scripts" if sys.platform == "win32" else "bin",
                            "python.exe" if sys.platform == "win32" else "python3")
    subprocess.run([venv_py, "-m", "pip", "install", "-q", "-r", "requirements.txt"],
                    check=True, cwd=release_path)

    env_src = os.path.join(BASE_DIR, ".env")
    env_dst = os.path.join(data_dir, ".env")
    if os.path.isfile(env_src) and not os.path.isfile(env_dst):
        shutil.move(env_src, env_dst)
        print(f"  Moved .env -> {env_dst}")

    logs_src = os.path.join(BASE_DIR, "logs")
    logs_dst = os.path.join(data_dir, "logs")
    os.makedirs(logs_dst, exist_ok=True)
    if os.path.isdir(logs_src):
        for name in os.listdir(logs_src):
            shutil.move(os.path.join(logs_src, name), os.path.join(logs_dst, name))

    uploads_src = os.path.join(BASE_DIR, "uploads")
    uploads_dst = os.path.join(data_dir, "attachments", "uploads")
    if os.path.isdir(uploads_src):
        os.makedirs(os.path.dirname(uploads_dst), exist_ok=True)
        shutil.move(uploads_src, uploads_dst)
        print(f"  Moved uploads/ -> {uploads_dst}")

    with open(pointer, "w") as f:
        f.write(release_name)

    mac_launcher = os.path.join(data_dir, "Start VetClinicSystem JO.command")
    win_launcher = os.path.join(data_dir, "Start VetClinicSystem JO.bat")
    with open(mac_launcher, "w", newline="\n") as f:
        f.write(_MACOS_LAUNCHER)
    os.chmod(mac_launcher, 0o755)
    with open(win_launcher, "w", newline="\r\n") as f:
        f.write(_WINDOWS_LAUNCHER)

    ensure_desktop_shortcut(data_dir)

    print(
        f"\nDone. Add these two lines to {env_dst}:\n\n"
        f"  VETCLINICSYSTEMJO_DATA_DIR={data_dir}\n"
        f"  VETCLINICSYSTEMJO_RELEASES_DIR={releases_dir}\n\n"
        f"Then start the app from now on with:\n"
        f"  {mac_launcher}   (macOS)\n"
        f"  {win_launcher}   (Windows)\n\n"
        f"Not from {os.path.join(BASE_DIR, 'Start VetClinicSystem JO.command')} anymore — that copy has no "
        f"way to pick up future updates. This original folder is untouched and safe to keep "
        f"around, but the copy under {releases_dir}/ is what actually runs from now on.\n"
    )


if __name__ == "__main__":
    if "--desktop-shortcut" in sys.argv:
        ensure_desktop_shortcut()
    elif "--enable-updates" in sys.argv:
        enable_updates()
    else:
        main()
