"""
Windows autostart — the Scheduled Task path.

WHY THIS EXISTS. The Startup folder runs a program at USER LOGON, not at boot.
The realistic clinic failure is Windows Update restarting the PC at 3am, the
machine then sitting at the lock screen until 8am, and nothing running in
between: no app, no nightly backup, no heartbeat. A Scheduled Task set to
ONSTART runs without anyone signing in.

WHAT THESE TESTS CAN AND CANNOT SHOW. They run on the developer's Mac, so they
verify the DECISIONS and the COMMANDS — that a boot task is attempted, that
the arguments are the ones that produce a boot task, that failure falls back
honestly, that disabling removes both mechanisms. They cannot show that
Windows accepts the command, that the task survives a reboot, or that the app
starts correctly with no desktop session.

**Nothing here is a substitute for running it on Windows once.** The macOS
sleep bug that prompted all of this was found by running the thing, not by
reading it.
"""
import os
import platform

import pytest

import autostart


@pytest.fixture
def on_windows(monkeypatch, tmp_path):
    """Pretend to be Windows, with a launcher that exists and a writable
    Startup folder, so the real code paths run on a Mac."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    launcher = tmp_path / "Start VetClinicSystem JO.bat"
    launcher.write_text("@echo off\r\n")
    startup = tmp_path / "Startup"
    startup.mkdir()
    monkeypatch.setattr(autostart, "_windows_launcher_path", lambda: str(launcher))
    monkeypatch.setattr(autostart, "_windows_shortcut_path",
                        lambda: str(startup / "VetClinicSystem JO.bat"))
    return {"launcher": str(launcher), "startup": startup,
            "shortcut": startup / "VetClinicSystem JO.bat"}


def _fake_schtasks(calls, ok=True):
    def run(args):
        calls.append(args)
        return ok, "" if ok else "ERROR: Access is denied."
    return run


# --- the boot task is actually attempted, with the right arguments ---------

def test_enable_creates_a_boot_task_not_just_a_logon_entry(on_windows, monkeypatch):
    calls = []
    monkeypatch.setattr(autostart, "_run_schtasks", _fake_schtasks(calls))

    ok, msg = autostart.enable()
    assert ok is True
    assert calls, "no schtasks command was run — only the Startup folder was used"

    create = calls[0]
    assert "/Create" in create
    assert "/SC" in create and create[create.index("/SC") + 1] == "ONSTART", (
        "the task must be ONSTART; ONLOGON would reproduce the very gap this fixes"
    )
    assert "/RU" in create and create[create.index("/RU") + 1] == "SYSTEM", (
        "without /RU SYSTEM the task cannot run before anyone signs in"
    )
    assert "/TN" in create and create[create.index("/TN") + 1] == autostart.TASK_NAME
    assert "/F" in create, "must overwrite an existing task rather than fail"


def test_the_launcher_path_is_quoted_for_schtasks(on_windows, monkeypatch):
    """schtasks parses /TR itself, so a path containing spaces — which this one
    always does — must carry its own quotes inside the argument."""
    calls = []
    monkeypatch.setattr(autostart, "_run_schtasks", _fake_schtasks(calls))
    autostart.enable()
    tr = calls[0][calls[0].index("/TR") + 1]
    assert tr.startswith('"') and tr.endswith('"'), f"/TR not quoted: {tr!r}"
    assert " " in tr, "this test is pointless if the path has no spaces"


def test_the_boot_message_promises_boot_not_signin(on_windows, monkeypatch):
    monkeypatch.setattr(autostart, "_run_schtasks", _fake_schtasks([], ok=True))
    ok, msg = autostart.enable()
    assert ok is True
    assert "before anyone signs in" in msg


# --- falling back honestly -------------------------------------------------

def test_without_admin_it_falls_back_and_says_what_was_lost(on_windows, monkeypatch):
    """The important one. Reporting plain success here would leave someone
    believing the clinic is covered overnight when it is only covered from the
    first sign-in."""
    monkeypatch.setattr(autostart, "_run_schtasks", _fake_schtasks([], ok=False))

    ok, msg = autostart.enable()
    assert ok is True, "the logon fallback still works, so this is not a failure"
    assert on_windows["shortcut"].is_file(), "the Startup entry was not written"
    assert "Administrator" in msg
    assert "won't run" in msg and "backup" in msg, (
        "the message must state the consequence, not just that something failed"
    )
    assert "before anyone signs in" not in msg, (
        "the fallback must not claim the boot guarantee it did not get"
    )


def test_a_missing_launcher_is_refused_before_anything_is_created(on_windows, monkeypatch):
    calls = []
    monkeypatch.setattr(autostart, "_run_schtasks", _fake_schtasks(calls))
    monkeypatch.setattr(autostart, "_windows_launcher_path",
                        lambda: os.path.join(str(on_windows["startup"]), "nope.bat"))
    ok, msg = autostart.enable()
    assert ok is False
    assert calls == [], "no task should be created for a launcher that isn't there"
    assert not on_windows["shortcut"].is_file()


# --- turning it off removes BOTH ------------------------------------------

def test_a_successful_boot_task_leaves_no_startup_entry(on_windows, monkeypatch):
    """Both at once is not redundancy, it is a respawn loop.

    The boot task holds the port, so the copy the Startup entry launches at
    sign-in cannot bind and exits — and the launcher is a supervisor loop with
    no port check and no exit condition, so it relaunches every 2 seconds for
    the whole session.
    """
    monkeypatch.setattr(autostart, "_run_schtasks", _fake_schtasks([], ok=True))
    ok, msg = autostart.enable()
    assert ok is True
    assert not on_windows["shortcut"].is_file(), (
        "a Startup entry alongside the boot task respawns the app every 2s"
    )


def test_a_stale_startup_entry_is_removed_once_the_task_succeeds(on_windows, monkeypatch):
    """Enable without admin, then again with it: the logon entry must go."""
    monkeypatch.setattr(autostart, "_run_schtasks", _fake_schtasks([], ok=False))
    autostart.enable()
    assert on_windows["shortcut"].is_file(), "fallback should have written it"

    monkeypatch.setattr(autostart, "_run_schtasks", _fake_schtasks([], ok=True))
    autostart.enable()
    assert not on_windows["shortcut"].is_file(), (
        "the earlier fallback entry survived and now collides with the task"
    )


def test_disable_removes_the_task_and_the_startup_entry(on_windows, monkeypatch):
    calls = []
    monkeypatch.setattr(autostart, "_run_schtasks", _fake_schtasks(calls, ok=False))
    autostart.enable()  # no admin -> Startup-folder fallback
    assert on_windows["shortcut"].is_file()
    calls.clear()

    monkeypatch.setattr(autostart, "_run_schtasks", _fake_schtasks(calls, ok=True))
    monkeypatch.setattr(autostart, "_windows_task_exists", lambda: True)
    ok, msg = autostart.disable()
    assert ok is True
    assert not on_windows["shortcut"].is_file(), "the Startup entry survived"
    assert any("/Delete" in c for c in calls), (
        "the boot task was left behind — it would keep starting the app after "
        "the user turned the setting off"
    )


def test_is_enabled_sees_a_task_even_with_no_startup_entry(on_windows, monkeypatch):
    monkeypatch.setattr(autostart, "_windows_task_exists", lambda: True)
    assert autostart.is_enabled() is True


def test_is_enabled_is_false_when_neither_exists(on_windows, monkeypatch):
    monkeypatch.setattr(autostart, "_windows_task_exists", lambda: False)
    assert autostart.is_enabled() is False


# --- robustness ------------------------------------------------------------

def test_schtasks_missing_entirely_does_not_raise(on_windows, monkeypatch):
    """A machine without schtasks on PATH, or a locked-down one, must fall
    back rather than take the Settings page down."""
    import subprocess

    def boom(*a, **k):
        raise FileNotFoundError("schtasks")

    monkeypatch.setattr(subprocess, "run", boom)
    ok, msg = autostart.enable()
    assert ok is True, "should have fallen back to the Startup folder"
    assert on_windows["shortcut"].is_file()


def test_the_delay_is_dropped_rather_than_losing_the_task(on_windows, monkeypatch):
    """/DELAY is not accepted by every Windows build. Losing a one-minute
    delay is worth far less than losing the boot task."""
    attempts = []

    def run(args):
        attempts.append(args)
        return ("/DELAY" not in args), ""

    monkeypatch.setattr(autostart, "_run_schtasks", run)
    ok, msg = autostart.enable()
    assert ok is True
    assert len(attempts) == 2, "should have retried once without /DELAY"
    assert "/DELAY" in attempts[0] and "/DELAY" not in attempts[1]
    assert "before anyone signs in" in msg
