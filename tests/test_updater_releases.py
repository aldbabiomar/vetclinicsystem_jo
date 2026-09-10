"""
Version ordering in updater.py — the module that replaces the running app.

updater.py had no unit tests at all. It compared versions as strings in two
places, and both were wrong once a component reached two digits:

  "app_v1.9.0" > "app_v1.12.1"        # lexicographically true

* list_releases() sorted release folders with that comparison, and
  rollback_to_previous() takes candidates[0] from it. With three or more
  releases on disk, Rollback offered the OLDEST folder as "the most recent
  other release" — so an admin recovering from a bad update would be moved
  back several minor versions, across schema migrations, while the UI told
  them otherwise.

* is_update_available() asked `tag != current_version()`, which reports an
  update for any difference at all — including a lower version. A deleted
  release, a re-pointed /releases/latest, or a republished older tag would
  be offered to the clinic and installed.

Both are pure functions over names and strings, so these tests need no
database, no network and no release layout — only the module's own globals,
which are monkeypatched here.
"""
import os

import pytest

import updater


@pytest.fixture
def releases(tmp_path, monkeypatch):
    """A fake releases directory. Returns a helper that creates the given
    release folders and points the module at them."""
    def make(*names):
        root = tmp_path / "releases"
        root.mkdir(exist_ok=True)
        for n in names:
            (root / n).mkdir(exist_ok=True)
        data = tmp_path / "data"
        data.mkdir(exist_ok=True)
        monkeypatch.setattr(updater, "RELEASES_DIR", str(root))
        monkeypatch.setattr(updater, "DATA_DIR", str(data))
        monkeypatch.setattr(updater, "GITHUB_REPO", "example/repo")
        return root
    return make


# ---------------------------------------------------------------------------
# list_releases() ordering
# ---------------------------------------------------------------------------

def test_two_digit_minor_sorts_above_single_digit(releases):
    """GUARD. This is the exact comparison a string sort gets backwards."""
    releases("app_v1.9.0", "app_v1.10.0", "app_v1.12.1")
    assert updater.list_releases() == ["app_v1.12.1", "app_v1.10.0", "app_v1.9.0"]


def test_rollback_candidate_is_the_immediately_previous_release(releases):
    """GUARD. The behaviour that actually reached a user: with three folders
    on disk, the release Rollback would switch to.

    Reverting list_releases() to sorted(names, reverse=True) makes this
    return app_v1.9.0.
    """
    releases("app_v1.9.0", "app_v1.11.0", "app_v1.12.1")
    active = "app_v1.12.1"
    candidates = [n for n in updater.list_releases() if n != active]
    assert candidates[0] == "app_v1.11.0", (
        f"Rollback would switch to {candidates[0]}, not the release it replaced")


def test_two_releases_still_behave_exactly_as_before(releases):
    """CONTROL. The normal steady state — KEEP_RELEASES is 2 — where the old
    code happened to be right. The fix must not change this."""
    releases("app_v1.11.1", "app_v1.12.2")
    assert updater.list_releases() == ["app_v1.12.2", "app_v1.11.1"]
    candidates = [n for n in updater.list_releases() if n != "app_v1.12.2"]
    assert candidates == ["app_v1.11.1"]


def test_patch_and_major_components_also_compare_numerically(releases):
    """CONTROL for the parser: every component, not just the minor."""
    releases("app_v1.2.9", "app_v1.2.10", "app_v2.0.0", "app_v10.0.0")
    assert updater.list_releases() == [
        "app_v10.0.0", "app_v2.0.0", "app_v1.2.10", "app_v1.2.9"]


def test_an_unparseable_folder_name_does_not_break_rollback(releases):
    """GUARD. A stray directory must sort last, not raise — otherwise one
    hand-made folder takes Rollback out entirely."""
    releases("app_v1.11.0", "app_v1.12.1", "app_vBACKUP", "app_v1.12.1-old")
    listed = updater.list_releases()
    assert listed[0] == "app_v1.12.1"
    assert listed[1] == "app_v1.11.0"
    assert set(listed[2:]) == {"app_vBACKUP", "app_v1.12.1-old"}


def test_list_releases_ignores_files_and_foreign_directories(releases, tmp_path):
    """CONTROL. Only app_v* directories count."""
    root = releases("app_v1.12.1")
    (root / "notes.txt").write_text("x", encoding="utf-8")
    (root / "unrelated").mkdir()
    assert updater.list_releases() == ["app_v1.12.1"]


# ---------------------------------------------------------------------------
# _version_tuple()
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("1.12.1", (1, 12, 1)),
    ("v1.12.1", (1, 12, 1)),
    ("  1.2.3  ", (1, 2, 3)),
    ("1.12", None),
    ("1.12.1-rc1", None),
    ("unknown", None),
    ("", None),
    (None, None),
])
def test_version_tuple_parsing(raw, expected):
    assert updater._version_tuple(raw) == expected


# ---------------------------------------------------------------------------
# is_update_available() — strictly newer, not merely different
# ---------------------------------------------------------------------------

@pytest.fixture
def remote(monkeypatch):
    """Stub the GitHub call and the local VERSION read."""
    def set_versions(remote_tag, local_version):
        monkeypatch.setattr(updater, "check_latest_release",
                            lambda: {"tag_name": remote_tag, "body": "", "tarball_url": "x"})
        monkeypatch.setattr(updater, "current_version", lambda: local_version)
        monkeypatch.setattr(updater, "_log", lambda *a, **k: None)
    return set_versions


def test_a_newer_remote_release_is_offered(remote):
    """CONTROL. The ordinary case must keep working."""
    remote("v1.13.0", "1.12.1")
    available, _ = updater.is_update_available()
    assert available is True


def test_the_same_version_is_not_offered(remote):
    """CONTROL."""
    remote("v1.12.1", "1.12.1")
    available, _ = updater.is_update_available()
    assert available is False


def test_an_older_remote_release_is_not_offered_as_an_update(remote):
    """GUARD. `tag != current_version()` reports True here and installs a
    downgrade onto a clinic."""
    remote("v1.9.0", "1.12.1")
    available, _ = updater.is_update_available()
    assert available is False, "an older release was offered as an update"


def test_a_lower_minor_with_more_digits_locally_is_not_offered(remote):
    """GUARD. The two-digit case again, this time in the update check."""
    remote("v1.9.9", "1.10.0")
    available, _ = updater.is_update_available()
    assert available is False


def test_an_unparseable_remote_tag_is_never_installed(remote):
    """GUARD. Do not install something the version logic cannot reason about."""
    remote("nightly", "1.12.1")
    available, _ = updater.is_update_available()
    assert available is False


def test_an_unreadable_local_version_still_allows_recovery(remote):
    """CONTROL. A broken VERSION file must not lock the install out of
    updating — that would make a bad state unrecoverable in-app."""
    remote("v1.12.1", "unknown")
    available, _ = updater.is_update_available()
    assert available is True
