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


def _macos_plist_path():
    return os.path.expanduser(f"~/Library/LaunchAgents/{AGENT_LABEL}.plist")


def _macos_launcher_path():
    return os.path.join(BASE_DIR, "Start VetClinicSystem JO.command")


def _windows_startup_dir():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def _windows_shortcut_path():
    d = _windows_startup_dir()
    return os.path.join(d, "VetClinicSystem JO.bat") if d else None


def _windows_launcher_path():
    return os.path.join(BASE_DIR, "Start VetClinicSystem JO.bat")


def is_enabled():
    system = platform.system()
    if system == "Darwin":
        return os.path.isfile(_macos_plist_path())
    if system == "Windows":
        path = _windows_shortcut_path()
        return bool(path and os.path.isfile(path))
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
        return False, "Could not find “Start VetClinicSystem JO.command” next to app.py — can't set up automatic startup."
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


def _windows_enable():
    launcher = _windows_launcher_path()
    if not os.path.isfile(launcher):
        return False, "Could not find “Start VetClinicSystem JO.bat” next to app.py — can't set up automatic startup."
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
        return True, "VetClinicSystem JO will now start automatically when you log in."
    except OSError as e:
        return False, f"Could not set up automatic startup: {e}"


def _windows_disable():
    shortcut_path = _windows_shortcut_path()
    if not shortcut_path or not os.path.isfile(shortcut_path):
        return True, "Automatic startup is already off."
    try:
        os.remove(shortcut_path)
    except OSError as e:
        return False, f"Could not remove automatic startup: {e}"
    return True, "Automatic startup turned off."
