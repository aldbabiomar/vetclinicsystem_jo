"""
The launcher must survive a Python upgrade.

Both supervisor launchers respawn the app forever if it exits. That is right
for a crash and wrong for a broken interpreter: when `brew upgrade` deletes
the versioned Cellar directory a venv was built against, `venv/bin/python3`
dangles, bash cannot exec it, and the loop respawns a missing interpreter
every two seconds indefinitely with nothing on screen explaining why.

That happened on a real install on 2026-09-02 — two releases, both
unstartable. The app does not 500, it never starts at all, so nothing in the
app itself can report it. Only the launcher can.

Needs nothing but a shell and python3; the rebuild is exercised against an
EMPTY requirements file so it stays offline and fast.
"""
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import setup  # noqa: E402


# ---------------------------------------------------------------------------
# The templates carry the check, and carry it BEFORE the app is launched
# ---------------------------------------------------------------------------

def test_the_macos_launcher_checks_python_before_starting_the_app():
    t = setup._MACOS_LAUNCHER
    probe = t.index('"$RELEASE_DIR/venv/bin/python3" -c ""')
    launch = t.index('"$RELEASE_DIR/venv/bin/python3" "$RELEASE_DIR/app.py"')
    assert probe < launch, (
        "the interpreter check runs after the app launch, so a broken venv "
        "still spins the restart loop"
    )
    assert "rm -rf \"$RELEASE_DIR/venv\"" in t and "-m venv" in t, (
        "the launcher detects a broken venv but never rebuilds it"
    )


def test_the_windows_launcher_checks_python_before_starting_the_app():
    t = setup._WINDOWS_LAUNCHER
    # _WINDOWS_LAUNCHER is the evaluated string, so the doubled backslashes
    # in setup.py's source are single backslashes here.
    probe = t.index(r'\venv\Scripts\python.exe" -c ""')
    launch = t.index(r'\venv\Scripts\python.exe" "%RELEASE_DIR%\app.py"')
    assert probe < launch, (
        "the interpreter check runs after the app launch on Windows"
    )
    assert ":rebuildfailed" in t, (
        "the Windows launcher jumps to a failure label that does not exist"
    )


def test_a_failed_rebuild_stops_instead_of_looping():
    """The whole point is to break the infinite respawn. If the rebuild fails
    the launcher must exit and say why, not fall through into the loop."""
    t = setup._MACOS_LAUNCHER
    block = t[t.index("Could not rebuild it"):]
    assert "exit 1" in block[:400], (
        "a failed rebuild does not exit — the launcher would loop forever "
        "on an unfixable environment, which is the bug this guards against"
    )


# ---------------------------------------------------------------------------
# The rebuild actually works, run for real
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="bash preflight; Windows path is the .bat")
def test_the_preflight_rebuilds_a_venv_broken_by_a_python_upgrade(tmp_path):
    """Reproduces the real fault: a venv whose interpreter symlink points at a
    directory that no longer exists, exactly as `brew upgrade` leaves it."""
    release = tmp_path / "app_v1.0.0"
    (release / "venv" / "bin").mkdir(parents=True)
    (release / "requirements.txt").write_text("")  # keeps pip offline and instant

    broken = release / "venv" / "bin" / "python3"
    broken.symlink_to("/opt/nonexistent/python@3.14/3.14.6/bin/python3")
    assert not broken.exists(), "the arrangement did not actually break the venv"
    # exec of a dangling symlink RAISES rather than returning non-zero, which
    # is why the launcher probes it through the shell rather than calling it.
    assert subprocess.run(["bash", "-c", f'"{broken}" -c ""'],
                          capture_output=True).returncode != 0

    # the preflight, lifted verbatim in shape from _MACOS_LAUNCHER
    script = f'''
    RELEASE_DIR="{release}"
    if ! "$RELEASE_DIR/venv/bin/python3" -c "" >/dev/null 2>&1; then
      rm -rf "$RELEASE_DIR/venv"
      python3 -m venv "$RELEASE_DIR/venv" >/dev/null 2>&1 && \\
        "$RELEASE_DIR/venv/bin/python3" -m pip install -q -r "$RELEASE_DIR/requirements.txt" \\
        || exit 1
      echo REBUILT
    fi
    "$RELEASE_DIR/venv/bin/python3" -c "print('APP WOULD START')"
    '''
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, f"preflight failed: {r.stderr[-400:]}"
    assert "REBUILT" in r.stdout, "the preflight did not rebuild the broken venv"
    assert "APP WOULD START" in r.stdout, (
        "the venv was rebuilt but the interpreter still does not run"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="bash preflight; Windows path is the .bat")
def test_the_preflight_leaves_a_working_venv_alone(tmp_path):
    """Control. Without this, a preflight that rebuilt unconditionally would
    pass the test above while adding a minute to every single startup."""
    release = tmp_path / "app_v1.0.0"
    release.mkdir(parents=True)
    (release / "requirements.txt").write_text("")
    subprocess.run([sys.executable, "-m", "venv", str(release / "venv")],
                   check=True, capture_output=True, timeout=180)

    script = f'''
    RELEASE_DIR="{release}"
    if ! "$RELEASE_DIR/venv/bin/python3" -c "" >/dev/null 2>&1; then
      echo REBUILT
    fi
    echo DONE
    '''
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    assert "REBUILT" not in r.stdout, (
        "a healthy venv was treated as broken — every startup would rebuild"
    )
    assert "DONE" in r.stdout


# ---------------------------------------------------------------------------
# Releases must not be built from a versioned interpreter path
# ---------------------------------------------------------------------------

def test_release_venvs_are_built_from_the_base_interpreter():
    """setup.py used sys.executable, which inside a venv is that venv's own
    python — so a release inherited whatever path its parent resolved to. On
    Homebrew that is a versioned Cellar path, and every release built from it
    was one `brew upgrade` away from being unstartable."""
    base = setup._base_interpreter()
    assert os.path.isfile(base), f"_base_interpreter() returned a non-file: {base}"
    assert subprocess.run([base, "-c", ""], capture_output=True).returncode == 0, (
        "_base_interpreter() returned something that will not run, which would "
        "make every release build fail"
    )
    src = (ROOT / "setup.py").read_text()
    assert 'subprocess.run([sys.executable, "-m", "venv"' not in src, (
        "a release venv is still being created from sys.executable"
    )


def test_base_interpreter_falls_back_rather_than_failing(monkeypatch):
    """Control. sys._base_executable is not guaranteed to exist or to point at
    a real file; the fallback must keep release-building working."""
    monkeypatch.setattr(sys, "_base_executable", "/nonexistent/python3", raising=False)
    assert setup._base_interpreter() == sys.executable
    monkeypatch.delattr(sys, "_base_executable", raising=False)
    assert setup._base_interpreter() == sys.executable
