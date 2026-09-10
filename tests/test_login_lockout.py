"""
Account lockout: escalation must depend on how many wrong guesses were made,
not on whether the attacker paused between them.

The two apps carried two different implementations of login_lock_status(),
neither documented as a deliberate divergence, and they disagreed on three of
five ordinary scenarios:

    scenario                        IQ      JO
    5 rapid failures                15m     15m     agreed
    10 rapid failures               30m     14m     disagreed
    15 rapid failures               60m     13m     disagreed
    3 bursts of 5, 30 min apart     59m     59m     agreed
    4 strays over 12h, then 5 rapid 14m     15m     disagreed

JO grouped failures into bursts and escalated per burst, so an attacker who
never paused stayed at the flat 15-minute base forever — fifteen straight
guesses cost thirteen minutes, while the same fifteen spread over three
batches cost an hour. Pausing was rewarded. IQ escalated on total volume but
had no bursts, so stray old failures dragged its anchor backwards and shortened
a real lock.

Both apps now use one implementation with both properties: bursts, AND one
escalation step per completed block of LOCKOUT_THRESHOLD *within* a burst.

The scenarios are driven through a stub connection because the function is
pure given its two queries, and that keeps this in the pure tier. One
database-tier test at the bottom exercises the real SQL, so a change to the
queries cannot pass while the stub keeps agreeing with itself.
"""
from datetime import datetime, timedelta

import pytest

import auth
from conftest import needs_db


class _Row(dict):
    def __getitem__(self, key):
        return dict.get(self, key)


class StubDB:
    """Answers exactly the two queries login_lock_status() makes."""

    def __init__(self, failures, last_success=None):
        self.failures = sorted(failures)
        self.last_success = last_success

    def execute(self, sql, params=()):
        outer = self

        class Cur:
            def fetchone(self):
                if "MAX(timestamp)" in sql:
                    return _Row(t=outer.last_success)
                return _Row()

            def fetchall(self):
                since = params[1] if len(params) > 1 else None
                return [_Row(timestamp=t.isoformat(timespec="seconds"))
                        for t in outer.failures
                        if since is None or t.isoformat(timespec="seconds") > since]

        return Cur()


def _rapid(n, end_offset_seconds=10):
    """n failures ten seconds apart, ending just now."""
    now = datetime.now()
    return [now - timedelta(seconds=(n - i) * end_offset_seconds) for i in range(n)]


def _minutes(failures):
    locked, mins, unlock = auth.login_lock_status(StubDB(failures), "victim")
    return locked, mins


# ---------------------------------------------------------------------------
# GUARDS — a sustained attack escalates
# ---------------------------------------------------------------------------

def test_ten_rapid_failures_escalate_to_the_second_step():
    """GUARD. JO's per-burst counting gave ~14 minutes here: two full blocks
    of five, in one unbroken burst, counted as a single episode."""
    locked, mins = _minutes(_rapid(10))
    assert locked
    assert 25 <= mins <= 31, f"ten straight failures bought only {mins} minutes"


def test_fifteen_rapid_failures_escalate_to_the_third_step():
    """GUARD. The clearest form: fifteen guesses with no pause must cost the
    same as fifteen guesses in three spaced batches."""
    locked, mins = _minutes(_rapid(15))
    assert locked
    assert 55 <= mins <= 61, f"fifteen straight failures bought only {mins} minutes"


def test_pausing_is_not_rewarded():
    """GUARD, stated as the property rather than the number. Continuous and
    batched attacks of the same size must cost the same."""
    now = datetime.now()
    continuous = _rapid(15)
    batched = ([now - timedelta(minutes=70) + timedelta(seconds=i * 10) for i in range(5)]
               + [now - timedelta(minutes=35) + timedelta(seconds=i * 10) for i in range(5)]
               + [now - timedelta(minutes=2) + timedelta(seconds=i * 10) for i in range(5)])
    _, mins_continuous = _minutes(continuous)
    _, mins_batched = _minutes(batched)
    assert abs(mins_continuous - mins_batched) <= 2, (
        f"continuous attack cost {mins_continuous}m but the same number of "
        f"guesses spread out cost {mins_batched}m — pausing is cheaper")


def test_stray_old_failures_do_not_shorten_a_real_lock():
    """GUARD. Burst-less counting anchors on the Nth failure OVERALL, so stray
    guesses from earlier in the day push the anchor backwards and take time off
    the lock the recent attempts just earned.

    The recent group here is spread ten minutes apart — inside the burst gap,
    so it is one burst, but spanning forty minutes. With bursts the anchor is
    its fifth and most recent failure, and the account is locked. Without
    bursts the four strays fill the first four slots, the anchor lands on the
    recent group's FIRST failure forty minutes ago, and the lock has already
    expired before it began. A tightly-packed burst cannot tell these apart —
    the anchors are then seconds rather than tens of minutes apart, which is
    why the earlier version of this test passed against both implementations.
    """
    now = datetime.now()
    strays = [now - timedelta(hours=h) for h in (12, 9, 6, 3)]
    recent = [now - timedelta(minutes=40) + timedelta(minutes=10 * i) for i in range(5)]
    locked, mins = _minutes(strays + recent)
    assert locked, "strays dragged the anchor back far enough to expire the lock"
    assert mins >= 13, f"strays shortened the lock to {mins} minutes"


# ---------------------------------------------------------------------------
# CONTROLS — the ordinary cases must be unchanged
# ---------------------------------------------------------------------------

def test_five_rapid_failures_still_lock_for_the_base_window():
    """CONTROL. The first lockout is the common case and must not have moved;
    both old implementations agreed on it."""
    locked, mins = _minutes(_rapid(5))
    assert locked
    assert 13 <= mins <= 16, mins


def test_three_spaced_bursts_still_reach_the_third_step():
    """CONTROL. Both old implementations agreed here too."""
    now = datetime.now()
    failures = ([now - timedelta(minutes=70) + timedelta(seconds=i * 10) for i in range(5)]
                + [now - timedelta(minutes=35) + timedelta(seconds=i * 10) for i in range(5)]
                + [now - timedelta(minutes=2) + timedelta(seconds=i * 10) for i in range(5)])
    locked, mins = _minutes(failures)
    assert locked
    assert 55 <= mins <= 61, mins


@pytest.mark.parametrize("n", [0, 1, 4])
def test_below_the_threshold_is_never_locked(n):
    """CONTROL. Ordinary typos must not lock anyone out."""
    locked, _ = _minutes(_rapid(n)) if n else (auth.login_lock_status(StubDB([]), "victim")[0], None)
    assert not locked


def test_stray_failures_that_never_reach_the_threshold_do_not_lock():
    """GUARD for the burst grouping: five failures spread over twelve hours are
    five separate bursts of one, not one lockout episode.

    The most recent is a minute ago deliberately. Without bursts the five
    unrelated typos count as one completed block and the anchor is that recent
    one, so the account locks — a receptionist who mistypes once every few
    hours across a shift gets locked out at the fifth. Dating the last one
    hours back instead lets the lock expire on its own and the test passes
    against a burst-less implementation too, proving nothing.
    """
    now = datetime.now()
    failures = [now - timedelta(hours=h) for h in (12, 9, 6, 3)] + [now - timedelta(minutes=1)]
    locked, _ = _minutes(failures)
    assert not locked, (
        "five unrelated typos spread across a day locked the account — "
        "they are five bursts of one, not one episode")


def test_a_successful_login_clears_the_slate():
    """CONTROL. Getting in must reset the count, or a user who eventually
    remembers their password stays locked out anyway."""
    now = datetime.now()
    failures = _rapid(10)
    just_after = (now + timedelta(seconds=1)).isoformat(timespec="seconds")
    locked, _, _ = auth.login_lock_status(StubDB(failures, last_success=just_after), "victim")
    assert not locked


def test_an_expired_lock_reports_unlocked():
    """CONTROL. The window has to actually end."""
    old = [datetime.now() - timedelta(hours=3) + timedelta(seconds=i * 10) for i in range(5)]
    locked, _ = _minutes(old)
    assert not locked


def test_the_escalation_is_capped():
    """CONTROL. Doubling forever would make one bad afternoon a permanent
    lockout; LOCKOUT_MAX_MINUTES is the ceiling."""
    locked, mins = _minutes(_rapid(60))
    assert locked
    assert mins <= auth.LOCKOUT_MAX_MINUTES + 1, mins


def test_no_username_is_never_locked():
    """CONTROL. A blank submission must not be treated as an attack on a
    real account."""
    assert auth.login_lock_status(StubDB(_rapid(20)), "")[0] is False


# ---------------------------------------------------------------------------
# The real SQL, so the stub above cannot drift away from it
# ---------------------------------------------------------------------------

@needs_db
def test_a_real_burst_of_failures_locks_the_account(db):
    """GUARD on the queries, not the algorithm. The stub answers whatever the
    function asks; only this notices if the SQL stops selecting the right
    rows."""
    import uuid
    username = f"lockvictim{uuid.uuid4().hex[:6]}"
    now = datetime.now()
    try:
        for i in range(auth.LOCKOUT_THRESHOLD):
            db.execute(
                "INSERT INTO login_log (user_id, username, success, timestamp, ip, user_agent) "
                "VALUES (?,?,?,?,?,?)",
                (None, username, 0,
                 (now - timedelta(seconds=(5 - i) * 10)).isoformat(timespec="seconds"),
                 "127.0.0.1", "test"))
        db.commit()
        locked, mins, unlock_at = auth.login_lock_status(db, username)
        assert locked, "five real failed logins did not lock the account"
        assert mins and mins >= 1
        assert unlock_at > now
    finally:
        db.execute("DELETE FROM login_log WHERE username=?", (username,))
        db.commit()


@needs_db
def test_a_real_successful_login_unlocks_it(db):
    """CONTROL against the real queries."""
    import uuid
    username = f"lockvictim{uuid.uuid4().hex[:6]}"
    now = datetime.now()
    try:
        for i in range(auth.LOCKOUT_THRESHOLD):
            db.execute(
                "INSERT INTO login_log (user_id, username, success, timestamp, ip, user_agent) "
                "VALUES (?,?,?,?,?,?)",
                (None, username, 0,
                 (now - timedelta(seconds=(6 - i) * 10)).isoformat(timespec="seconds"),
                 "127.0.0.1", "test"))
        db.execute(
            "INSERT INTO login_log (user_id, username, success, timestamp, ip, user_agent) "
            "VALUES (?,?,?,?,?,?)",
            (None, username, 1, now.isoformat(timespec="seconds"), "127.0.0.1", "test"))
        db.commit()
        locked, _, _ = auth.login_lock_status(db, username)
        assert not locked, "the account stayed locked after a successful login"
    finally:
        db.execute("DELETE FROM login_log WHERE username=?", (username,))
        db.commit()
