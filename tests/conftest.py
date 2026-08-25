"""
Shared test setup.

app.py refuses to import without a real SECRET_KEY (a deliberate fail-fast
so nobody boots the app with the placeholder from .env.example), and it
lives at the repo root rather than on the default import path. Both are
fixed here so individual test modules can just `import app` / `import
logic` / `import money`.

Nothing here touches a database. Every test in test_money.py exercises
pure functions, so the suite runs anywhere — no Postgres, no Docker, no
isolated test environment. That is the point: the money math is the part
most worth checking on every change, so checking it has to be free.
"""
import os
import sys
import pathlib

REPO_ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Any non-placeholder value satisfies app.py's guard. Set before import.
os.environ.setdefault("SECRET_KEY", "test-only-key-not-used-for-real-sessions")
