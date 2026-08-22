"""
Lightweight in-process background job tracker, used by anything slow
enough to need a visible progress bar — currently the in-app updater's
Update Now / Rollback flow (see updater.py, and the Settings -> Updates
section of app.py).

This app runs as a single Python process (see the launcher scripts and
serve()/app.run() in app.py) with no task queue or multi-worker
deployment, so an in-memory registry protected by a lock is the
right-sized tool here — it doesn't need to survive a process restart, and
it doesn't need to coordinate across worker processes that don't exist.
If VetClinicSystem JO ever moves to multiple worker processes, this would
need to move to something shared across them (Postgres itself, most
simply); it's explicitly not that today.

Every job is a sequence of named STEPS. progress is always
`steps completed / total steps` — a real fraction of real work done, not
a timed animation standing in for one. Steps are coarse (a handful of
named phases like "Downloading release" or "Verifying the new version"),
not a smooth per-row counter, because that's the actual granularity of
work these operations do — reporting anything finer would be manufacturing
false precision.
"""
import threading
import time
import uuid

_lock = threading.Lock()
_jobs = {}


def start(step_labels, fn):
    """
    Runs fn(update) in a background thread and returns a job_id for
    polling via status(job_id).

    fn receives update(step_index) — call it each time a named step (from
    step_labels) finishes. fn's return value is stashed as the job's
    `result` once it completes, for the caller to use however it needs
    (e.g. the finished report data, ready for a normal in-request render).
    """
    job_id = uuid.uuid4().hex[:12]
    state = {
        "steps": list(step_labels),
        "current": 0,
        "fraction": None,  # optional 0..1 override for jobs with real sub-step
                            # progress within a single step (see update() below)
        "status": "running",  # running | done | error
        "error": None,
        "result": None,
        "started_at": time.time(),
        "finished_at": None,
    }
    with _lock:
        _jobs[job_id] = state
        _prune_locked()

    def update(step_index, label=None, fraction=None):
        """Advances to step_index. label, if given, overwrites that
        step's display text in place — used for live sub-counts within a
        single step. fraction (0..1), if given, overrides the
        current/total-steps percentage the client would otherwise
        compute — for a job that's conceptually one step but has a real
        internal count, this lets the progress bar reflect that real
        count instead of just jumping from 0% to 100%."""
        with _lock:
            if job_id in _jobs:
                _jobs[job_id]["current"] = step_index
                if label is not None and 0 <= step_index < len(_jobs[job_id]["steps"]):
                    _jobs[job_id]["steps"][step_index] = label
                if fraction is not None:
                    _jobs[job_id]["fraction"] = max(0.0, min(1.0, fraction))

    def runner():
        try:
            result = fn(update)
            with _lock:
                if job_id in _jobs:
                    _jobs[job_id]["status"] = "done"
                    _jobs[job_id]["current"] = len(state["steps"])
                    _jobs[job_id]["result"] = result
                    _jobs[job_id]["finished_at"] = time.time()
        except Exception as e:
            with _lock:
                if job_id in _jobs:
                    _jobs[job_id]["status"] = "error"
                    _jobs[job_id]["error"] = str(e)
                    _jobs[job_id]["finished_at"] = time.time()

    threading.Thread(target=runner, daemon=True).start()
    return job_id


def status(job_id):
    """Returns a snapshot (safe to read after the lock is released) or None
    if the job doesn't exist / has been pruned. `steps` is copied too, not
    just the outer dict — update() mutates that list in place under the
    lock, so a plain dict(state) would still share the same list object
    and could show a step's label changing mid-read after the lock is
    released."""
    with _lock:
        state = _jobs.get(job_id)
        if state is None:
            return None
        snapshot = dict(state)
        snapshot["steps"] = list(state["steps"])
        return snapshot


def take_result(job_id):
    """Reads and removes a finished job's result in one step, so a page
    reload or a second browser tab can't accidentally reuse stale data
    from an old job. Returns None if the job isn't done (or doesn't
    exist)."""
    with _lock:
        state = _jobs.get(job_id)
        if not state or state["status"] != "done":
            return None
        del _jobs[job_id]
        return state["result"]


def _prune_locked(max_age_seconds=1800):
    """Best-effort cleanup so a long-running app doesn't accumulate
    finished/errored jobs forever. Called with _lock already held."""
    now = time.time()
    stale = [jid for jid, s in _jobs.items()
             if s.get("finished_at") and now - s["finished_at"] > max_age_seconds]
    for jid in stale:
        del _jobs[jid]
