"""
updater.describe_check_failure() — the sentence a clinic admin is shown when
"Check for Updates" cannot reach a verdict.

Why this file exists. Until 2026-09-10 every failure on that path produced one
fixed sentence: "Couldn't check for updates — offline, or GitHub is
unreachable." It was shown for a rate limit, a 404, a rejected token and a
real outage alike. The failure that actually reached a clinic was the rate
limit — GitHub allows 60 unauthenticated API calls per hour per IP address —
and the app responded by telling the admin to check an internet connection
that was working fine. A wrong diagnosis is worse than a vague one: it sends
someone to look in the wrong place. COMPARISON.md §46.

Pure: no database, no network, no app import. Every case below builds the
exception itself.
"""
import pathlib
from datetime import datetime, timedelta

import pytest

import updater


class _Resp:
    """The parts of a requests.Response that describe_check_failure() reads."""

    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


def _http_error(status, headers=None):
    import requests
    err = requests.HTTPError(f"HTTP {status}")
    err.response = _Resp(status, headers)
    return err


def _rate_limited(remaining="0", reset=None):
    headers = {"x-ratelimit-remaining": remaining}
    if reset is not None:
        headers["x-ratelimit-reset"] = str(reset)
    return _http_error(403, headers)


# --- the case that actually reached a clinic -------------------------------

def test_a_rate_limit_says_so_and_does_not_blame_the_connection():
    reset = int((datetime.now() + timedelta(minutes=11)).timestamp())
    msg = updater.describe_check_failure(_rate_limited(reset=reset))
    assert "limit" in msg.lower(), msg
    assert "offline" not in msg.lower(), (
        "the whole point of this function: a rate limit must not be reported "
        "as being offline")
    assert datetime.fromtimestamp(reset).strftime("%H:%M") in msg, (
        "the admin needs to know WHEN to try again, not just that they cannot now")


def test_a_rate_limit_with_no_reset_header_still_reads_sensibly():
    """GitHub always sends x-ratelimit-reset, but a proxy in front of the
    clinic might not. The sentence must not end up with a dangling ' — try
    again after '."""
    msg = updater.describe_check_failure(_rate_limited(reset=None))
    assert "limit" in msg.lower()
    assert "try again after" not in msg, msg
    assert not msg.rstrip().endswith("—"), msg


def test_a_reset_header_that_is_nonsense_is_ignored_rather_than_raising():
    """This runs on the error path. It must not become a second error."""
    for junk in ("", "not-a-number", "99999999999999999999", None):
        msg = updater.describe_check_failure(_rate_limited(reset=junk))
        assert "limit" in msg.lower()


# --- the other causes, each distinguishable from the others ----------------

def test_a_403_that_is_not_a_rate_limit_is_not_called_one():
    """403 covers both the hourly cap and plain 'you may not do that'.
    Remaining == 0 is what separates them; without that check every
    permissions problem would be reported as a rate limit."""
    msg = updater.describe_check_failure(_http_error(403, {"x-ratelimit-remaining": "57"}))
    assert "limit" not in msg.lower(), msg
    assert "403" in msg


def test_a_404_points_at_the_repository_not_the_network():
    msg = updater.describe_check_failure(_http_error(404))
    assert "offline" not in msg.lower()
    assert "repositor" in msg.lower() or "release" in msg.lower(), msg


def test_a_401_points_at_the_token():
    msg = updater.describe_check_failure(_http_error(401))
    assert "token" in msg.lower(), msg


def test_a_genuine_connection_failure_still_says_offline():
    """The control. If this stopped saying 'offline' the function would have
    over-corrected — a real outage is the one case where blaming the
    connection is right."""
    import requests
    msg = updater.describe_check_failure(requests.ConnectionError("no route to host"))
    assert "offline" in msg.lower(), msg


def test_a_timeout_is_treated_as_offline_too():
    import requests
    msg = updater.describe_check_failure(requests.Timeout("timed out"))
    assert "offline" in msg.lower(), msg


def test_an_unrecognised_exception_still_returns_a_sentence():
    """Never raise on the error path, and never return None — the caller puts
    this straight into a JSON body the page renders."""
    msg = updater.describe_check_failure(ValueError("something else entirely"))
    assert isinstance(msg, str) and msg.strip(), repr(msg)


def test_every_cause_produces_a_DIFFERENT_sentence():
    """The control for the whole file. Each assertion above would still pass
    if the function returned one generic sentence containing every keyword,
    which is exactly the failure being fixed."""
    import requests
    reset = int(datetime.now().timestamp()) + 600
    messages = [
        updater.describe_check_failure(_rate_limited(reset=reset)),
        updater.describe_check_failure(_http_error(404)),
        updater.describe_check_failure(_http_error(401)),
        updater.describe_check_failure(_http_error(500)),
        updater.describe_check_failure(requests.ConnectionError("down")),
    ]
    assert len(set(messages)) == len(messages), (
        "two different causes produced the same message:\n  " + "\n  ".join(messages))


# --- the page must actually USE the local route ----------------------------
# The route tests in test_admin_routes.py prove /settings/updates/status does
# not call GitHub. They say nothing about which route the page asks for on
# load, so on their own they would still pass if someone pointed
# loadUpdatesStatus() back at /check — which IS the bug. Static, because the
# alternative is a browser.

SETTINGS_HTML = pathlib.Path(__file__).parent.parent / "templates" / "settings.html"


def _js_function_body(name):
    src = SETTINGS_HTML.read_text(encoding="utf-8")
    start = src.index(f"async function {name}(")
    # to the closing brace of the function, found by the next line that is a
    # bare "}" at column 0 -- these are top-level functions in a <script>.
    end = src.index("\n}", start)
    return src[start:end]


def _calls_route(body, path, endpoint):
    """True if this JS body targets that route, written either way.

    URLs in the templates moved from string literals to url_for() (see
    test_frontend.py's hardcoded-URL guard), so the source now reads
    `{{ url_for('settings.settings_updates_status') }}` where it used to read
    "/settings/updates/status". Both spellings mean the same request; matching
    either keeps this guard about WHICH ROUTE IS CALLED rather than about how
    the URL happens to be spelled. The rendered page still contains the literal
    path, which is what the browser tier sees.
    """
    return path in body or endpoint in body


def test_the_settings_page_asks_the_local_route_on_load():
    body = _js_function_body("loadUpdatesStatus")
    assert _calls_route(body, "/settings/updates/status", "settings_updates_status"), (
        "the page-load handler must call the local-only status route")
    assert not _calls_route(body, "/settings/updates/check", "settings_updates_check"), (
        "the page-load handler is calling GitHub again — every Settings visit "
        "spends one of the 60 requests this network gets per hour")


def test_the_check_button_still_asks_the_route_that_calls_github():
    """The control. Without it, deleting the GitHub call altogether would pass
    the test above while quietly removing the feature."""
    body = _js_function_body("checkForUpdates")
    assert _calls_route(body, "/settings/updates/check", "settings_updates_check"), (
        "the Check for Updates button must still perform a real check")
