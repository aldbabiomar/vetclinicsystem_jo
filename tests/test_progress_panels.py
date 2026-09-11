# -*- coding: utf-8 -*-
"""Every long-running job reports through the shared progress component.

`progress.js` (VZProgress) draws the step label, a bar and the elapsed time.
Four jobs in this app take long enough to need it — update, rollback, backup
and restore — and for a long time only two of them used it. The update path
polled `job-status` by hand and wrote plain text into the panel, so the
LONGEST job in the app was the one that told the admin least: no bar, no
progress fraction, no elapsed time, while the app restarted underneath them.

That is the shape `SEAM_RULES.md` is about: the rule lived in two of four
sibling paths, and nothing failed, because each path was individually
correct.

Also pinned here: the job step labels are display text. They are sent to the
browser as JSON and drawn in the panel, so they go through `_()` like any
other string a person reads.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETTINGS_HTML = ROOT / "templates" / "settings.html"


def _job_status_pollers():
    """Places that poll the job-status endpoint by hand rather than using
    VZProgress.poll."""
    src = SETTINGS_HTML.read_text(encoding="utf-8")
    offenders = []
    for m in re.finditer(r"setInterval\(", src):
        window = src[m.start():m.start() + 900]
        if "job-status" in window or "job_id=" in window:
            offenders.append(src[:m.start()].count("\n") + 1)
    return offenders


def test_no_job_polls_status_by_hand():
    lines = _job_status_pollers()
    assert not lines, (
        "settings.html polls the job-status endpoint with its own setInterval "
        f"at line(s) {lines}. Use window.VZProgress.poll/render instead — it is "
        "already used by the other jobs on this page, and it is what draws the "
        "bar and the elapsed time.")


def test_the_update_panel_uses_the_progress_component():
    src = SETTINGS_HTML.read_text(encoding="utf-8")
    m = re.search(r"function runUpdateJob\(.*?\n\}", src, re.S)
    assert m, "runUpdateJob is gone — if the update flow moved, point this test at it"
    body = m.group(0)
    assert "VZProgress.poll" in body and "VZProgress.render" in body, (
        "the update job does not render through VZProgress, so Update and "
        "Rollback show no progress bar")


def test_the_panel_is_styled_as_a_progress_panel():
    """`.vz-progress-panel` is what gives the rendered bar its frame. With the
    old text-only class the markup renders unstyled rather than visibly
    broken, which is exactly the kind of thing nobody reports."""
    src = SETTINGS_HTML.read_text(encoding="utf-8")
    m = re.search(r'<div id="updateProgress"[^>]*>', src)
    assert m, "the update progress panel is missing"
    assert "vz-progress-panel" in m.group(0), (
        f"the update panel is not styled as a progress panel: {m.group(0)}")


def test_job_step_labels_are_translated():
    """The labels are sent to the browser and drawn in the panel, so they are
    display text. An untranslated list leaves the progress bar in English on
    an otherwise Arabic screen."""
    offenders = []
    for rel in ("routes/settings.py", "app.py", "routes/consignment.py"):
        f = ROOT / rel
        if not f.exists():
            continue
        src = f.read_text(encoding="utf-8")
        for m in re.finditer(r"jobs\.start\(\s*(\[[^\]]*\])", src, re.S):
            steps = m.group(1)
            for lit in re.findall(r'"([^"]{3,})"', steps):
                if f'_("{lit}")' not in steps:
                    line = src[:m.start()].count("\n") + 1
                    offenders.append(f"{rel}:{line}: {lit!r}")
    assert not offenders, (
        "These job step labels are not translated, so the progress bar shows "
        "English on an Arabic page:\n  " + "\n  ".join(offenders))


def test_control_the_scanners_read_real_code():
    """Floors. Each of the checks above passes hardest when it matches
    nothing at all."""
    src = SETTINGS_HTML.read_text(encoding="utf-8")
    assert src.count("VZProgress.poll") >= 3, (
        "fewer VZProgress users than this page has jobs — the scanner is "
        "probably looking at the wrong file")
    starts = 0
    for rel in ("routes/settings.py", "app.py", "routes/consignment.py"):
        f = ROOT / rel
        if f.exists():
            starts += len(re.findall(r"jobs\.start\(", f.read_text(encoding="utf-8")))
    assert starts >= 4, f"only {starts} jobs.start() calls found"
