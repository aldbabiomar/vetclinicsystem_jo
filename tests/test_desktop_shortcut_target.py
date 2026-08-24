"""
Regression guard for the trap desktop_shortcut.py exists to avoid, and that
autostart.py shipped with for a while before it was caught (see
CHANGELOG 1.3.1): resolving the launcher relative to whichever copy of the
code happens to be running, which pins the shortcut to one frozen
vetclinicsystemjo-releases/app_vX.Y.Z/ snapshot. The next update flips
active_release.txt, that snapshot eventually gets pruned, and the shortcut
is left starting an old version — or nothing at all.

The only correct target is the supervisor launcher in vetclinicsystemjo-data/,
which re-reads active_release.txt on every start. These tests pin that.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import autostart  # noqa: E402
import desktop_shortcut  # noqa: E402

LAUNCHER_NAME = "Start VetClinicSystem JO.command"


def _managed_install(root):
    """Builds the on-disk shape enable_updates() produces: a data dir holding
    the pointer + supervisor launcher, beside a releases dir with one release."""
    data_dir = os.path.join(root, "vetclinicsystemjo-data")
    release_dir = os.path.join(root, "vetclinicsystemjo-releases", "app_v9.9.9")
    os.makedirs(data_dir)
    os.makedirs(release_dir)
    with open(os.path.join(data_dir, "active_release.txt"), "w") as f:
        f.write("app_v9.9.9")
    with open(os.path.join(data_dir, LAUNCHER_NAME), "w") as f:
        f.write("#!/bin/bash\ntrue\n")
    # The release snapshot carries its own copy of the launcher — this is the
    # decoy the resolution must not latch onto.
    with open(os.path.join(release_dir, LAUNCHER_NAME), "w") as f:
        f.write("#!/bin/bash\ntrue\n")
    return data_dir, release_dir


@pytest.fixture
def sandbox(monkeypatch):
    """A managed install in a temp dir, with the module-level BASE_DIR and the
    Desktop location pointed inside it so nothing touches the real Desktop."""
    with tempfile.TemporaryDirectory() as root:
        data_dir, release_dir = _managed_install(root)
        desktop = os.path.join(root, "Desktop")
        os.makedirs(desktop)
        bundle = os.path.join(desktop, "VetClinicSystem JO.app")
        monkeypatch.setattr(desktop_shortcut, "_macos_bundle_path", lambda: bundle)
        monkeypatch.delenv("VETCLINICSYSTEMJO_DATA_DIR", raising=False)
        yield {"root": root, "data_dir": data_dir, "release_dir": release_dir,
               "bundle": bundle}


def _script(bundle):
    with open(os.path.join(bundle, "Contents", "MacOS", "launch")) as f:
        return f.read()


def _launcher_line(bundle):
    """Just the line that decides what actually gets run. Asserting against the
    whole script would trip over its own header comment, which names the
    releases folder precisely to explain why it is not used."""
    return next(l for l in _script(bundle).splitlines() if l.startswith("LAUNCHER="))


def test_running_from_a_release_folder_still_targets_the_data_dir(sandbox, monkeypatch):
    """The case that actually bites: the app is running out of
    app_v9.9.9/, so a naive __file__-relative resolution would bake that
    path in."""
    monkeypatch.setattr(autostart, "BASE_DIR", sandbox["release_dir"])

    ok, message = desktop_shortcut._macos_create()
    assert ok, message

    line = _launcher_line(sandbox["bundle"])
    assert sandbox["data_dir"] in line
    assert "vetclinicsystemjo-releases" not in line, (
        "shortcut baked a release-folder path — it will break on the next update")


def test_running_from_the_original_checkout_also_targets_the_data_dir(sandbox, monkeypatch):
    """Someone re-running setup.py from the folder they first downloaded,
    after updates were enabled from there earlier."""
    checkout = os.path.join(sandbox["root"], "vetclinicsystem_jo-main")
    os.makedirs(checkout)
    monkeypatch.setattr(autostart, "BASE_DIR", checkout)

    ok, message = desktop_shortcut._macos_create()
    assert ok, message

    line = _launcher_line(sandbox["bundle"])
    assert os.path.join(sandbox["data_dir"], LAUNCHER_NAME) in line
    assert checkout not in line


def test_explicit_data_dir_is_honoured(sandbox, monkeypatch):
    monkeypatch.setattr(autostart, "BASE_DIR", sandbox["release_dir"])
    ok, message = desktop_shortcut._macos_create(sandbox["data_dir"])
    assert ok, message
    assert sandbox["data_dir"] in _launcher_line(sandbox["bundle"])


def test_unmanaged_install_refuses_rather_than_guessing(sandbox, monkeypatch):
    """No versioned-release layout anywhere: there is no update-proof launcher
    to point at, so this must decline instead of pinning the local copy and
    quietly creating a shortcut that stops working after the first update."""
    plain = os.path.join(sandbox["root"], "somewhere-else", "app")
    os.makedirs(plain)
    monkeypatch.setattr(autostart, "BASE_DIR", plain)

    ok, message = desktop_shortcut._macos_create()
    assert not ok
    assert "updates" in message.lower()
    assert not os.path.exists(sandbox["bundle"])


def test_generated_script_quotes_paths_with_spaces(sandbox, monkeypatch):
    """Every real install path here contains a space ("Start VetClinicSystem
    JO.command"), so an unquoted interpolation would word-split at runtime."""
    monkeypatch.setattr(autostart, "BASE_DIR", sandbox["release_dir"])
    desktop_shortcut._macos_create()
    line = _launcher_line(sandbox["bundle"])
    assert line == f"LAUNCHER='{os.path.join(sandbox['data_dir'], LAUNCHER_NAME)}'"
