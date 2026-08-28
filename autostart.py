"""
"Start VetClinicSystem JO automatically when this computer starts" — the Settings
page toggle for this installs/removes a normal OS login item, the same
mechanism any desktop app uses; it isn't a background service and doesn't
need admin/root privileges to set up.

macOS: a LaunchAgent plist in ~/Library/LaunchAgents, loaded with launchctl.
Windows: a shortcut in the current user's Startup folder.
Anything else (Linux server installs, etc.): not supported — is_supported()
returns False and the Settings page shows the toggle disabled.

This deliberately re-derives its enabled/disabled state from whether the
OS-level file actually exists each time (is_enabled()) rather than trusting
a flag stored in the database — the database and "does launchctl actually
have this loaded" can drift (e.g. someone removes the LaunchAgent by hand),
and the toggle should always reflect the real, current state of the
computer it's running on.
"""
import os
import platform
import subprocess

AGENT_LABEL = "com.vetclinicsystemjo.autostart"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def is_supported():
    return platform.system() in ("Darwin", "Windows")


def managed_data_dir():
    """Returns vetclinicsystemjo-data/ if this install has been switched onto
    the versioned-release layout (setup.py --enable-updates), whether or not
    the CURRENTLY RUNNING process happens to be using it. Autostart must
    always target the update-aware supervisor launcher that lives in that
    folder — never a specific release snapshot's own static launcher, or the
    original checkout's — otherwise a reboot after the next update silently
    keeps running whatever was pinned when autostart was enabled, since only
    the supervisor launcher re-reads active_release.txt on every start.

    Checked two ways: the env var the supervisor launcher sets when it
    starts app.py (fast path, no filesystem walk needed), or — since
    autostart can be toggled from a process that isn't running that way,
    e.g. the original checkout after enable_updates() has already been run
    elsewhere — the sibling folder structure on disk, mirroring setup.py's
    own already_managed check.

    Public because desktop_shortcut.py needs the exact same answer for the
    exact same reason — both have to resolve to the update-aware supervisor
    launcher rather than whichever code copy is running right now."""
    env_dir = os.environ.get("VETCLINICSYSTEMJO_DATA_DIR")
    if env_dir and os.path.isfile(os.path.join(env_dir, "active_release.txt")):
        return env_dir
    # BASE_DIR is either the original checkout, or — if this process is
    # itself running from inside a managed release — app_vX.Y.Z/ one level
    # under vetclinicsystemjo-releases/, in which case the sibling data dir
    # is two levels up, not one.
    in_release_folder = (
        os.path.basename(BASE_DIR).startswith("app_v")
        and os.path.basename(os.path.dirname(BASE_DIR)) == "vetclinicsystemjo-releases"
    )
    parent = os.path.dirname(os.path.dirname(BASE_DIR)) if in_release_folder else os.path.dirname(BASE_DIR)
    candidate = os.path.join(parent, "vetclinicsystemjo-data")
    if os.path.isfile(os.path.join(candidate, "active_release.txt")):
        return candidate
    return None


def _macos_plist_path():
    return os.path.expanduser(f"~/Library/LaunchAgents/{AGENT_LABEL}.plist")


def _macos_launcher_path():
    data_dir = managed_data_dir()
    base = data_dir if data_dir else BASE_DIR
    return os.path.join(base, "Start VetClinicSystem JO.command")


# --- Windows: Task Scheduler ------------------------------------------------
#
# The Startup folder runs a program at USER LOGON, not at boot. That is a real
# gap for a clinic PC, and the common case is the worst one: Windows Update
# reboots the machine at 3am, it then sits at the lock screen until someone
# arrives at 8am, and in the meantime nothing runs — no app, no nightly backup,
# no heartbeat. A Scheduled Task set to ONSTART runs without anyone logging in.
#
# Creating a boot task requires Administrator. When that is not available this
# falls back to the Startup folder and SAYS SO, rather than reporting success
# for a weaker guarantee than the user asked for.
#
# **A caveat that matters more than this module does:** the app needs its
# database. If PostgreSQL is installed as a Windows service it starts at boot
# too and all of this works. If it is running under Docker Desktop, Docker
# Desktop itself starts at user logon — so the database will not be there
# either, and starting the app earlier achieves nothing. Deploy Postgres as a
# service, or accept that backups happen after the first logon.
TASK_NAME = "VetClinicSystemJO Autostart"

# Give Postgres a moment after boot before the app tries to connect. Not
# load-bearing — the launcher script's supervisor loop restarts the app if it
# exits — but it avoids a burst of failed starts in the log every boot.
# schtasks parses /DELAY as mmmm:ss (MINUTES:seconds), not hours:minutes.
# "0001:00" is one minute. Widening this to five minutes is "0005:00";
# "0000:05" would be five SECONDS.
TASK_BOOT_DELAY = "0001:00"


def _run_schtasks(args):
    """Returns (ok, output). Never raises, including when schtasks is absent."""
    import subprocess
    try:
        p = subprocess.run(["schtasks"] + args, capture_output=True, text=True, timeout=30)
        return p.returncode == 0, ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as e:
        return False, str(e)


def _windows_task_exists():
    ok, _ = _run_schtasks(["/Query", "/TN", TASK_NAME])
    return ok


def _windows_task_create():
    """Create the boot task. Returns (ok, message)."""
    launcher = _windows_launcher_path()
    if not os.path.isfile(launcher):
        return False, "launcher not found"
    # /TR is parsed by schtasks itself, so the path is quoted INSIDE the
    # argument; passing argv as a list only protects it from the shell.
    target = f'"{launcher}"'
    base = ["/Create", "/TN", TASK_NAME, "/TR", target,
            "/SC", "ONSTART", "/RU", "SYSTEM", "/RL", "HIGHEST", "/F"]
    ok, out = _run_schtasks(base + ["/DELAY", TASK_BOOT_DELAY])
    if ok:
        return True, out
    # /DELAY is not accepted by every Windows build's schtasks. Losing the
    # delay is worth far less than losing the task, so try again without it.
    return _run_schtasks(base)


def _windows_task_delete():
    return _run_schtasks(["/Delete", "/TN", TASK_NAME, "/F"])


def _windows_startup_dir():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def _windows_shortcut_path():
    d = _windows_startup_dir()
    return os.path.join(d, "VetClinicSystem JO.bat") if d else None


def _windows_launcher_path():
    data_dir = managed_data_dir()
    base = data_dir if data_dir else BASE_DIR
    return os.path.join(base, "Start VetClinicSystem JO.bat")


def is_enabled():
    system = platform.system()
    if system == "Darwin":
        return os.path.isfile(_macos_plist_path())
    if system == "Windows":
        # Either mechanism counts as on, so the Settings checkbox reflects
        # reality after an admin-created boot task as well as after a
        # Startup-folder fallback.
        path = _windows_shortcut_path()
        if path and os.path.isfile(path):
            return True
        return _windows_task_exists()
    return False


def enable():
    system = platform.system()
    if system == "Darwin":
        return _macos_enable()
    if system == "Windows":
        return _windows_enable()
    return False, "Automatic startup isn't supported on this operating system."


def disable():
    system = platform.system()
    if system == "Darwin":
        return _macos_disable()
    if system == "Windows":
        return _windows_disable()
    return False, "Automatic startup isn't supported on this operating system."


def _macos_enable():
    launcher = _macos_launcher_path()
    if not os.path.isfile(launcher):
        return False, f"Could not find “Start VetClinicSystem JO.command” at {launcher} — can't set up automatic startup."
    plist_path = _macos_plist_path()
    log_dir = os.path.join(BASE_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{AGENT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>{launcher}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{os.path.join(log_dir, "autostart.log")}</string>
    <key>StandardErrorPath</key>
    <string>{os.path.join(log_dir, "autostart.log")}</string>
</dict>
</plist>
"""
    try:
        os.makedirs(os.path.dirname(plist_path), exist_ok=True)
        with open(plist_path, "w") as f:
            f.write(plist)
        subprocess.run(["launchctl", "unload", plist_path], capture_output=True, text=True)
        result = subprocess.run(["launchctl", "load", plist_path], capture_output=True, text=True)
        if result.returncode != 0:
            os.remove(plist_path)
            return False, f"Could not register automatic startup: {result.stderr.strip() or 'launchctl failed.'}"
        return True, "VetClinicSystem JO will now start automatically when you log in."
    except OSError as e:
        return False, f"Could not set up automatic startup: {e}"


def _macos_disable():
    plist_path = _macos_plist_path()
    if not os.path.isfile(plist_path):
        return True, "Automatic startup is already off."
    subprocess.run(["launchctl", "unload", plist_path], capture_output=True, text=True)
    try:
        os.remove(plist_path)
    except OSError as e:
        return False, f"Could not remove automatic startup: {e}"
    return True, "Automatic startup turned off."


def _windows_startup_folder_enable(launcher):
    """The fallback: runs at user logon. Returns (ok, message)."""
    shortcut_path = _windows_shortcut_path()
    if not shortcut_path:
        return False, "Could not find this account's Startup folder (%APPDATA% isn't set)."
    try:
        os.makedirs(os.path.dirname(shortcut_path), exist_ok=True)
        # A tiny .bat that calls the real launcher is simpler and more
        # transparent than a binary .lnk shortcut (no extra library, no
        # COM), and Windows runs .bat files placed in the Startup folder
        # exactly the same way it runs shortcuts placed there.
        with open(shortcut_path, "w") as f:
            f.write(f'@echo off\r\ncall "{launcher}"\r\n')
        return True, ""
    except OSError as e:
        return False, f"Could not set up automatic startup: {e}"


def _windows_startup_folder_remove():
    """Remove the logon-time entry, if present. Returns (ok, error)."""
    path = _windows_shortcut_path()
    if not path or not os.path.isfile(path):
        return True, ""
    try:
        os.remove(path)
        return True, ""
    except OSError as e:
        return False, str(e)


def _windows_enable():
    """Prefers a boot-time Scheduled Task; falls back to the Startup folder.

    Exactly one of the two is ever active — see the comment below for why
    writing both is actively harmful. _windows_disable() removes both
    regardless, so turning the feature off cannot strand a boot task.
    """
    launcher = _windows_launcher_path()
    if not os.path.isfile(launcher):
        return False, f"Could not find “Start VetClinicSystem JO.bat” at {launcher} — can't set up automatic startup."

    task_ok, task_out = _windows_task_create()

    if task_ok:
        # Exactly ONE mechanism must be active. Writing both makes the app
        # fight itself: the boot task already holds the port, so the copy the
        # Startup entry launches at sign-in cannot bind and exits — and the
        # launcher is a SUPERVISOR LOOP with no port check and no exit
        # condition, so it relaunches every 2 seconds for the whole logon
        # session, console window and browser tab included. Remove any entry
        # left by an earlier non-elevated enable.
        _windows_startup_folder_remove()
        return True, ("VetClinicSystem JO will now start automatically when this "
                      "computer starts up, even before anyone signs in.")

    folder_ok, folder_err = _windows_startup_folder_enable(launcher)
    if folder_ok:
        # Say plainly what was and was not achieved. Reporting plain success
        # here would leave someone believing the clinic is covered overnight
        # when it is only covered from the first sign-in.
        return True, ("VetClinicSystem JO will now start automatically when you sign in. "
                      "It could not be set to start at boot as well, which needs "
                      "Administrator — so if this computer restarts overnight, the app "
                      "won't run (and no backup will be taken) until someone signs in. "
                      "To fix that, run this app as an administrator once and turn this "
                      "setting on again.")
    return False, folder_err or f"Could not set up automatic startup. {task_out}".strip()


def _windows_disable():
    """Removes both mechanisms. Succeeds if neither is left behind."""
    task_removed, task_out = (True, "")
    if _windows_task_exists():
        task_removed, task_out = _windows_task_delete()

    shortcut_path = _windows_shortcut_path()
    folder_removed = True
    folder_err = ""
    if shortcut_path and os.path.isfile(shortcut_path):
        try:
            os.remove(shortcut_path)
        except OSError as e:
            folder_removed = False
            folder_err = str(e)

    if task_removed and folder_removed:
        return True, "Automatic startup turned off."
    if not task_removed:
        return False, ("Could not remove the startup task — it may need Administrator. "
                       f"{task_out}").strip()
    return False, f"Could not remove automatic startup: {folder_err}"
